from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from PIL import Image

from .conformance import sha256_file
from .tools import image_tools
from .tools.project_tools import ToolExecutionError


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
ICON_LIBRARY_SUMMARY = {
    "placeholder_syntax": '<use data-icon="tabler-outline/chart-bar" x="100" y="120" width="36" height="36" fill="#2563EB"/>',
    "finalize_behavior": "finalize_svg.py embeds these placeholders from templates/icons during export.",
    "libraries": [
        {
            "prefix": "chunk-filled",
            "style": "solid geometric fill, sharp and dense; good for enterprise, engineering, dashboards",
            "examples": ["chart-bar", "users", "shield", "database", "rocket", "settings"],
        },
        {
            "prefix": "tabler-filled",
            "style": "solid rounded fill, approachable; good for product, education, lifestyle",
            "examples": ["home", "heart", "star", "user", "chart-pie", "bulb"],
        },
        {
            "prefix": "tabler-outline",
            "style": "thin stroke line icons; good for refined screen decks and lightweight UI diagrams",
            "examples": ["home", "users", "chart-bar", "target", "settings", "sparkles"],
        },
        {
            "prefix": "phosphor-duotone",
            "style": "soft duotone single-color icons; good for modern explanatory slides",
            "examples": ["house", "chart-line-up", "users-three", "shield-check", "database", "rocket-launch"],
        },
        {
            "prefix": "simple-icons",
            "style": "brand logos only; may be used alongside the selected generic icon library",
            "examples": ["github", "openai", "microsoft", "google", "amazonaws", "slack"],
        },
    ],
    "rules": [
        "Choose one generic icon library per deck based on the actual task and style.",
        "Use simple-icons only for real brand/product logos.",
        "Use icon placeholders only when icons improve scanning, diagrams, navigation, or concept labels.",
    ],
}


@dataclass(frozen=True)
class ImageManifestVerification:
    ready: bool
    verified: tuple[dict[str, Any], ...]
    incomplete_asset_ids: tuple[str, ...]
    issues: tuple[dict[str, Any], ...]


def build_visual_asset_prompt(project_path: Path) -> str:
    context = build_visual_asset_context(project_path)
    return (
        "Visual asset capabilities available to this workflow:\n"
        f"{json.dumps(context, ensure_ascii=False, indent=2)}\n\n"
        "Decision rules:\n"
        "- Decide from the actual task whether icons or generated raster images are useful; do not use them by default.\n"
        "- For icons, choose a deck-wide generic library and emit <use data-icon=\"library/name\" .../> placeholders in SVG.\n"
        "- For generated images, include image.image_prompts.items in the strategy only when raster imagery materially improves the deck.\n"
        "- Each image prompt item must include asset_id, filename, prompt, purpose, aspect_ratio, status, and slide_index; asset_id must be a stable unique slug. placement and alt_text are optional.\n"
        "- Use status \"Pending\" for image prompts that the image provider should generate.\n"
        "- Generated images become project-local SVG hrefs such as ../images/generated_name.png or ../images/generated_name.webp.\n"
    )


def build_visual_asset_context(project_path: Path) -> dict[str, Any]:
    return {
        "icon_library": ICON_LIBRARY_SUMMARY,
        "image_provider": {
            "available": _image_provider_configured(),
            "manifest_path": "images/image_prompts.json",
            "manifest_schema": {
                "items": [
                    {
                        "asset_id": "slide_03_hero",
                        "filename": "slide_03_hero.png",
                        "prompt": "Detailed image prompt aligned to the deck style and slide purpose.",
                        "aspect_ratio": "16:9",
                        "status": "Pending",
                        "slide_index": 3,
                        "purpose": "hero visual",
                        "placement": "full bleed background",
                        "alt_text": "Short description",
                    }
                ]
            },
            "note": "If no provider is configured, the service keeps the manifest and falls back to SVG-native visuals.",
        },
        "available_generated_images": collect_generated_images(project_path),
    }


