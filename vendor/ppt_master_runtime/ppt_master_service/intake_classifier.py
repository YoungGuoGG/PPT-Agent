from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .llm_client import LLMError, call_llm_json
from .schemas import GeneratePPTRequest, WorkflowMode
from .workflow_router import route_workflow


WORKFLOWS = {
    "topic_research",
    "document_to_ppt",
    "template_fill_pptx",
    "template_guided_svg",
    "revision",
    "brand_style",
    "image_heavy",
    "mirror_template",
}
POWERPOINT_SUFFIXES = {".pptx", ".pptm", ".ppsx", ".ppsm", ".potx", ".potm"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".svg"}


def classify_auto_request(request: GeneratePPTRequest) -> GeneratePPTRequest:
    if request.mode != "auto":
        return request
    if request.template_pptx and request.template_strategy == "guided_svg":
        return _with_plan(
            request,
            "template_guided_svg",
            {},
            fallback_reason="explicit template_strategy guided_svg",
        )

    available_paths = _candidate_paths(request)
    if not available_paths:
        workflow = route_workflow(request)
        return _with_plan(request, workflow, {}, fallback_reason="no uploaded files to classify")

    metadata = [_file_metadata(path) for path in available_paths]
    try:
        raw_plan = _call_intake_llm(request, metadata)
        return _normalize_llm_plan(request, raw_plan, available_paths)
    except Exception as exc:
        fallback = _deterministic_roles(request, available_paths)
        fallback_request = request.model_copy(update=fallback)
        workflow = route_workflow(fallback_request)
        return _with_plan(
            fallback_request,
            workflow,
            {"fallback_error": str(exc)},
            fallback_reason="intake classifier failed; used deterministic fallback",
        )


def _candidate_paths(request: GeneratePPTRequest) -> list[str]:
    paths: list[str] = []
    paths.extend(request.source_files or [])
    paths.extend(request.brand_assets or [])
    if request.template_pptx:
        paths.append(request.template_pptx)
    if request.reference_pptx:
        paths.append(request.reference_pptx)
    return list(dict.fromkeys(str(path) for path in paths if path))


def _file_metadata(path_text: str) -> dict[str, Any]:
    path = Path(path_text)
    metadata: dict[str, Any] = {
        "path": str(path),
        "filename": path.name,
        "suffix": path.suffix.lower(),
        "exists": path.exists(),
        "size_bytes": path.stat().st_size if path.exists() and path.is_file() else None,
    }
    if path.suffix.lower() in IMAGE_SUFFIXES and path.exists():
        metadata.update(_image_metadata(path))
    return metadata


def _image_metadata(path: Path) -> dict[str, Any]:
    try:
        from PIL import Image

        with Image.open(path) as image:
            width, height = image.size
        return {
            "kind": "image",
            "width": width,
            "height": height,
            "aspect_ratio": round(width / height, 4) if height else None,
        }
    except Exception:
        return {"kind": "image"}


def _call_intake_llm(request: GeneratePPTRequest, metadata: list[dict[str, Any]]) -> dict[str, Any]:
    prompt = f"""
Classify the uploaded assets for a PPT Master production order.

You choose the workflow for auto mode. You must return only valid JSON.

Allowed workflows:
- topic_research
- document_to_ppt
- template_fill_pptx
- template_guided_svg
- revision
- brand_style
- image_heavy
- mirror_template

Asset role rules:
- content_sources: documents, spreadsheets, markdown, URLs, screenshots used as source material.
- template_pptx: one PowerPoint file to preserve/fill as a template.
- reference_pptx: one existing deck to revise or use as reference.
- brand_assets: logos, brand guides, style references.
- image_assets: photos/screenshots/diagrams that should be understood and potentially placed in slides.
- Choose template_fill_pptx when the customer wants original slots/layout preserved and copy replaced in place.
- Choose template_guided_svg when the customer wants the PPTX's visual style reused but asks for free page design, new layouts, or freedom from original text boxes.
- Do not invent paths. Use exactly the path strings supplied in file_metadata.

Customer request:
{json.dumps(request.model_dump(), ensure_ascii=False, indent=2)}

File metadata:
{json.dumps(metadata, ensure_ascii=False, indent=2)}

Return JSON with this exact shape:
{{
  "workflow": "one allowed workflow",
  "reason": "short reason",
  "content_sources": ["path"],
  "template_pptx": "path or null",
  "reference_pptx": "path or null",
  "brand_assets": ["path"],
  "image_assets": ["path"],
  "must_use_images": true,
  "research_queries": ["web search query derived from request/assets"]
}}
"""
    return call_llm_json(prompt, "You are a strict production intake classifier for presentation generation.")


