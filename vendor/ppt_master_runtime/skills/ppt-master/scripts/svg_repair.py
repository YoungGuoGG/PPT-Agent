#!/usr/bin/env python3
"""
PPT Master - SVG Repair Tool

Applies deterministic repairs that make LLM-authored SVG safer for native
DrawingML export. The repair step intentionally avoids layout changes and
bitmap fallbacks; it only rewrites equivalent SVG constructs into forms the
native converter and quality gate can handle.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET


SVG_NS = "http://www.w3.org/2000/svg"
ET.register_namespace("", SVG_NS)
ET.register_namespace("xlink", "http://www.w3.org/1999/xlink")

STYLE_SPLIT_RE = re.compile(r"\s*;\s*")
RGBA_RE = re.compile(r"^rgba\((.+)\)$", re.IGNORECASE)

GROUP_TAG = "g"
VISIBLE_TAGS = {
    "rect",
    "circle",
    "ellipse",
    "line",
    "polyline",
    "polygon",
    "path",
    "text",
    "tspan",
}
TEXT_TAGS = {"text", "tspan"}
STROKE_DEFAULT_TAGS = {"line", "polyline"}
DEFINITION_CONTAINER_TAGS = {
    "defs",
    "pattern",
    "linearGradient",
    "radialGradient",
    "clipPath",
    "marker",
    "filter",
    "mask",
    "symbol",
}


@dataclass
class RepairStats:
    files_seen: int = 0
    files_changed: int = 0
    group_opacity_removed: int = 0
    element_opacity_expanded: int = 0
    opacity_targets_updated: int = 0
    rgba_rewritten: int = 0
    image_opacity_overlays: int = 0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "files_seen": self.files_seen,
            "files_changed": self.files_changed,
            "group_opacity_removed": self.group_opacity_removed,
            "element_opacity_expanded": self.element_opacity_expanded,
            "opacity_targets_updated": self.opacity_targets_updated,
            "rgba_rewritten": self.rgba_rewritten,
            "image_opacity_overlays": self.image_opacity_overlays,
            "warnings": self.warnings,
        }

    def summary(self) -> str:
        return (
            f"files_changed={self.files_changed}/{self.files_seen}, "
            f"rgba_rewritten={self.rgba_rewritten}, "
            f"image_opacity_overlays={self.image_opacity_overlays}, "
            f"group_opacity_expanded={self.group_opacity_removed}"
        )


def local_name(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def parse_style(style: str | None) -> dict[str, str]:
    if not style:
        return {}
    result: dict[str, str] = {}
    for part in STYLE_SPLIT_RE.split(style.strip()):
        if not part or ":" not in part:
            continue
        key, value = part.split(":", 1)
        key = key.strip().lower()
        value = value.strip()
        if key:
            result[key] = value
    return result


def write_style(elem: ET.Element, style: dict[str, str]) -> None:
    if style:
        elem.set("style", "; ".join(f"{key}: {value}" for key, value in style.items()))
    elif "style" in elem.attrib:
        del elem.attrib["style"]


def parse_opacity(value: str | None) -> float | None:
    if value is None:
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        if raw.endswith("%"):
            opacity = float(raw[:-1]) / 100.0
        else:
            opacity = float(raw)
    except ValueError:
        return None
    return max(0.0, min(1.0, opacity))


def format_opacity(value: float) -> str:
    value = max(0.0, min(1.0, value))
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text or "0"


def pop_opacity(elem: ET.Element, stats: RepairStats, svg_file: Path) -> float | None:
    style = parse_style(elem.get("style"))
    attr_value = elem.get("opacity")
    style_value = style.get("opacity")
    opacity = parse_opacity(attr_value if attr_value is not None else style_value)

    if attr_value is not None:
        del elem.attrib["opacity"]
    if "opacity" in style:
        del style["opacity"]
        write_style(elem, style)

    if attr_value is not None or style_value is not None:
        if opacity is None:
            stats.warnings.append(f"{svg_file.name}: ignored invalid opacity on <{local_name(elem.tag)}>")
        return opacity
    return None


def get_paint(elem: ET.Element, name: str) -> str | None:
    style = parse_style(elem.get("style"))
    return style.get(name) or elem.get(name)


def opacity_property_exists(elem: ET.Element, name: str) -> bool:
    style = parse_style(elem.get("style"))
    return name in style or name in elem.attrib


def multiply_opacity_property(elem: ET.Element, name: str, multiplier: float) -> None:
    style = parse_style(elem.get("style"))
    current = parse_opacity(style.get(name) or elem.get(name))
    effective = (current if current is not None else 1.0) * multiplier
    formatted = format_opacity(effective)

    if name in style:
        style[name] = formatted
        write_style(elem, style)
    else:
        elem.set(name, formatted)


def should_apply_fill(elem: ET.Element, tag: str) -> bool:
    if tag in TEXT_TAGS:
        return True
    fill = get_paint(elem, "fill")
    if fill is not None:
        return fill.strip().lower() != "none"
    return tag not in STROKE_DEFAULT_TAGS


def should_apply_stroke(elem: ET.Element) -> bool:
    stroke = get_paint(elem, "stroke")
    if stroke is None:
        return opacity_property_exists(elem, "stroke-opacity")
    return stroke.strip().lower() != "none"


def apply_opacity_to_visible(elem: ET.Element, opacity: float, stats: RepairStats) -> None:
    tag = local_name(elem.tag)
    targets = 0
    if should_apply_fill(elem, tag):
        multiply_opacity_property(elem, "fill-opacity", opacity)
        targets += 1
    if should_apply_stroke(elem):
        multiply_opacity_property(elem, "stroke-opacity", opacity)
        targets += 1
    if targets:
        stats.opacity_targets_updated += targets


def repair_element(elem: ET.Element, inherited_opacity: float, stats: RepairStats, svg_file: Path) -> bool:
    changed = False
    tag = local_name(elem.tag)
    if tag == "image":
        return False
    if tag == GROUP_TAG and any(local_name(child.tag) == "image" for child in elem.iter() if child is not elem):
        style = parse_style(elem.get("style"))
        if elem.get("opacity") is not None or "opacity" in style:
            stats.warnings.append(
                f"{svg_file.name}: group opacity left for AI repair because the group contains an image"
            )
            return False
    own_opacity = pop_opacity(elem, stats, svg_file)

    if tag in DEFINITION_CONTAINER_TAGS:
        if own_opacity is not None:
            stats.warnings.append(
                f"{svg_file.name}: removed opacity from non-visual <{tag}> without applying it"
            )
            changed = True
        for child in list(elem):
            if repair_element(child, 1.0, stats, svg_file):
                changed = True
        return changed

    if tag == GROUP_TAG:
        child_opacity = inherited_opacity
        if own_opacity is not None:
            child_opacity *= own_opacity
            stats.group_opacity_removed += 1
            changed = True
        for child in list(elem):
            if repair_element(child, child_opacity, stats, svg_file):
                changed = True
        return changed

    effective_opacity = inherited_opacity
    if own_opacity is not None:
        effective_opacity *= own_opacity
        stats.element_opacity_expanded += 1
        changed = True

    if tag in VISIBLE_TAGS and effective_opacity < 1.0:
        apply_opacity_to_visible(elem, effective_opacity, stats)
        changed = True

    for child in list(elem):
        if repair_element(child, inherited_opacity, stats, svg_file):
            changed = True
    return changed


def repair_svg_file(svg_file: Path, stats: RepairStats) -> bool:
    try:
        tree = ET.parse(svg_file)
    except ET.ParseError as exc:
        stats.warnings.append(f"{svg_file.name}: XML parse failed: {exc}")
        return False

    root = tree.getroot()
    changed = _repair_rgba_paints(root, stats)
    if _repair_image_opacity(root, stats, svg_file):
        changed = True
    if repair_element(root, 1.0, stats, svg_file):
        changed = True
    if changed:
        tree.write(svg_file, encoding="unicode", xml_declaration=False)
    return changed


def _repair_rgba_paints(root: ET.Element, stats: RepairStats) -> bool:
    changed = False
    opacity_names = {
        "fill": "fill-opacity",
        "stroke": "stroke-opacity",
        "stop-color": "stop-opacity",
        "flood-color": "flood-opacity",
    }
    for elem in root.iter():
        style = parse_style(elem.get("style"))
        style_changed = False
        for paint_name, opacity_name in opacity_names.items():
            parsed = _parse_rgba(elem.get(paint_name))
            if parsed is not None:
                color, alpha = parsed
                elem.set(paint_name, color)
                _multiply_named_opacity(elem, opacity_name, alpha)
                stats.rgba_rewritten += 1
                changed = True
            parsed_style = _parse_rgba(style.get(paint_name))
            if parsed_style is not None:
                color, alpha = parsed_style
                style[paint_name] = color
                current = parse_opacity(style.get(opacity_name) or elem.get(opacity_name))
                style[opacity_name] = format_opacity((current if current is not None else 1.0) * alpha)
                stats.rgba_rewritten += 1
                style_changed = True
                changed = True
        if style_changed:
            write_style(elem, style)
    return changed


def _parse_rgba(value: str | None) -> tuple[str, float] | None:
    if not value:
        return None
    match = RGBA_RE.match(value.strip())
    if not match:
        return None
    parts = [part.strip() for part in match.group(1).split(",")]
    if len(parts) != 4:
        return None
    try:
        channels = [max(0, min(255, round(float(part)))) for part in parts[:3]]
        alpha = parse_opacity(parts[3])
    except ValueError:
        return None
    if alpha is None:
        return None
    return "#" + "".join(f"{channel:02X}" for channel in channels), alpha


def _multiply_named_opacity(elem: ET.Element, name: str, multiplier: float) -> None:
    current = parse_opacity(elem.get(name))
    elem.set(name, format_opacity((current if current is not None else 1.0) * multiplier))


def _repair_image_opacity(root: ET.Element, stats: RepairStats, svg_file: Path) -> bool:
    background = _infer_background_fill(root)
    changed = False
    parent_map = {child: parent for parent in root.iter() for child in list(parent)}
    images = [element for element in root.iter() if local_name(element.tag) == "image"]
    for image_index, image in enumerate(images, start=1):
        style = parse_style(image.get("style"))
        raw_opacity = image.get("opacity") if image.get("opacity") is not None else style.get("opacity")
        opacity = parse_opacity(raw_opacity)
        if opacity is None:
            continue
        if background is None and opacity < 1.0:
            stats.warnings.append(
                f"{svg_file.name}: image opacity left for AI repair because no solid slide background was found"
            )
            continue
        image.attrib.pop("opacity", None)
        if "opacity" in style:
            del style["opacity"]
            write_style(image, style)
        changed = True
        if opacity >= 1.0:
            continue
        parent = parent_map.get(image)
        if parent is None:
            continue
        overlay = ET.Element(_qualified_tag(root, "rect"))
        view_box = root.get("viewBox", "0 0 0 0").replace(",", " ").split()
        defaults = {
            "x": "0",
            "y": "0",
            "width": view_box[2] if len(view_box) == 4 else "0",
            "height": view_box[3] if len(view_box) == 4 else "0",
        }
        for name, default in defaults.items():
            overlay.set(name, image.get(name, default))
        for name in ("transform", "clip-path"):
            if image.get(name):
                overlay.set(name, image.get(name, ""))
        overlay.set("fill", background or "#000000")
        overlay.set("fill-opacity", format_opacity(1.0 - opacity))
        overlay.set("data-pptmaster-repair", f"image-opacity-{image_index}")
        siblings = list(parent)
        parent.insert(siblings.index(image) + 1, overlay)
        stats.image_opacity_overlays += 1
    return changed


def _infer_background_fill(root: ET.Element) -> str | None:
    view_box = root.get("viewBox", "").replace(",", " ").split()
    if len(view_box) != 4:
        return None
    width, height = view_box[2], view_box[3]
    for elem in root.iter():
        if local_name(elem.tag) != "rect":
            continue
        if elem.get("x", "0") not in {"0", "0.0"} or elem.get("y", "0") not in {"0", "0.0"}:
            continue
        if elem.get("width") not in {width, "100%"} or elem.get("height") not in {height, "100%"}:
            continue
        fill = parse_style(elem.get("style")).get("fill") or elem.get("fill")
        if fill and fill.lower() != "none" and not fill.lower().startswith("url("):
            return fill
    return None


def _qualified_tag(root: ET.Element, name: str) -> str:
    if root.tag.startswith("{"):
        namespace = root.tag.split("}", 1)[0][1:]
        return f"{{{namespace}}}{name}"
    return name


def repair_project(project_path: Path) -> RepairStats:
    stats = RepairStats()
    svg_dir = project_path / "svg_output"
    if not svg_dir.exists() or not svg_dir.is_dir():
        stats.warnings.append(f"svg_output directory does not exist: {svg_dir}")
        return stats

    for svg_file in sorted(svg_dir.glob("*.svg")):
        stats.files_seen += 1
        if repair_svg_file(svg_file, stats):
            stats.files_changed += 1
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Repair SVG files for PPT native export")
    parser.add_argument("project_path", help="PPT Master project directory")
    parser.add_argument("--json", action="store_true", help="print machine-readable summary")
    args = parser.parse_args()

    project_path = Path(args.project_path)
    stats = repair_project(project_path)
    payload = stats.to_dict()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print("PPT Master - SVG Repair Tool")
        print(f"Project path: {project_path}")
        print(f"Files seen: {stats.files_seen}")
        print(f"Files changed: {stats.files_changed}")
        print(f"Group opacity removed: {stats.group_opacity_removed}")
        print(f"Element opacity expanded: {stats.element_opacity_expanded}")
        print(f"Opacity targets updated: {stats.opacity_targets_updated}")
        for warning in stats.warnings:
            print(f"[WARN] {warning}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