def materialize_visual_assets(
    project_path: Path,
    strategy: dict[str, Any],
    *,
    log: Callable[[str, dict[str, Any]], None] | None = None,
    run_provider: bool = True,
    resume: bool = False,
    required_asset_ids: set[str] | None = None,
    provider: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    image_prompts = _extract_image_prompts(strategy)
    manifest_path = project_path / "images" / "image_prompts.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    manifest_result: dict[str, Any] = {
        "image_prompts": 0,
        "manifest_path": str(manifest_path),
        "provider_attempted": False,
        "provider_available": provider is not None or _image_provider_configured(),
        "provider_error": None,
        "generated_images": [],
        "verified_images": [],
        "incomplete_asset_ids": [],
        "preserved_generated": 0,
        "issues": [],
    }

    if image_prompts:
        fresh_manifest = _build_image_manifest(project_path, strategy, image_prompts)
        manifest = _merge_image_manifest(project_path, manifest_path, fresh_manifest, resume=resume)
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        manifest_result["image_prompts"] = len(manifest["items"])
        required = required_asset_ids or {str(item["asset_id"]) for item in manifest["items"]}
        before = verify_image_manifest(project_path, manifest_path, required_asset_ids=required)
        manifest_result["preserved_generated"] = len(before.verified)
        _log(log, "visual_asset_image_manifest_written", {"path": str(manifest_path), "items": len(manifest["items"])})
        if run_provider and before.incomplete_asset_ids:
            manifest_result["provider_attempted"] = True
            try:
                result = (provider or image_tools.generate_images_from_manifest)(str(manifest_path))
                tool_payload = result.to_dict() if hasattr(result, "to_dict") else {"result": str(result)}
                _log(log, "visual_asset_image_provider_complete", {"tool": tool_payload})
            except (ToolExecutionError, RuntimeError, ValueError) as exc:
                manifest_result["provider_error"] = str(exc)
                _log(log, "visual_asset_image_provider_unavailable", {"error": str(exc)})
                manifest = _read_manifest(manifest_path)
                for item in manifest["items"]:
                    if item.get("status") in {"Pending", "Failed"}:
                        item["status"] = "Needs-Manual"
                manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        verification = verify_image_manifest(project_path, manifest_path, required_asset_ids=required)
        _write_verification_metadata(manifest_path, verification)
        try:
            image_tools.render_image_manifest_markdown(str(manifest_path))
        except ToolExecutionError as exc:
            _log(log, "visual_asset_image_manifest_markdown_failed", {"error": str(exc)})
        manifest_result["verified_images"] = list(verification.verified)
        manifest_result["incomplete_asset_ids"] = list(verification.incomplete_asset_ids)
        manifest_result["issues"] = list(verification.issues)
    else:
        _log(log, "visual_asset_image_manifest_skipped", {"reason": "strategy did not request generated images"})

    generated_images = list(manifest_result["verified_images"])
    manifest_result["generated_images"] = generated_images
    _attach_generated_images(strategy, image_prompts, generated_images)
    strategy["visual_assets"] = {
        **build_visual_asset_context(project_path),
        "materialization": manifest_result,
    }
    (project_path / "visual_assets.json").write_text(
        json.dumps(strategy["visual_assets"], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (project_path / ".service_strategy.json").write_text(
        json.dumps(strategy, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if isinstance(strategy.get("spec_lock"), dict):
        (project_path / "spec_lock.md").write_text(
            json.dumps(strategy["spec_lock"], ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return manifest_result


def verify_image_manifest(
    project_path: Path,
    manifest_path: Path,
    *,
    required_asset_ids: set[str],
) -> ImageManifestVerification:
    manifest = _read_manifest(manifest_path)
    items = manifest.get("items")
    if not isinstance(items, list):
        raise ValueError(f"image manifest items must be a list: {manifest_path}")
    by_id = {
        str(item.get("asset_id") or ""): item
        for item in items
        if isinstance(item, dict) and item.get("asset_id")
    }
    verified: list[dict[str, Any]] = []
    incomplete: list[str] = []
    issues: list[dict[str, Any]] = []
    for asset_id in sorted(required_asset_ids):
        item = by_id.get(asset_id)
        if item is None:
            incomplete.append(asset_id)
            issues.append(_image_issue("image.manifest.asset_missing", asset_id, "manifest", "an item with the required asset_id"))
            continue
        filename = _safe_filename(str(item.get("filename") or ""))
        image_path = manifest_path.parent / filename
        if str(item.get("status") or "") != "Generated":
            incomplete.append(asset_id)
            issues.append(_image_issue("image.manifest.status", asset_id, item.get("status"), "Generated with a verified file"))
            continue
        if not image_path.is_file() or image_path.stat().st_size == 0:
            incomplete.append(asset_id)
            issues.append(_image_issue("image.artifact.missing", asset_id, str(image_path), "a non-empty readable raster file"))
            continue
        try:
            width, height, checksum = _verify_raster(image_path)
        except (OSError, ValueError) as exc:
            incomplete.append(asset_id)
            issues.append(_image_issue("image.artifact.unreadable", asset_id, str(exc), "a readable PNG, JPEG, or WebP file"))
            continue
        relative = image_path.relative_to(project_path).as_posix()
        verified.append(
            {
                "asset_id": asset_id,
                "filename": filename,
                "asset": relative,
                "svg_href": f"../{relative}",
                "stem": image_path.stem,
                "checksum": checksum,
                "width": width,
                "height": height,
            }
        )
    return ImageManifestVerification(
        ready=not incomplete,
        verified=tuple(verified),
        incomplete_asset_ids=tuple(incomplete),
        issues=tuple(issues),
    )


def collect_generated_images(project_path: Path) -> list[dict[str, Any]]:
    images_dir = project_path / "images"
    if not images_dir.exists():
        return []
    results: list[dict[str, Any]] = []
    for path in sorted(images_dir.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        if path.name == "image_prompts.json":
            continue
        relative = path.relative_to(project_path).as_posix()
        results.append(
            {
                "filename": path.name,
                "asset": relative,
                "svg_href": f"../{relative}",
                "stem": path.stem,
            }
        )
    return results


def _extract_image_prompts(strategy: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[Any] = []
    image = strategy.get("image")
    if isinstance(image, dict):
        image_prompts = image.get("image_prompts")
        if isinstance(image_prompts, dict):
            candidates.append(image_prompts.get("items"))
        else:
            candidates.append(image_prompts)
        candidates.append(image.get("generated_images"))
    candidates.append(strategy.get("image_prompts"))

    items: list[dict[str, Any]] = []
    for candidate in candidates:
        if not isinstance(candidate, list):
            continue
        for item in candidate:
            if isinstance(item, dict):
                normalized = _normalize_image_prompt_item(item, len(items) + 1)
                if normalized:
                    items.append(normalized)
    return items


def _normalize_image_prompt_item(item: dict[str, Any], offset: int) -> dict[str, Any] | None:
    prompt = str(item.get("prompt") or "").strip()
    if not prompt:
        return None
    filename = _safe_filename(str(item.get("filename") or f"generated_{offset:02d}.png"))
    aspect_ratio = str(item.get("aspect_ratio") or "16:9").strip()
    normalized = dict(item)
    normalized.update(
        {
            "asset_id": str(item.get("asset_id") or f"generated:{filename}"),
            "filename": filename,
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
            "status": str(item.get("status") or "Pending"),
        }
    )
    return normalized


def _safe_filename(filename: str) -> str:
    name = Path(filename).name.strip() or "generated.png"
    if not Path(name).suffix:
        name += ".png"
    return name.replace(" ", "_")


def _build_image_manifest(project_path: Path, strategy: dict[str, Any], items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "project": project_path.name,
        "deck_style_anchor": strategy.get("visual_theme") or strategy.get("beautify_strategy") or {},
        "color_scheme": (strategy.get("spec_lock") or {}).get("colors") or {},
        "items": items,
    }


def _merge_image_manifest(
    project_path: Path,
    manifest_path: Path,
    fresh: dict[str, Any],
    *,
    resume: bool,
) -> dict[str, Any]:
    existing = _read_manifest(manifest_path) if manifest_path.is_file() else {"items": []}
    old_by_id = {
        str(item.get("asset_id") or ""): item
        for item in existing.get("items") or []
        if isinstance(item, dict) and item.get("asset_id")
    }
    merged_items: list[dict[str, Any]] = []
    for fresh_item in fresh["items"]:
        item = dict(fresh_item)
        old = old_by_id.get(str(item["asset_id"]))
        fields_match = bool(
            old
            and old.get("filename") == item.get("filename")
            and old.get("prompt") == item.get("prompt")
            and old.get("aspect_ratio") == item.get("aspect_ratio")
        )
        if fields_match and old is not None:
            item.update({key: value for key, value in old.items() if key not in {"last_error"}})
        image_path = manifest_path.parent / str(item["filename"])
        if item.get("status") == "Generated":
            try:
                _verify_raster(image_path)
            except (OSError, ValueError):
                item["status"] = "Pending"
        elif image_path.is_file():
            try:
                _verify_raster(image_path)
            except (OSError, ValueError):
                pass
            else:
                item["status"] = "Generated"
        if resume and item.get("status") in {"Failed", "Needs-Manual"}:
            item["status"] = "Pending"
            item.pop("last_error", None)
        merged_items.append(item)
    return {**fresh, "items": merged_items}


def _read_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"image manifest must be an object: {path}")
    return payload


def _verify_raster(path: Path) -> tuple[int, int, str]:
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError("image file is missing or empty")
    if path.suffix.lower() not in IMAGE_EXTENSIONS:
        raise ValueError(f"unsupported raster extension: {path.suffix}")
    with Image.open(path) as image:
        image.verify()
    with Image.open(path) as image:
        width, height = image.size
    return width, height, sha256_file(path)


def _write_verification_metadata(
    manifest_path: Path,
    verification: ImageManifestVerification,
) -> None:
    manifest = _read_manifest(manifest_path)
    verified_by_id = {item["asset_id"]: item for item in verification.verified}
    for item in manifest.get("items") or []:
        verified = verified_by_id.get(str(item.get("asset_id") or ""))
        if verified:
            item.update(
                {
                    "status": "Generated",
                    "checksum": verified["checksum"],
                    "width": verified["width"],
                    "height": verified["height"],
                }
            )
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _image_issue(code: str, asset_id: str, observed: Any, required: str) -> dict[str, Any]:
    return {
        "code": code,
        "asset_id": asset_id,
        "observed": observed,
        "required": required,
    }


def _attach_generated_images(
    strategy: dict[str, Any],
    requested_items: list[dict[str, Any]],
    generated_images: list[dict[str, Any]],
) -> None:
    if not requested_items or not generated_images:
        return
    by_stem = {image["stem"]: image for image in generated_images}
    by_name = {image["filename"]: image for image in generated_images}

    slide_slots: dict[int, list[dict[str, Any]]] = {}
    for item in requested_items:
        image = by_name.get(item["filename"]) or by_stem.get(Path(item["filename"]).stem)
        if not image:
            continue
        try:
            slide_index = int(item.get("slide_index") or item.get("page_index") or 0)
        except (TypeError, ValueError):
            slide_index = 0
        if slide_index <= 0:
            continue
        slot = {
            "asset_id": f"generated:{image['filename']}",
            "path": image["svg_href"],
            "role": "generated_image",
            "placement": item.get("placement") or item.get("purpose") or "supporting visual",
            "reason": item.get("purpose") or "Generated by image provider for this slide.",
            "alt_text": item.get("alt_text") or "",
        }
        slide_slots.setdefault(slide_index, []).append(slot)

    for key in ("outline", "slide_briefs"):
        slides = strategy.get(key)
        if not isinstance(slides, list):
            continue
        for offset, slide in enumerate(slides, start=1):
            if not isinstance(slide, dict):
                continue
            try:
                index = int(slide.get("index") or offset)
            except (TypeError, ValueError):
                index = offset
            slots = slide_slots.get(index)
            if not slots:
                continue
            existing = slide.get("image_slots")
            if not isinstance(existing, list):
                existing = []
            slide["image_slots"] = [*existing, *slots]

    spec_lock = strategy.get("spec_lock")
    if isinstance(spec_lock, dict):
        outline = spec_lock.get("outline")
        if isinstance(outline, list):
            for offset, slide in enumerate(outline, start=1):
                if not isinstance(slide, dict):
                    continue
                try:
                    index = int(slide.get("index") or offset)
                except (TypeError, ValueError):
                    index = offset
                slots = slide_slots.get(index)
                if not slots:
                    continue
                existing = slide.get("image_slots")
                if not isinstance(existing, list):
                    existing = []
                slide["image_slots"] = [*existing, *slots]


def _image_provider_configured() -> bool:
    return bool(os.environ.get("IMAGE_BACKEND"))


def _log(log: Callable[[str, dict[str, Any]], None] | None, event: str, payload: dict[str, Any]) -> None:
    if log:
        log(event, payload)
