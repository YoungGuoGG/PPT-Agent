from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


_NON_CONTENT_PLACEHOLDERS = {"title", "ctrTitle", "subTitle", "dt", "ftr", "sldNum"}


def build_template_context(import_dir: Path) -> dict[str, Any]:
    import_dir = import_dir.expanduser().resolve()
    manifest_path = import_dir / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"template import manifest does not exist: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    slides = manifest.get("slides")
    if not isinstance(slides, list) or not slides:
        raise ValueError("template import manifest must contain slides")

    slides_by_index = {int(slide["index"]): slide for slide in slides}
    ordered_indices = sorted(slides_by_index)
    candidates = manifest.get("pageTypeCandidates") or {}
    cover_index = _candidate_index(candidates, "cover_candidate", ordered_indices[0])
    ending_index = _candidate_index(candidates, "ending_candidate", ordered_indices[-1])
    content_fallback = next(
        (index for index in ordered_indices if index not in {cover_index, ending_index}),
        cover_index,
    )
    content_index = _candidate_index(candidates, "content_candidate", content_fallback)
    canvas = _canvas(manifest.get("slideSize") or {})
    asset_href_prefix = _output_asset_href_prefix(import_dir)

    roles = {
        "cover": _role_record(import_dir, manifest, slides_by_index[cover_index], "literal", "fixed-composition", canvas, asset_href_prefix),
        "content": _role_record(import_dir, manifest, slides_by_index[content_index], "adapted", "free-content-area", canvas, asset_href_prefix),
        "ending": _role_record(import_dir, manifest, slides_by_index[ending_index], "literal", "fixed-composition", canvas, asset_href_prefix),
    }
    return {
        "schema": "template_guidance.v1",
        "svg_distribution_policy": "locked",
        "import_dir": str(import_dir),
        "manifest_path": str(manifest_path),
        "source": manifest.get("source") or {},
        "canvas": canvas,
        "theme": manifest.get("theme") or {},
        "assets_dir": str(import_dir / str((manifest.get("assets") or {}).get("exportDir") or "assets")),
        "roles": roles,
    }


def resolve_page_reference(context: dict[str, Any], page_index: int, page_count: int) -> dict[str, Any]:
    if page_count < 1:
        raise ValueError("page_count must be at least 1")
    if page_index < 1 or page_index > page_count:
        raise ValueError(f"page_index {page_index} is outside 1..{page_count}")
    role = "cover" if page_index == 1 else "ending" if page_index == page_count else "content"
    reference = dict(context["roles"][role])
    reference["svg_distribution_policy"] = context.get("svg_distribution_policy", "locked")
    reference["role"] = role
    reference["page_index"] = page_index
    return reference


def load_reference_svg(reference: dict[str, Any]) -> str:
    path = Path(str(reference.get("svg_path") or "")).expanduser().resolve()
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"template reference SVG does not exist: {path}")
    svg = path.read_text(encoding="utf-8")
    if "<svg" not in svg or "</svg>" not in svg:
        raise ValueError(f"template reference is not a complete SVG: {path}")
    asset_href_prefix = str(reference.get("asset_href_prefix") or "../assets").rstrip("/")
    svg = svg.replace("../assets/", f"{asset_href_prefix}/")
    return svg


def _candidate_index(candidates: dict[str, Any], key: str, fallback: int) -> int:
    values = candidates.get(key)
    if isinstance(values, list) and values:
        return int(values[0])
    return fallback


def _canvas(slide_size: dict[str, Any]) -> dict[str, int]:
    width = int(slide_size.get("width_px") or 1280)
    height = int(slide_size.get("height_px") or 720)
    return {"width": width, "height": height}


def _role_record(
    import_dir: Path,
    manifest: dict[str, Any],
    slide: dict[str, Any],
    behavior: str,
    layout_policy: str,
    canvas: dict[str, int],
    asset_href_prefix: str,
) -> dict[str, Any]:
    return {
        "slide_index": int(slide["index"]),
        "svg_path": str(_resolve_svg_path(import_dir, slide)),
        "behavior": behavior,
        "layout_policy": layout_policy,
        "content_area": _content_area(manifest, slide, canvas),
        "asset_href_prefix": asset_href_prefix,
    }


def _output_asset_href_prefix(import_dir: Path) -> str:
    if import_dir.parent.name != "templates":
        return "../assets"
    project_path = import_dir.parent.parent
    relative = os.path.relpath(import_dir / "assets", project_path / "svg_output")
    return Path(relative).as_posix()


def _resolve_svg_path(import_dir: Path, slide: dict[str, Any]) -> Path:
    flat_name = str(slide.get("flatSvgFile") or slide.get("svgFile") or "")
    layered_name = str(slide.get("svgFile") or flat_name)
    candidates = [import_dir / "svg-flat" / flat_name, import_dir / "svg" / layered_name]
    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate.resolve()
    raise FileNotFoundError(f"template reference SVG does not exist for slide {slide.get('index')}")


def _content_area(
    manifest: dict[str, Any],
    slide: dict[str, Any],
    canvas: dict[str, int],
) -> dict[str, int]:
    layout_path = slide.get("layoutPath")
    layouts = manifest.get("layouts") if isinstance(manifest.get("layouts"), list) else []
    layout = next((item for item in layouts if item.get("path") == layout_path), None)
    placeholders = layout.get("placeholders", []) if isinstance(layout, dict) else []
    geometries = [
        item["geometry"]
        for item in placeholders
        if isinstance(item, dict)
        and item.get("type") not in _NON_CONTENT_PLACEHOLDERS
        and isinstance(item.get("geometry"), dict)
    ]
    if geometries:
        return dict(max(geometries, key=lambda item: int(item.get("width", 0)) * int(item.get("height", 0))))
    margin_x = max(round(canvas["width"] * 0.07), 24)
    margin_top = max(round(canvas["height"] * 0.16), 36)
    margin_bottom = max(round(canvas["height"] * 0.08), 24)
    return {
        "x": margin_x,
        "y": margin_top,
        "width": canvas["width"] - margin_x * 2,
        "height": canvas["height"] - margin_top - margin_bottom,
    }
