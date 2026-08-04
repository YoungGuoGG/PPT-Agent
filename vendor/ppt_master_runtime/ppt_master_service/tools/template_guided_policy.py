from __future__ import annotations

import hashlib
import json
import re
import uuid
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from typing import Any

from .template_guidance import resolve_page_reference


_VISIBLE_ATOMS = {
    "rect",
    "line",
    "circle",
    "ellipse",
    "polygon",
    "polyline",
    "path",
    "image",
    "use",
}
_SIGNATURE_ATTRIBUTES = (
    "x",
    "y",
    "x1",
    "y1",
    "x2",
    "y2",
    "cx",
    "cy",
    "r",
    "rx",
    "ry",
    "width",
    "height",
    "points",
    "d",
    "fill",
    "stroke",
    "stroke-width",
    "fill-opacity",
    "stroke-opacity",
    "href",
)
_NUMBER_RE = re.compile(r"-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")
_SLIDE_RE = re.compile(r"slide_0*([1-9]\d*)\.svg$", flags=re.I)


def load_template_context(project: Path) -> dict[str, Any]:
    """Load and validate the imported template context for a project."""
    project = project.expanduser().resolve()
    path = project / "templates" / "template_context.json"
    if not path.is_file():
        raise FileNotFoundError(
            f"template context does not exist: {path}; import the source PPTX first"
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != "template_guidance.v1":
        raise ValueError(
            "template context schema must be 'template_guidance.v1'; re-import the source PPTX"
        )
    canvas = payload.get("canvas")
    if not isinstance(canvas, dict) or int(canvas.get("width") or 0) <= 0 or int(
        canvas.get("height") or 0
    ) <= 0:
        raise ValueError(
            "template context canvas must contain positive width and height; re-import the source PPTX"
        )
    theme = payload.get("theme")
    if not isinstance(theme, dict) or not isinstance(theme.get("colors"), dict) or not isinstance(
        theme.get("fonts"), dict
    ):
        raise ValueError(
            "template context theme must contain colors and fonts; re-import the source PPTX"
        )
    roles = payload.get("roles")
    if not isinstance(roles, dict):
        raise ValueError("template context must contain role mappings; re-import the source PPTX")
    for role in ("cover", "content", "ending"):
        record = roles.get(role)
        if not isinstance(record, dict):
            raise ValueError(
                f"template context is missing role '{role}'; re-import the source PPTX"
            )
        svg_path = Path(str(record.get("svg_path") or "")).expanduser().resolve()
        if not svg_path.is_file():
            raise ValueError(
                f"template context role '{role}' has no readable svg_path at {svg_path}; "
                "re-import the source PPTX and verify templates/source_import/manifest.json"
            )
        svg = svg_path.read_text(encoding="utf-8", errors="replace")
        if "<svg" not in svg or "</svg>" not in svg:
            raise ValueError(
                f"template context role '{role}' does not reference a complete SVG at {svg_path}; "
                "re-import the source PPTX"
            )
        area = record.get("content_area")
        if not isinstance(area, dict) or int(area.get("width") or 0) <= 0 or int(
            area.get("height") or 0
        ) <= 0:
            raise ValueError(
                f"template context role '{role}' has an invalid content_area; re-import the source PPTX"
            )
        if not record.get("behavior") or not record.get("layout_policy"):
            raise ValueError(
                f"template context role '{role}' must define behavior and layout_policy; "
                "re-import the source PPTX"
            )
    return payload


def build_page_bindings(
    context: dict[str, Any], page_count: int
) -> dict[str, dict[str, Any]]:
    """Build deterministic cover/content/ending bindings for an output deck."""
    if page_count < 1:
        raise ValueError("page_count must be at least 1")
    bindings: dict[str, dict[str, Any]] = {}
    for page in range(1, page_count + 1):
        reference = resolve_page_reference(context, page, page_count)
        svg_path = Path(str(reference["svg_path"])).expanduser().resolve()
        if not svg_path.is_file():
            raise FileNotFoundError(
                f"template binding page {page} references missing SVG: {svg_path}; re-import the source PPTX"
            )
        bindings[str(page)] = {
            "page": page,
            "role": reference["role"],
            "behavior": reference["behavior"],
            "layout_policy": reference["layout_policy"],
            "content_area": dict(reference["content_area"]),
            "svg_path": str(svg_path),
            "reference_digest": "sha256:" + hashlib.sha256(svg_path.read_bytes()).hexdigest(),
        }
    return bindings


def write_template_bindings(project: Path, page_count: int) -> Path:
    """Persist the immutable imported-template page binding artifact atomically."""
    project = project.expanduser().resolve()
    context = load_template_context(project)
    payload = {
        "schema": "template_bindings.v1",
        "page_count": page_count,
        "context_digest": _json_digest(context),
        "bindings": build_page_bindings(context, page_count),
    }
    path = project / "templates" / "template_bindings.json"
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
    return path


def check_template_page_findings(
    project_path: str, svg_path: str
) -> list[dict[str, Any]]:
    """Return deterministic template-conformance findings for one generated page."""
    project = Path(project_path).expanduser().resolve()
    output = Path(svg_path).expanduser().resolve()
    page = _page_number(output)
    bindings = _load_bindings(project)
    binding = bindings.get(str(page))
    if not isinstance(binding, dict):
        return [
            _finding(
                output,
                page,
                "unknown",
                {"field": "template_bindings", "page": page},
                {"available_pages": sorted(bindings)},
                f"page {page} has no template binding",
                "regenerate templates/template_bindings.json for the confirmed page count",
                "{\"page_count\": 3, \"bindings\": {\"1\": {\"role\": \"cover\"}}}",
            )
        ]
    role = str(binding["role"])
    reference_path = Path(str(binding["svg_path"])).expanduser().resolve()
    try:
        reference_root = ET.fromstring(reference_path.read_text(encoding="utf-8"))
        output_root = ET.fromstring(output.read_text(encoding="utf-8"))
    except (OSError, ET.ParseError) as exc:
        return [
            _finding(
                output,
                page,
                role,
                {"field": "svg"},
                {"error": str(exc)},
                "generated or reference SVG is not parseable",
                "return one complete parseable SVG document and preserve the current page contract",
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720"></svg>',
            )
        ]
    findings: list[dict[str, Any]] = []
    context = load_template_context(project)
    canvas = context["canvas"]
    expected_view_box = f"0 0 {int(canvas['width'])} {int(canvas['height'])}"
    actual_view_box = " ".join(str(output_root.get("viewBox") or "").split())
    if actual_view_box != expected_view_box:
        findings.append(
            _finding(
                output,
                page,
                role,
                {"field": "viewBox"},
                {"expected": expected_view_box, "actual": actual_view_box},
                "generated SVG viewBox does not match the imported template canvas",
                f"set viewBox exactly to '{expected_view_box}' without changing confirmed content",
                f'<svg viewBox="{expected_view_box}">',
            )
        )
    content_area = binding["content_area"]
    reference_atoms = _required_atoms(
        reference_root,
        behavior=str(binding["behavior"]),
        content_area=content_area,
        canvas=canvas,
    )
    output_atoms = Counter(_signature(element) for element in _visible_atoms(output_root))
    missing = reference_atoms - output_atoms
    reference_examples = {
        _signature(element): ET.tostring(element, encoding="unicode", short_empty_elements=True)
        for element in _visible_atoms(reference_root)
    }
    for signature, count in missing.items():
        findings.append(
            _finding(
                output,
                page,
                role,
                {
                    "template_role": role,
                    "element": signature.split("|", 1)[0],
                    "signature": signature,
                },
                {"missing_count": count, "reference_svg": str(reference_path)},
                "required template chrome is missing from the generated page",
                "restore the missing template atom at its imported geometry and preserve all unrelated content",
                reference_examples.get(signature, signature),
            )
        )
    output_text = {_normalize_text("".join(element.itertext())) for element in _text_atoms(output_root)}
    for element in _text_atoms(reference_root):
        sample = _normalize_text("".join(element.itertext()))
        if not sample or not _text_inside_content_area(element, content_area):
            continue
        if sample in output_text:
            findings.append(
                _finding(
                    output,
                    page,
                    role,
                    {"template_role": role, "element": "text"},
                    {"text": sample, "reference_svg": str(reference_path)},
                    "audience-facing template sample text remains inside the editable content area",
                    "replace the sample text with source-grounded content while preserving locked geometry and chrome",
                    '<text x="100" y="220">NEW SOURCE-GROUNDED CONTENT</text>',
                )
            )
    return findings


def check_template_project_findings(project_path: str) -> list[dict[str, Any]]:
    """Return template findings for every bound page in a project."""
    project = Path(project_path).expanduser().resolve()
    bindings = _load_bindings(project)
    findings: list[dict[str, Any]] = []
    for page in sorted(int(value) for value in bindings):
        output = project / "svg_output" / f"slide_{page:02d}.svg"
        if not output.is_file():
            findings.append(
                _finding(
                    output,
                    page,
                    str(bindings[str(page)].get("role") or "unknown"),
                    {"field": "svg_output"},
                    {"expected_path": str(output)},
                    "bound template page has no generated SVG",
                    "generate this page after reading its current spec_lock and bound reference SVG",
                    f"slide_{page:02d}.svg",
                )
            )
            continue
        findings.extend(check_template_page_findings(str(project), str(output)))
    return findings


def _load_bindings(project: Path) -> dict[str, dict[str, Any]]:
    path = project / "templates" / "template_bindings.json"
    if not path.is_file():
        raise FileNotFoundError(
            f"template bindings do not exist: {path}; run template-guided prepare again"
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != "template_bindings.v1" or not isinstance(
        payload.get("bindings"), dict
    ):
        raise ValueError(
            f"template bindings are invalid at {path}; run template-guided prepare again"
        )
    return payload["bindings"]


def _required_atoms(
    root: ET.Element,
    *,
    behavior: str,
    content_area: dict[str, Any],
    canvas: dict[str, Any],
) -> Counter[str]:
    required: Counter[str] = Counter()
    for element in _visible_atoms(root):
        bounds = _bounds(element)
        if behavior == "literal" or bounds is None or _covers_canvas(bounds, canvas) or _outside(
            bounds, content_area
        ):
            required[_signature(element)] += 1
    return required


def _visible_atoms(root: ET.Element) -> list[ET.Element]:
    return [element for element in root.iter() if _local_name(element.tag) in _VISIBLE_ATOMS]


def _text_atoms(root: ET.Element) -> list[ET.Element]:
    return [element for element in root.iter() if _local_name(element.tag) == "text"]


def _signature(element: ET.Element) -> str:
    parts = [_local_name(element.tag)]
    for name in _SIGNATURE_ATTRIBUTES:
        value = element.get(name)
        if value is None and name == "href":
            value = element.get("{http://www.w3.org/1999/xlink}href")
        if value is None:
            continue
        parts.append(f"{name}={_normalize_attribute(name, value)}")
    return "|".join(parts)


def _normalize_attribute(name: str, value: str) -> str:
    value = " ".join(value.split())
    if name in {"fill", "stroke"} and value.startswith("#"):
        return value.upper()
    if name in {"points", "d"}:
        return re.sub(r"\s*,\s*", ",", value)
    numbers = _NUMBER_RE.fullmatch(value)
    if numbers:
        number = float(value)
        return str(int(number)) if number.is_integer() else f"{number:.6f}".rstrip("0").rstrip(".")
    return value


def _bounds(element: ET.Element) -> tuple[float, float, float, float] | None:
    tag = _local_name(element.tag)
    if tag in {"rect", "image", "use"}:
        x = _float(element.get("x"))
        y = _float(element.get("y"))
        width = _float(element.get("width"))
        height = _float(element.get("height"))
        return (x, y, x + width, y + height)
    if tag == "circle":
        cx, cy, radius = _float(element.get("cx")), _float(element.get("cy")), _float(element.get("r"))
        return (cx - radius, cy - radius, cx + radius, cy + radius)
    if tag == "ellipse":
        cx, cy = _float(element.get("cx")), _float(element.get("cy"))
        rx, ry = _float(element.get("rx")), _float(element.get("ry"))
        return (cx - rx, cy - ry, cx + rx, cy + ry)
    if tag == "line":
        x1, y1 = _float(element.get("x1")), _float(element.get("y1"))
        x2, y2 = _float(element.get("x2")), _float(element.get("y2"))
        return (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
    values = [float(value) for value in _NUMBER_RE.findall(element.get("points") or element.get("d") or "")]
    if len(values) >= 2:
        xs, ys = values[0::2], values[1::2]
        if xs and ys:
            return (min(xs), min(ys), max(xs), max(ys))
    return None


def _covers_canvas(bounds: tuple[float, float, float, float], canvas: dict[str, Any]) -> bool:
    x1, y1, x2, y2 = bounds
    return x1 <= 0 and y1 <= 0 and x2 >= float(canvas["width"]) and y2 >= float(canvas["height"])


def _outside(bounds: tuple[float, float, float, float], area: dict[str, Any]) -> bool:
    x1, y1, x2, y2 = bounds
    area_x1, area_y1 = float(area["x"]), float(area["y"])
    area_x2 = area_x1 + float(area["width"])
    area_y2 = area_y1 + float(area["height"])
    return x2 <= area_x1 or x1 >= area_x2 or y2 <= area_y1 or y1 >= area_y2


def _text_inside_content_area(element: ET.Element, area: dict[str, Any]) -> bool:
    x, y = _float(element.get("x")), _float(element.get("y"))
    return (
        float(area["x"]) <= x <= float(area["x"]) + float(area["width"])
        and float(area["y"]) <= y <= float(area["y"]) + float(area["height"])
    )


def _finding(
    path: Path,
    page: int,
    role: str,
    location: dict[str, Any],
    evidence: dict[str, Any],
    reason: str,
    required: str,
    example: str,
) -> dict[str, Any]:
    return {
        "file": path.name,
        "page": page,
        "location": {"template_role": role, **location},
        "evidence": evidence,
        "reason": reason,
        "correction": {
            "required": required,
            "preserve": [
                "confirmed facts",
                "spec_lock",
                "page number",
                "unrelated content and template anchors",
            ],
            "example": example,
            "recheck": "run generic and template page quality checks again",
        },
    }


def _page_number(path: Path) -> int:
    match = _SLIDE_RE.fullmatch(path.name)
    if match is None:
        raise ValueError(f"template quality requires slide_NN.svg, got: {path.name}")
    return int(match.group(1))


def _json_digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _float(value: str | None) -> float:
    try:
        return float(value or 0)
    except ValueError:
        return 0.0


def _normalize_text(value: str) -> str:
    return " ".join(value.split())


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]
