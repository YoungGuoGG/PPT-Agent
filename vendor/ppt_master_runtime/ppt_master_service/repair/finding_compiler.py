from __future__ import annotations

import re
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET

from .models import ElementLocation, QualityFinding
from .recipes import REPAIR_RECIPES


ERROR_PATTERNS = (
    (re.compile(r"image opacity", re.I), "SVG_IMAGE_OPACITY_UNSUPPORTED"),
    (re.compile(r"group opacity|<g[^>]*opacity", re.I), "SVG_GROUP_OPACITY_UNSUPPORTED"),
    (re.compile(r"rgba\(", re.I), "SVG_RGBA_COLOR"),
    (re.compile(r"forbidden <(?:mask|filter)", re.I), "SVG_MASK_OR_FILTER_UNSUPPORTED"),
    (re.compile(r"image file not found|asset.*not found|unresolved.*image", re.I), "ASSET_PATH_INVALID"),
    (re.compile(r"icon.*(?:missing|unknown|unresolved)", re.I), "ICON_NAME_UNKNOWN"),
    (re.compile(r"text.*overflow|overflow.*text", re.I), "TEXT_OVERFLOW"),
    (re.compile(r"out of bounds|outside (?:the )?canvas|safe margin", re.I), "ELEMENT_OUT_OF_BOUNDS"),
    (re.compile(r"overlap", re.I), "ELEMENT_OVERLAP"),
    (re.compile(r"spec_lock drift|outside spec_lock", re.I), "SVG_SPEC_COLOR_DRIFT"),
    (re.compile(r"viewBox", re.I), "SVG_VIEWBOX_INVALID"),
    (re.compile(r"xml|not well-formed|parse", re.I), "SVG_XML_INVALID"),
)


def compile_findings(project: Path, report: dict[str, Any]) -> list[QualityFinding]:
    findings: list[QualityFinding] = []
    for result in report.get("results") or []:
        if not isinstance(result, dict):
            continue
        filename = Path(str(result.get("file") or "")).name
        svg_path = _resolve_svg_path(project, filename, result.get("path"))
        for raw_error in result.get("errors") or []:
            code = _classify_error(str(raw_error))
            if not code:
                continue
            recipe = REPAIR_RECIPES[code]
            findings.append(
                QualityFinding(
                    code=code,
                    severity="error",
                    file=filename,
                    message=recipe.diagnosis,
                    location=_locate(svg_path, code),
                    evidence={"checker_message": str(raw_error)},
                )
            )
    return findings


def _resolve_svg_path(project: Path, filename: str, reported: object) -> Path:
    candidate = Path(str(reported)) if reported else None
    if candidate is not None and candidate.exists() and candidate.is_file():
        return candidate
    return project / "svg_output" / filename


def _classify_error(message: str) -> str:
    for pattern, code in ERROR_PATTERNS:
        if pattern.search(message):
            return code
    return ""


def _locate(svg_path: Path, code: str) -> ElementLocation:
    if not svg_path.exists():
        return ElementLocation()
    try:
        root = ET.fromstring(svg_path.read_text(encoding="utf-8", errors="replace"))
    except ET.ParseError:
        return ElementLocation(tag="svg", snippet="XML parse failed")

    parent_map = {child: parent for parent in root.iter() for child in list(parent)}
    candidates = list(root.iter())
    if code == "SVG_IMAGE_OPACITY_UNSUPPORTED":
        candidates = [element for element in root.iter() if _local_name(element.tag) == "image" and _has_opacity(element)]
    elif code == "SVG_GROUP_OPACITY_UNSUPPORTED":
        candidates = [element for element in root.iter() if _local_name(element.tag) == "g" and _has_opacity(element)]
    elif code == "SVG_MASK_OR_FILTER_UNSUPPORTED":
        candidates = [element for element in root.iter() if _local_name(element.tag) in {"mask", "filter"}]
    elif code == "SVG_RGBA_COLOR":
        candidates = [element for element in root.iter() if "rgba(" in str(element.attrib).lower()]

    if not candidates:
        return ElementLocation()
    element = candidates[0]
    tag = _local_name(element.tag)
    same_tag = [item for item in root.iter() if _local_name(item.tag) == tag]
    index = same_tag.index(element) + 1
    group = parent_map.get(element)
    while group is not None and _local_name(group.tag) != "g":
        group = parent_map.get(group)
    element_id = element.get("id", "")
    group_id = group.get("id", "") if group is not None else ""
    selector = f"#{element_id}" if element_id else f"{tag}[{index}]"
    return ElementLocation(
        element_id=element_id,
        group_id=group_id,
        tag=tag,
        index=index,
        selector=selector,
        snippet=ET.tostring(element, encoding="unicode")[:2000],
        attributes={str(key): str(value) for key, value in element.attrib.items()},
        related_elements=_related_references(root, element),
    )


def _related_references(root: ET.Element, definition: ET.Element) -> list[dict[str, Any]]:
    definition_id = definition.get("id", "")
    if not definition_id:
        return []

    reference = f"#{definition_id}"
    related: list[dict[str, Any]] = []
    for element in root.iter():
        if element is definition:
            continue
        tag = _local_name(element.tag)
        same_tag = [item for item in root.iter() if _local_name(item.tag) == tag]
        for attribute, value in element.attrib.items():
            if value != reference and f"url({reference})" not in value:
                continue
            element_id = element.get("id", "")
            index = same_tag.index(element) + 1
            related.append(
                {
                    "selector": f"#{element_id}" if element_id else f"{tag}[{index}]",
                    "element_id": element_id,
                    "tag": tag,
                    "index": index,
                    "attribute": _local_name(attribute),
                    "value": value,
                }
            )
    return related


def _has_opacity(element: ET.Element) -> bool:
    if "opacity" in element.attrib:
        return True
    return bool(re.search(r"(?:^|;)\s*opacity\s*:", element.get("style", ""), flags=re.I))


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]
