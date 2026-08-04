from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
from typing import Any, Callable, Iterable, Sequence
import xml.etree.ElementTree as ET

from ..conformance import (
    ErrorKind,
    SuggestedAction,
    WorkflowError,
    WorkflowExecutionError,
)
from ..llm_client import FailureKind, call_llm_json, classify_llm_failure


MIRROR_SYSTEM_PROMPT = """You plan safe text replacement for an existing SVG presentation.
Return one JSON object only. Use only source_svg and editable slot_id values from the roster.
Never return SVG, XML, coordinates, styles, or new elements. Preserve each slot's exact line count."""
URL_RE = re.compile(r"https?://|www\.", flags=re.I)
LEGAL_MARKERS = ("confidential", "copyright", "all rights reserved")


@dataclass(frozen=True)
class ResolvedMirrorTemplate:
    template_id: str
    root: Path
    svg_pages: tuple[Path, ...]
    metadata: dict[str, Any]


@dataclass(frozen=True)
class MirrorTextSlot:
    slot_id: str
    source_svg: str
    element_index: int
    original_text: str
    original_lines: tuple[str, ...]
    capacities: tuple[int, ...]
    editable: bool
    lock_reason: str | None
    x: str | None
    y: str | None
    font_size: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "MirrorTextSlot":
        data = dict(payload)
        data["original_lines"] = tuple(data.get("original_lines") or ())
        data["capacities"] = tuple(int(item) for item in data.get("capacities") or ())
        return cls(**data)


@dataclass(frozen=True)
class MirrorRosterPage:
    source_svg: str
    source_path: str
    width: float
    height: float
    fingerprint: str
    slots: tuple[MirrorTextSlot, ...]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["slots"] = [slot.to_dict() for slot in self.slots]
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "MirrorRosterPage":
        data = dict(payload)
        data["slots"] = tuple(MirrorTextSlot.from_dict(item) for item in data.get("slots") or [])
        return cls(**data)