def _normalize_llm_plan(
    request: GeneratePPTRequest,
    plan: dict[str, Any],
    available_paths: list[str],
) -> GeneratePPTRequest:
    allowed = set(available_paths)
    workflow = _workflow(plan.get("workflow")) or route_workflow(request)
    source_files = _path_list(plan.get("content_sources"), allowed)
    image_assets = _path_list(plan.get("image_assets"), allowed)
    brand_assets = _path_list(plan.get("brand_assets"), allowed)
    template_pptx = _single_path(plan.get("template_pptx"), allowed)
    reference_pptx = _single_path(plan.get("reference_pptx"), allowed)

    # Keep image assets available as source material so image_heavy can copy and
    # understand them, but keep template/reference PPTX out of generic sources.
    normalized_sources = [
        path
        for path in [*source_files, *image_assets]
        if path not in {template_pptx, reference_pptx} and path is not None
    ]
    if not normalized_sources and not template_pptx and not reference_pptx and not brand_assets:
        fallback = _deterministic_roles(request, available_paths)
        normalized_sources = fallback["source_files"]
        template_pptx = fallback["template_pptx"]
        reference_pptx = fallback["reference_pptx"]
        brand_assets = fallback["brand_assets"]
        image_assets = fallback["image_assets"]

    update = {
        "mode": workflow,
        "source_files": list(dict.fromkeys(normalized_sources)),
        "template_pptx": template_pptx,
        "reference_pptx": reference_pptx,
        "brand_assets": brand_assets,
        "image_assets": image_assets,
        "intake_plan": {
            "classifier": "llm",
            "workflow": workflow,
            "reason": str(plan.get("reason") or ""),
            "must_use_images": bool(plan.get("must_use_images")),
            "research_queries": [str(item) for item in plan.get("research_queries") or [] if item],
            "raw": plan,
        },
    }
    return request.model_copy(update=update)


def _deterministic_roles(request: GeneratePPTRequest, available_paths: list[str]) -> dict[str, Any]:
    prompt = " ".join(str(part or "") for part in (request.prompt, request.topic, request.style, request.extra_requirements)).lower()
    pptx_paths = [path for path in available_paths if Path(path).suffix.lower() in POWERPOINT_SUFFIXES]
    image_paths = [path for path in available_paths if Path(path).suffix.lower() in IMAGE_SUFFIXES]
    template_pptx = None
    reference_pptx = None
    if pptx_paths:
        if any(token in prompt for token in ("template", "模板", "版式", "填充", "preserve", "layout")):
            template_pptx = pptx_paths[0]
        elif any(token in prompt for token in ("修改", "美化", "润色", "revision", "polish", "revise")):
            reference_pptx = pptx_paths[0]
        else:
            template_pptx = pptx_paths[0]
    excluded = {item for item in (template_pptx, reference_pptx) if item}
    return {
        "source_files": [path for path in available_paths if path not in excluded],
        "template_pptx": template_pptx,
        "reference_pptx": reference_pptx,
        "brand_assets": [],
        "image_assets": image_paths,
    }


def _with_plan(
    request: GeneratePPTRequest,
    workflow: str,
    extra: dict[str, Any],
    *,
    fallback_reason: str,
) -> GeneratePPTRequest:
    return request.model_copy(
        update={
            "mode": workflow,
            "intake_plan": {
                "classifier": "fallback",
                "workflow": workflow,
                "reason": fallback_reason,
                **extra,
            },
        }
    )


def _workflow(value: Any) -> WorkflowMode | None:
    workflow = str(value or "").strip()
    return workflow if workflow in WORKFLOWS else None  # type: ignore[return-value]


def _path_list(value: Any, allowed: set[str]) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item) in allowed]


def _single_path(value: Any, allowed: set[str]) -> str | None:
    path = str(value or "")
    return path if path in allowed else None
