from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from html import unescape
from pathlib import Path
from typing import Any

from .llm_client import call_vision_llm_json


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".svg"}


def understand_project_images(
    project_path: Path,
    image_files: list[str],
    request: dict[str, Any],
) -> dict[str, Any]:
    assets: list[dict[str, Any]] = []
    research_queries: list[str] = []
    for index, image_file in enumerate(image_files, start=1):
        asset = _understand_one_image(index, Path(image_file), request)
        assets.append(asset)
        research_queries.extend(str(item) for item in asset.get("research_queries") or [] if item)

    research = _research_queries(research_queries[:6])
    payload = {
        "assets": assets,
        "research": research,
    }
    (project_path / ".image_assets.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _write_markdown(project_path, payload)
    return payload


def _understand_one_image(index: int, image_file: Path, request: dict[str, Any]) -> dict[str, Any]:
    metadata = _image_metadata(image_file)
    base = {
        "asset_id": f"image_{index:03d}",
        "path": str(image_file),
        "filename": image_file.name,
        "suffix": image_file.suffix.lower(),
        **metadata,
    }
    if not _vision_enabled(image_file):
        return {
            **base,
            "understanding_status": "metadata_only",
            "visual_summary": "Image content was not inspected by a vision model.",
            "detected_text": [],
            "detected_entities": _entities_from_filename(image_file.name),
            "possible_usage": ["supporting_visual"],
            "research_queries": _entities_from_filename(image_file.name),
        }

    prompt = f"""
Analyze this uploaded image for a presentation generation workflow.

Customer request:
{json.dumps(request, ensure_ascii=False, indent=2)}

File metadata:
{json.dumps(base, ensure_ascii=False, indent=2)}

Return JSON with this exact shape:
{{
  "visual_summary": "plain-language description of image content",
  "detected_text": ["OCR text visible in the image"],
  "detected_entities": ["organizations, products, places, people, logos, landmarks"],
  "possible_usage": ["cover", "supporting_photo", "evidence_screenshot", "brand_asset", "diagram", "do_not_embed"],
  "recommended_slide_topics": ["short slide topic"],
  "research_queries": ["web search query for entities that need factual verification"],
  "should_embed_directly": true,
  "reason": "short reason"
}}
"""
    try:
        result = call_vision_llm_json(
            prompt,
            [str(image_file)],
            "You inspect images for PPT planning. Be factual and do not invent unreadable text.",
        )
    except Exception as exc:
        return {
            **base,
            "understanding_status": "vision_failed",
            "vision_error": str(exc),
            "visual_summary": "Image content was not inspected because the vision model call failed.",
            "detected_text": [],
            "detected_entities": _entities_from_filename(image_file.name),
            "possible_usage": ["supporting_visual"],
            "research_queries": _entities_from_filename(image_file.name),
        }

    return {
        **base,
        "understanding_status": "vision",
        "visual_summary": str(result.get("visual_summary") or ""),
        "detected_text": _string_list(result.get("detected_text")),
        "detected_entities": _string_list(result.get("detected_entities")),
        "possible_usage": _string_list(result.get("possible_usage")),
        "recommended_slide_topics": _string_list(result.get("recommended_slide_topics")),
        "research_queries": _string_list(result.get("research_queries")),
        "should_embed_directly": bool(result.get("should_embed_directly", True)),
        "reason": str(result.get("reason") or ""),
        "raw": result,
    }


def _image_metadata(path: Path) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "size_bytes": path.stat().st_size if path.exists() else None,
    }
    if path.suffix.lower() == ".svg":
        metadata["kind"] = "svg"
        return metadata
    try:
        from PIL import Image

        with Image.open(path) as image:
            width, height = image.size
        metadata.update(
            {
                "kind": "raster",
                "width": width,
                "height": height,
                "aspect_ratio": round(width / height, 4) if height else None,
            }
        )
    except Exception:
        metadata["kind"] = "image"
    return metadata


def _vision_enabled(image_file: Path) -> bool:
    if os.environ.get("PPT_MASTER_VISION_ENABLED", "1").strip().lower() in {"0", "false", "no"}:
        return False
    if image_file.suffix.lower() not in IMAGE_SUFFIXES - {".svg"}:
        return False
    max_bytes = int(os.environ.get("PPT_MASTER_VISION_MAX_IMAGE_BYTES", "6000000"))
    return image_file.exists() and image_file.stat().st_size <= max_bytes


def _research_queries(queries: list[str]) -> list[dict[str, str]]:
    if os.environ.get("PPT_MASTER_IMAGE_WEB_RESEARCH", "1").strip().lower() in {"0", "false", "no"}:
        return []
    results: list[dict[str, str]] = []
    seen: set[str] = set()
    for query in queries:
        if not query or query in seen:
            continue
        seen.add(query)
        try:
            results.extend(_duckduckgo_search(query, limit=2))
        except Exception:
            continue
    return results[:10]


def _duckduckgo_search(query: str, limit: int) -> list[dict[str, str]]:
    url = "https://duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
    request = urllib.request.Request(url, headers={"User-Agent": "OfficeAgent/1.0"})
    with urllib.request.urlopen(request, timeout=10) as response:
        html = response.read().decode("utf-8", errors="replace")
    matches = re.findall(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html, flags=re.S)
    results: list[dict[str, str]] = []
    for href, title_html in matches[:limit]:
        title = re.sub(r"<.*?>", "", title_html)
        results.append({"query": query, "title": unescape(title).strip(), "url": unescape(href)})
    return results


def _write_markdown(project_path: Path, payload: dict[str, Any]) -> None:
    path = project_path / "sources" / "image_understanding.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Image Understanding", ""]
    for asset in payload.get("assets") or []:
        lines.extend(
            [
                f"## {asset.get('asset_id')} - {asset.get('filename')}",
                "",
                f"- Path: {asset.get('path')}",
                f"- Status: {asset.get('understanding_status')}",
                f"- Size: {asset.get('width', 'unknown')}x{asset.get('height', 'unknown')}",
                f"- Summary: {asset.get('visual_summary')}",
                f"- Detected text: {', '.join(asset.get('detected_text') or []) or 'None'}",
                f"- Detected entities: {', '.join(asset.get('detected_entities') or []) or 'None'}",
                f"- Possible usage: {', '.join(asset.get('possible_usage') or []) or 'supporting_visual'}",
                f"- Should embed directly: {asset.get('should_embed_directly', True)}",
                "",
            ]
        )
    research = payload.get("research") or []
    if research:
        lines.extend(["## Web Research Leads", ""])
        for item in research:
            lines.append(f"- [{item.get('title')}]({item.get('url')}) - query: {item.get('query')}")
        lines.append("")
    lines.extend(
        [
            "## Execution Guidance",
            "",
            "When image assets are relevant, assign them to slide image slots explicitly. Use uploaded images directly instead of redrawing them unless the request requires a fully vector-native recreation.",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _entities_from_filename(filename: str) -> list[str]:
    stem = Path(filename).stem
    parts = [part for part in re.split(r"[_\-\s]+", stem) if len(part) >= 2]
    return parts[:5]


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]