@dataclass(frozen=True)
class MirrorRoster:
    pages: tuple[MirrorRosterPage, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "pptmaster.mirror-roster.v1",
            "pages": [page.to_dict() for page in self.pages],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "MirrorRoster":
        if payload.get("schema") != "pptmaster.mirror-roster.v1":
            raise ValueError(f"unsupported mirror roster schema: {payload.get('schema')}")
        pages = payload.get("pages")
        if not isinstance(pages, list):
            raise ValueError("mirror roster pages must be a list")
        return cls(tuple(MirrorRosterPage.from_dict(item) for item in pages))


@dataclass(frozen=True)
class MirrorPlanValidation:
    passed: bool
    issues: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {"passed": self.passed, "issues": list(self.issues)}


def resolve_deck_template(
    template_id: str,
    *,
    metadata_dir: Path,
    template_root: Path,
) -> ResolvedMirrorTemplate:
    metadata, metadata_path = _find_metadata(template_id, metadata_dir)
    if str(metadata.get("kind") or "") != "deck":
        raise _workflow_error(
            ErrorKind.PROTOCOL,
            "mirror.template.kind",
            "load_template",
            f"template {template_id} has kind {metadata.get('kind')}; expected deck",
            {"template_id": template_id, "kind": metadata.get("kind")},
        )

    registered_raw = Path(str(metadata.get("source_path") or metadata.get("path") or ""))
    registered = registered_raw if registered_raw.is_absolute() else metadata_path.parent / registered_raw
    fallback_name = template_id.removeprefix("deck_")
    candidates = (registered, template_root / "decks" / fallback_name)
    approved_root = template_root.resolve()
    root = next(
        (
            candidate.resolve()
            for candidate in candidates
            if candidate.exists()
            and candidate.is_dir()
            and _is_within(candidate.resolve(), approved_root)
        ),
        None,
    )
    if root is None:
        raise _workflow_error(
            ErrorKind.ARTIFACT,
            "mirror.template.path",
            "load_template",
            f"registered deck path is missing or outside the approved template root: {template_id}",
            {"template_id": template_id},
        )
    pages = tuple(sorted(root.glob("*.svg")))
    if not pages:
        raise _workflow_error(
            ErrorKind.ARTIFACT,
            "mirror.template.roster_empty",
            "load_template",
            f"deck {template_id} has no root SVG roster",
            {"template_id": template_id, "path": str(root)},
        )
    return ResolvedMirrorTemplate(template_id, root, pages, metadata)


def analyze_mirror_roster(svg_pages: Iterable[Path]) -> MirrorRoster:
    parsed: list[tuple[Path, ET.Element, float, float, list[dict[str, Any]]]] = []
    text_counts: Counter[str] = Counter()
    for svg_path in svg_pages:
        root = ET.parse(svg_path).getroot()
        width, height = _canvas_size(root)
        raw_slots: list[dict[str, Any]] = []
        for index, element in enumerate(_text_elements(root), start=1):
            line_nodes = [node for node in list(element) if _local_name(node.tag) == "tspan"]
            lines = tuple((node.text or "").strip() for node in line_nodes) if line_nodes else ((element.text or "").strip(),)
            original = "\n".join(lines).strip()
            if not original:
                continue
            normalized = _normalize_text(original)
            text_counts[normalized] += 1
            raw_slots.append(
                {
                    "element_index": index,
                    "original_text": original,
                    "original_lines": lines,
                    "x": element.get("x"),
                    "y": element.get("y"),
                    "font_size": element.get("font-size") or element.get("style"),
                    "normalized": normalized,
                }
            )
        parsed.append((svg_path.resolve(), root, width, height, raw_slots))

    pages: list[MirrorRosterPage] = []
    for svg_path, _root, width, height, raw_slots in parsed:
        slots: list[MirrorTextSlot] = []
        for item in raw_slots:
            reason = _lock_reason(item, text_counts, height)
            original_lines = tuple(str(line) for line in item["original_lines"])
            slots.append(
                MirrorTextSlot(
                    slot_id=f"{svg_path.stem}:text:{int(item['element_index']):03d}",
                    source_svg=svg_path.name,
                    element_index=int(item["element_index"]),
                    original_text=str(item["original_text"]),
                    original_lines=original_lines,
                    capacities=tuple(max(8, math.ceil(max(1, len(line)) * 1.25)) for line in original_lines),
                    editable=reason is None,
                    lock_reason=reason,
                    x=item["x"],
                    y=item["y"],
                    font_size=item["font_size"],
                )
            )
        pages.append(
            MirrorRosterPage(
                source_svg=svg_path.name,
                source_path=str(svg_path),
                width=width,
                height=height,
                fingerprint=structural_fingerprint(svg_path),
                slots=tuple(slots),
            )
        )
    return MirrorRoster(tuple(pages))


def write_mirror_roster(path: Path, roster: MirrorRoster) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(roster.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def structural_fingerprint(svg_path: Path) -> str:
    root = ET.parse(svg_path).getroot()
    payload = [
        (_local_name(node.tag), sorted((str(key), str(value)) for key, value in node.attrib.items()))
        for node in root.iter()
    ]
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def validate_mirror_plan(
    plan: dict[str, Any],
    roster: MirrorRoster,
    expected_pages: int,
) -> MirrorPlanValidation:
    issues: list[dict[str, Any]] = []
    raw_pages = plan.get("pages") if isinstance(plan, dict) else None
    pages = raw_pages if isinstance(raw_pages, list) else []
    if len(pages) != expected_pages:
        issues.append(
            _issue(
                "mirror.plan.page_count",
                None,
                None,
                len(pages),
                f"exactly {expected_pages} pages",
            )
        )

    roster_pages = {page.source_svg: page for page in roster.pages}
    seen_indexes: set[int] = set()
    for offset, page_plan in enumerate(pages, start=1):
        if not isinstance(page_plan, dict):
            issues.append(_issue("mirror.plan.page_object", offset, None, type(page_plan).__name__, "a page object"))
            continue
        output_index = _int_or_zero(page_plan.get("output_index"))
        if output_index < 1 or output_index > expected_pages or output_index in seen_indexes:
            issues.append(_issue("mirror.plan.output_index", offset, None, output_index, f"a unique integer from 1 to {expected_pages}"))
        seen_indexes.add(output_index)
        source_svg = str(page_plan.get("source_svg") or "")
        roster_page = roster_pages.get(source_svg)
        if roster_page is None:
            issues.append(_issue("mirror.plan.source_svg", output_index or offset, None, source_svg, "a source_svg from the approved roster"))
            continue
        slot_map = {slot.slot_id: slot for slot in roster_page.slots}
        replacements = page_plan.get("replacements")
        if not isinstance(replacements, list):
            issues.append(_issue("mirror.plan.replacements", output_index, None, type(replacements).__name__, "a list of replacement objects"))
            continue
        seen_slots: set[str] = set()
        for replacement in replacements:
            if not isinstance(replacement, dict):
                issues.append(_issue("mirror.plan.replacement_object", output_index, None, type(replacement).__name__, "a replacement object"))
                continue
            slot_id = str(replacement.get("slot_id") or "")
            slot = slot_map.get(slot_id)
            if slot is None or slot_id in seen_slots:
                issues.append(_issue("mirror.plan.slot", output_index, slot_id, slot_id, "one unique editable slot_id on source_svg"))
                continue
            seen_slots.add(slot_id)
            if not slot.editable:
                issues.append(_issue("mirror.plan.slot_locked", output_index, slot_id, slot.lock_reason, "an editable slot"))
                continue
            lines = replacement.get("lines")
            if not isinstance(lines, list) or not all(isinstance(line, str) for line in lines):
                issues.append(_issue("mirror.plan.lines", output_index, slot_id, lines, f"exactly {len(slot.original_lines)} string lines"))
                continue
            if len(lines) != len(slot.original_lines):
                issues.append(_issue("mirror.plan.line_count", output_index, slot_id, len(lines), f"exactly {len(slot.original_lines)} lines"))
                continue
            for line_index, (line, capacity) in enumerate(zip(lines, slot.capacities), start=1):
                if "<" in line or ">" in line:
                    issues.append(_issue("mirror.plan.markup", output_index, slot_id, line, "plain text without angle-bracket markup"))
                if len(line) > capacity:
                    issues.append(_issue("mirror.plan.capacity", output_index, slot_id, len(line), f"line {line_index} at most {capacity} characters"))
    return MirrorPlanValidation(not issues, tuple(issues))


def plan_mirror_content(
    source_markdown: str,
    roster: MirrorRoster,
    expected_pages: int,
    *,
    call_json: Callable[[str, str | None], dict[str, Any]] = call_llm_json,
) -> tuple[dict[str, Any], MirrorPlanValidation]:
    prompt = _build_mirror_prompt(source_markdown, roster, expected_pages)
    correction = ""
    last_payload: dict[str, Any] = {}
    report = MirrorPlanValidation(False, (_issue("mirror.plan.missing", None, None, None, "a complete plan"),))
    for attempt in range(1, 4):
        try:
            last_payload = call_json(prompt + correction, MIRROR_SYSTEM_PROMPT)
        except Exception as exc:
            kind = classify_llm_failure(exc)
            error_kind = ErrorKind.TRANSPORT if kind is FailureKind.TRANSPORT else ErrorKind.PROTOCOL
            raise WorkflowExecutionError(
                WorkflowError(
                    kind=error_kind,
                    code="mirror.provider.transport" if error_kind is ErrorKind.TRANSPORT else "mirror.provider.protocol",
                    step="map_roster",
                    message=f"mirror planning call failed on attempt {attempt}: {exc}",
                    location={"attempt": attempt},
                    retryable=error_kind is ErrorKind.TRANSPORT,
                    suggested_action=(SuggestedAction.RETRY if error_kind is ErrorKind.TRANSPORT else SuggestedAction.REGENERATE_STEP),
                    attempt=attempt,
                )
            ) from exc
        report = validate_mirror_plan(last_payload, roster, expected_pages)
        if report.passed:
            return last_payload, report
        correction = _build_mirror_correction(report.issues)
    return last_payload, report


def apply_mirror_plan(
    plan: dict[str, Any],
    roster: MirrorRoster,
    project_path: Path,
) -> dict[str, Any]:
    svg_dir = project_path / "svg_output"
    notes_dir = project_path / "notes"
    svg_dir.mkdir(parents=True, exist_ok=True)
    notes_dir.mkdir(parents=True, exist_ok=True)
    pages = {page.source_svg: page for page in roster.pages}
    generated: list[str] = []
    note_sections: list[str] = []
    fingerprints: dict[str, str] = {}
    for page_plan in sorted(plan["pages"], key=lambda item: int(item["output_index"])):
        output_index = int(page_plan["output_index"])
        roster_page = pages[str(page_plan["source_svg"])]
        source = Path(roster_page.source_path)
        target = svg_dir / f"slide_{output_index:02d}.svg"
        shutil.copy2(source, target)
        root = ET.parse(target).getroot()
        text_elements = list(_text_elements(root))
        slots = {slot.slot_id: slot for slot in roster_page.slots}
        for replacement in page_plan.get("replacements") or []:
            slot = slots[str(replacement["slot_id"])]
            element = text_elements[slot.element_index - 1]
            tspans = [node for node in list(element) if _local_name(node.tag) == "tspan"]
            lines = list(replacement["lines"])
            if tspans:
                for node, line in zip(tspans, lines):
                    node.text = line
            else:
                element.text = lines[0]
        ET.ElementTree(root).write(target, encoding="unicode", xml_declaration=False)
        actual_fingerprint = structural_fingerprint(target)
        if actual_fingerprint != roster_page.fingerprint:
            target.unlink(missing_ok=True)
            raise _workflow_error(
                ErrorKind.QUALITY,
                "mirror.patch.structure_changed",
                "replace_content",
                f"non-text SVG structure changed while patching output page {output_index}",
                {"output_index": output_index, "source_svg": roster_page.source_svg},
            )
        generated.append(str(target))
        fingerprints[target.name] = actual_fingerprint
        note = str(page_plan.get("speaker_note") or "").strip()
        note_sections.append(f"# slide_{output_index:02d}\n\n{note}\n")
    notes_total = notes_dir / "total.md"
    notes_total.write_text("\n---\n\n".join(note_sections), encoding="utf-8")
    return {
        "svg_files": generated,
        "notes_total": str(notes_total),
        "fingerprints": fingerprints,
    }


def _find_metadata(template_id: str, metadata_dir: Path) -> tuple[dict[str, Any], Path]:
    if not metadata_dir.is_dir():
        raise _workflow_error(ErrorKind.ARTIFACT, "mirror.template.metadata", "load_template", f"template metadata directory does not exist: {metadata_dir}", {"template_id": template_id})
    for path in sorted(metadata_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        items = payload if isinstance(payload, list) else [payload]
        for item in items:
            if not isinstance(item, dict):
                continue
            identifiers = {str(item.get("template_id") or ""), str(item.get("id") or ""), path.stem}
            if template_id in identifiers:
                return item, path.resolve()
    raise _workflow_error(ErrorKind.ARTIFACT, "mirror.template.metadata", "load_template", f"mirror template metadata not found for template_id: {template_id}", {"template_id": template_id})


def _canvas_size(root: ET.Element) -> tuple[float, float]:
    view_box = (root.get("viewBox") or "").replace(",", " ").split()
    if len(view_box) == 4:
        try:
            return float(view_box[2]), float(view_box[3])
        except ValueError:
            pass
    return _number(root.get("width"), 1280.0), _number(root.get("height"), 720.0)


def _lock_reason(item: dict[str, Any], counts: Counter[str], height: float) -> str | None:
    normalized = str(item["normalized"])
    if counts[normalized] > 1:
        return "repeated_text"
    if URL_RE.search(str(item["original_text"])):
        return "url"
    lowered = str(item["original_text"]).lower()
    if any(marker in lowered for marker in LEGAL_MARKERS):
        return "legal_text"
    y = _number(item.get("y"), -1.0)
    if y >= height * 0.94:
        return "footer"
    return None


def _text_elements(root: ET.Element) -> Iterable[ET.Element]:
    return (element for element in root.iter() if _local_name(element.tag) == "text")


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _normalize_text(value: str) -> str:
    return " ".join(value.lower().split())


def _number(value: Any, default: float) -> float:
    try:
        return float(str(value).replace("px", ""))
    except (TypeError, ValueError):
        return default


def _int_or_zero(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _is_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _issue(
    code: str,
    page: int | None,
    slot_id: str | None,
    observed: Any,
    required: str,
) -> dict[str, Any]:
    return {
        "code": code,
        "page": page,
        "slot_id": slot_id,
        "observed": observed,
        "required": required,
    }


def _build_mirror_prompt(source_markdown: str, roster: MirrorRoster, expected_pages: int) -> str:
    return (
        f"Create a {expected_pages}-page mirror plan.\n\n"
        f"Source material:\n{source_markdown[:30000]}\n\n"
        "Editable roster:\n"
        + json.dumps(roster.to_dict(), ensure_ascii=False, indent=2)
        + "\n\nReturn JSON with top-level pages. Each page requires output_index, source_svg, replacements, and speaker_note.\n"
        'Every replacement MUST have exactly this shape: {"slot_id": "approved-slot-id", "lines": ["replacement line"]}.\n'
        'Use the field name "lines", never "text" or "content". The lines array length must equal original_lines length for that slot.'
    )


def _build_mirror_correction(issues: Sequence[dict[str, Any]]) -> str:
    instructions = "\n".join(_mirror_correction_instruction(issue) for issue in issues)
    return (
        "\n\nCORRECTION REQUIRED. Fix every item and return the complete JSON object again.\n"
        "Exact repair instructions:\n"
        + instructions
        + "\nValidation findings:\n"
        + json.dumps(list(issues), ensure_ascii=False, indent=2)
    )


def _mirror_correction_instruction(issue: dict[str, Any]) -> str:
    page = issue.get("page")
    slot_id = issue.get("slot_id")
    location = f"At page {page}"
    if slot_id:
        location += f", slot {slot_id}"
    code = str(issue.get("code") or "")
    required = str(issue.get("required") or "the required value")
    if code == "mirror.plan.lines":
        return (
            f'- {location}: Use the field name "lines" with {required}, for example '
            '"lines": ["replacement line"]. Remove fields named "text" or "content".'
        )
    if code == "mirror.plan.line_count":
        return f'- {location}: Change only the "lines" array so it contains {required}.'
    if code == "mirror.plan.capacity":
        return f"- {location}: Shorten the reported line to {required}; keep the same slot_id and line count."
    if code in {"mirror.plan.slot", "mirror.plan.slot_locked"}:
        return f"- {location}: Replace it with {required} from the same source_svg roster."
    return f"- {location}: change {code or 'this value'} to {required}."


def _workflow_error(
    kind: ErrorKind,
    code: str,
    step: str,
    message: str,
    location: dict[str, Any],
) -> WorkflowExecutionError:
    return WorkflowExecutionError(
        WorkflowError(
            kind=kind,
            code=code,
            step=step,
            message=message,
            location=location,
            retryable=False,
            suggested_action=SuggestedAction.STOP,
        )
    )
