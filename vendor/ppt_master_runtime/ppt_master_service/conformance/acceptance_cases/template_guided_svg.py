from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Callable
from unittest import mock
import uuid
import xml.etree.ElementTree as ET
import zipfile

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.util import Inches, Pt

from ... import llm_runtime
from ...agent_phase_runner import AgentPhaseRunner
from ...job_store import JobStore
from ...phase_runner_router import PhaseRunnerRouter
from ...provider_config import initialize_provider_environment
from ...runtime import Runtime
from ...schemas import InteractionResponseRequest
from ...tools.project_tools import ToolResult
from ...tools.template_guided_policy import (
    check_template_project_findings,
    load_template_context,
)
from ...workflows import get_workflow_class
from ..acceptance import (
    AcceptanceArtifact,
    AcceptanceEvidence,
    sha256_file,
    sha256_tree,
)
from ..acceptance_runner import (
    _OfflinePreview,
    _event_index,
    _offline_document_strategy,
    _offline_export_project,
    _pptx_slide_count,
    _require_provider_configuration,
)
from ..registry import REGISTRY_SCHEMA, REPO_ROOT, get_workflow_definition


WORKFLOW_ID = "template_guided_svg"
FIXTURE = REPO_ROOT / "ppt_master_service" / "fixtures" / "workflows" / WORKFLOW_ID
SVG_NAMESPACE = "http://www.w3.org/2000/svg"
REQUIRED_GATES = (
    "source_ready",
    "template_pptx_valid",
    "template_import_complete",
    "template_context_valid",
    "design_confirmed",
    "design_spec_canonical",
    "spec_lock_parseable",
    "template_design_locked",
    "template_role_mapping",
    "preview_healthy",
    "template_references_preloaded",
    "sequential_pages",
    "spec_lock_read_per_page",
    "template_chrome_preserved",
    "template_sample_content_replaced",
    "svg_quality",
    "notes_after_quality",
    "postprocess_order",
    "pptx_readable",
    "pptx_page_count",
)


def run_case(
    *, provider: bool, output_root: Path | None = None
) -> AcceptanceEvidence:
    settings = initialize_provider_environment(REPO_ROOT)
    if provider:
        _require_provider_configuration(WORKFLOW_ID, settings)
    if not FIXTURE.is_dir():
        raise FileNotFoundError(f"acceptance fixture does not exist: {FIXTURE}")

    root = _resolve_output_root(output_root)
    run_root = root / f"{WORKFLOW_ID}-{uuid.uuid4().hex[:8]}"
    run_root.mkdir(parents=True, exist_ok=False)
    started = datetime.now(timezone.utc)
    stages, artifacts = _run_fixture(run_root, provider=provider)
    gates = _quality_gates(stages)
    failed = [name for name in REQUIRED_GATES if not gates.get(name, False)]
    if failed:
        raise RuntimeError(
            f"acceptance quality gates failed for {WORKFLOW_ID}: {failed}"
        )
    verified = datetime.now(timezone.utc)
    definition = get_workflow_definition(WORKFLOW_ID)
    return AcceptanceEvidence(
        workflow_id=WORKFLOW_ID,
        contract_version=REGISTRY_SCHEMA,
        fixture_version="v1",
        fixture_path=FIXTURE.relative_to(REPO_ROOT).as_posix(),
        fixture_checksum=sha256_tree(FIXTURE),
        layers={
            "contract": definition.adapter == WORKFLOW_ID,
            "adapter": get_workflow_class(WORKFLOW_ID).is_stub is False,
            "offline_e2e": True,
            "provider": provider,
        },
        gates=gates,
        artifacts=[
            AcceptanceArtifact(str(path.resolve()), sha256_file(path))
            for path in artifacts
        ],
        implementation_checksums=_implementation_checksums(),
        started_at=started.isoformat(),
        verified_at=verified.isoformat(),
        duration_seconds=(verified - started).total_seconds(),
        provider=settings.to_acceptance_metadata() if provider else {},
    )


def _resolve_output_root(output_root: Path | None) -> Path:
    root = output_root or (REPO_ROOT / ".scratch" / "workflow-acceptance")
    return root.expanduser().resolve()


class _TemplateFixtureCaller:
    """Drive the real persistent Agent protocol without calling a provider."""

    def __call__(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        initial = json.loads(messages[1]["content"])
        if initial["phase"] == "strategist_draft":
            pages = int(initial["checkpoint"]["page_count"])
            return {
                "action": "submit_strategy_draft",
                "arguments": {"strategy": _offline_document_strategy(pages)},
            }

        checkpoint = initial["checkpoint"]
        project = Path(initial["project_path"])
        page_count = int(checkpoint["page_count"])
        actions = [
            json.loads(item["content"])
            for item in messages
            if item.get("role") == "assistant"
        ]
        read_paths = {
            str(item["arguments"]["path"])
            for item in actions
            if item.get("action") == "read_reference_svg"
        }
        for reference in checkpoint.get("expected_references") or []:
            if reference not in read_paths:
                return {
                    "action": "read_reference_svg",
                    "arguments": {"path": reference},
                }

        written = [item for item in actions if item.get("action") == "write_svg_page"]
        if written and actions[-1].get("action") == "read_spec_lock":
            page = int(actions[-1]["arguments"]["page"])
            observation = json.loads(messages[-1]["content"])["observation"]
            return {
                "action": "write_svg_page",
                "arguments": {
                    "page": page,
                    "spec_digest": observation["spec_digest"],
                    "svg": _materialize_reference_page(project, page),
                    "notes": f"Acceptance note for template-guided page {page}.",
                    "summary": f"Template-guided acceptance page {page}.",
                },
            }
        if not written and actions and actions[-1].get("action") == "read_spec_lock":
            observation = json.loads(messages[-1]["content"])["observation"]
            return {
                "action": "write_svg_page",
                "arguments": {
                    "page": 1,
                    "spec_digest": observation["spec_digest"],
                    "svg": _materialize_reference_page(project, 1),
                    "notes": "Acceptance note for template-guided page 1.",
                    "summary": "Template-guided acceptance page 1.",
                },
            }
        if len(written) < page_count:
            return {
                "action": "read_spec_lock",
                "arguments": {"page": len(written) + 1},
            }
        if not any(item.get("action") == "submit_chart_receipt" for item in actions):
            return {
                "action": "submit_chart_receipt",
                "arguments": {
                    "receipt": {
                        "skipped": True,
                        "reason": "design spec declares no data-driven chart geometry",
                    }
                },
            }
        return {"action": "report_phase_complete", "arguments": {}}


def _materialize_reference_page(project: Path, page: int) -> str:
    bindings = json.loads(
        (project / "templates" / "template_bindings.json").read_text(
            encoding="utf-8"
        )
    )["bindings"]
    reference = Path(bindings[str(page)]["svg_path"])
    root = ET.fromstring(reference.read_text(encoding="utf-8"))
    replacements = {
        1: ("A RELIABLE INTERACTIVE PPT AGENT", "CONVERSATION WITH CONTROL"),
        2: ("MODULAR AGENT ARCHITECTURE", "CORE, TAURI, AND FUTURE WEB SHELLS"),
        3: ("WORKFLOWS VERIFIED FIRST", "READY FOR PRODUCT CONVERSION"),
    }
    title, body = replacements[page]
    replacement_index = 0
    for element in root.iter():
        text = str(element.text or "")
        if "SAMPLE" not in text.upper():
            continue
        element.text = title if replacement_index == 0 else body
        replacement_index += 1
    ET.register_namespace("", SVG_NAMESPACE)
    return ET.tostring(root, encoding="unicode", short_empty_elements=True)


def _run_fixture(
    run_root: Path, *, provider: bool
) -> tuple[dict[str, Any], list[Path]]:
    request = json.loads((FIXTURE / "request.json").read_text(encoding="utf-8"))
    request["source_files"] = [
        str((FIXTURE / item).resolve()) for item in request["source_files"]
    ]
    template_pptx = run_root / "input" / "template.pptx"
    _build_template_pptx(FIXTURE / "template_spec.json", template_pptx)
    request["template_pptx"] = str(template_pptx)
    expected_pages = int(request["pages"])

    store = JobStore(run_root / "jobs", run_root / "logs")
    runtime = Runtime(store, enforce_maturity=False)
    runtime.workspace_root = run_root / "runtime-workspaces"
    runtime.outputs_root = run_root / "outputs"
    project = run_root / "project"
    _create_project_tree(project)
    if not provider:
        base = AgentPhaseRunner(
            store,
            call_messages=_TemplateFixtureCaller(),
            page_checker=lambda *_: [],
            project_checker=lambda *_: [],
        )
        runtime.phase_runner = PhaseRunnerRouter(base)
        runtime.preview_runtime = _OfflinePreview(store)

    def fake_create_project(*_args: Any, **_kwargs: Any) -> str:
        return str(project)

    def fake_convert_sources(
        files: list[str], _urls: list[str], project_path: str
    ) -> list[str]:
        outputs: list[str] = []
        for source in files:
            target = Path(project_path) / "sources" / Path(source).name
            target.write_text(
                Path(source).read_text(encoding="utf-8"), encoding="utf-8"
            )
            outputs.append(str(target))
        return outputs

    def fake_export(project_path: str, log: Callable[..., Any] | None = None) -> str:
        result = ToolResult(
            command=["offline-export"],
            cwd=project_path,
            returncode=0,
            stdout="offline template-guided acceptance",
            stderr="",
        )
        for name in ("total_md_split", "finalize_svg", "svg_to_pptx"):
            if log:
                log(name, result)
        return _offline_export_project(project_path)

    with ExitStack() as stack:
        stack.enter_context(
            mock.patch(
                "ppt_master_service.workflows.document_to_ppt.project_tools.create_project",
                side_effect=fake_create_project,
            )
        )
        stack.enter_context(
            mock.patch(
                "ppt_master_service.workflows.document_to_ppt.source_tools.convert_sources_to_markdown",
                side_effect=fake_convert_sources,
            )
        )
        stack.enter_context(
            mock.patch(
                "ppt_master_service.workflows.document_to_ppt.project_tools.validate_project",
                return_value={"passed": True},
            )
        )
        if not provider:
            stack.enter_context(
                mock.patch(
                    "ppt_master_service.workflows.document_to_ppt.quality_tools.check_svg_project_findings",
                    return_value=[],
                )
            )
            stack.enter_context(
                mock.patch(
                    "ppt_master_service.workflows.document_to_ppt.export_tools.export_project_to_pptx",
                    side_effect=fake_export,
                )
            )

        job = store.create_job(request, workflow=WORKFLOW_ID)
        runtime.run_job(job["job_id"])
        waiting = store.get_job(job["job_id"]) or {}
        if waiting.get("status") != "waiting_user":
            raise RuntimeError(
                "template acceptance did not reach waiting_user: "
                + str(waiting.get("error_detail") or waiting.get("error"))
            )
        interaction = dict(waiting["interaction"])
        submission = runtime.submit_interaction(
            job["job_id"],
            interaction["interaction_id"],
            InteractionResponseRequest(
                action="confirm",
                revision=int(interaction["revision"]),
                strategy_digest=str(interaction["strategy_digest"]),
                confirmations=dict(interaction["confirmations"]),
            ),
        )
        if not submission.resume_required:
            raise RuntimeError("template acceptance confirmation did not schedule resume")
        runtime.run_job(job["job_id"])

    final = store.get_job(job["job_id"]) or {}
    if final.get("status") != "success":
        raise RuntimeError(
            "template acceptance failed: "
            + str(final.get("error_detail") or final.get("error"))
        )
    job_dir = store.jobs_dir / job["job_id"]
    receipt_files = sorted((job_dir / "interactions").glob("*.receipt.json"))
    if len(receipt_files) != 1:
        raise RuntimeError("template acceptance expected one interaction receipt")
    events = [
        json.loads(line)
        for line in (job_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    event_digest = project / "acceptance_event_digest.json"
    event_digest.write_text(
        json.dumps(
            {
                "events": [
                    {
                        key: event[key]
                        for key in (
                            "ts",
                            "message",
                            "phase",
                            "action",
                            "page",
                            "accepted",
                        )
                        if key in event
                    }
                    for event in events
                ]
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    quality_report = project / "quality_report.json"
    quality_report.write_text(
        json.dumps(final.get("review") or {}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    executor = store.load_agent_session(job["job_id"], "executor") or {}
    stages = {
        "prepare": {"ok": project.is_dir()},
        "run": {"ok": True, "execution": executor.get("output") or {}},
        "review": dict(final.get("review") or {}),
        "export": {"ok": True, "output_file": final["output_file"]},
        "_acceptance": {
            "project_path": str(project),
            "source_file": request["source_files"][0],
            "template_pptx": str(template_pptx),
            "receipt_file": str(receipt_files[0]),
            "events": events,
            "expected_pages": expected_pages,
            "expected_references": list(executor.get("expected_references") or []),
            "read_references": list(executor.get("read_references") or []),
        },
    }
    artifacts = [
        FIXTURE / "request.json",
        FIXTURE / "source.md",
        FIXTURE / "template_spec.json",
        template_pptx,
        receipt_files[0],
        project / "templates" / "source_import" / "manifest.json",
        project / "templates" / "template_context.json",
        project / "templates" / "template_bindings.json",
        project / "design_spec.md",
        project / "spec_lock.md",
        project / ".service_strategy.json",
        *sorted((project / "svg_output").glob("*.svg")),
        project / "notes" / "total.md",
        quality_report,
        Path(final["output_file"]),
        event_digest,
    ]
    return stages, [path for path in artifacts if path.is_file()]


def _quality_gates(stages: dict[str, Any]) -> dict[str, bool]:
    meta = stages["_acceptance"]
    project = Path(meta["project_path"])
    events = list(meta["events"])
    expected_pages = int(meta["expected_pages"])
    output = Path(stages["export"]["output_file"])
    context_path = project / "templates" / "template_context.json"
    bindings_path = project / "templates" / "template_bindings.json"
    strategy_path = project / ".service_strategy.json"
    design_path = project / "design_spec.md"
    lock_path = project / "spec_lock.md"
    context = _read_json(context_path)
    bindings = _read_json(bindings_path)
    strategy = _read_json(strategy_path)
    design = design_path.read_text(encoding="utf-8") if design_path.is_file() else ""
    lock = lock_path.read_text(encoding="utf-8") if lock_path.is_file() else ""
    write_events = _accepted_actions(events, "write_svg_page")
    lock_events = _accepted_actions(events, "read_spec_lock")
    read_events = _accepted_actions(events, "read_reference_svg")
    report_index = _event_index(
        events,
        lambda event: event.get("message") == "agent action"
        and event.get("action") == "report_phase_complete"
        and event.get("accepted") is True,
    )
    notes_index = _event_index(
        events, lambda event: event.get("message") == "notes_materialized"
    )
    preview_index = _event_index(
        events,
        lambda event: event.get("message") == "executor design parameters",
    )
    first_write_index = _event_index(
        events,
        lambda event: event.get("message") == "agent action"
        and event.get("action") == "write_svg_page",
    )
    export_indices = [
        _event_index(
            events,
            lambda event, name=name: event.get("message")
            == f"export tool step complete: {name}",
        )
        for name in ("total_md_split", "finalize_svg", "svg_to_pptx")
    ]
    roles = [
        (bindings.get("bindings") or {}).get(str(page), {}).get("role")
        for page in range(1, expected_pages + 1)
    ]
    layouts = (strategy.get("spec_lock") or {}).get("page_layouts") or {}
    expected_refs = set(meta["expected_references"])
    read_refs = set(meta["read_references"])
    reference_events_before_write = all(
        events.index(event) < first_write_index for event in read_events
    ) if read_events and first_write_index >= 0 else False
    svg_text = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in sorted((project / "svg_output").glob("*.svg"))
    )
    try:
        context_valid = bool(load_template_context(project))
    except (OSError, ValueError, json.JSONDecodeError):
        context_valid = False
    try:
        template_findings = check_template_project_findings(str(project))
    except (OSError, ValueError, json.JSONDecodeError):
        template_findings = [{"error": "template policy could not run"}]
    return {
        "source_ready": Path(meta["source_file"]).is_file(),
        "template_pptx_valid": zipfile.is_zipfile(Path(meta["template_pptx"])),
        "template_import_complete": (
            project / "templates" / "source_import" / "manifest.json"
        ).is_file(),
        "template_context_valid": context_valid,
        "design_confirmed": Path(meta["receipt_file"]).is_file()
        and any(event.get("message") == "job waiting for user" for event in events),
        "design_spec_canonical": all(
            f"## {heading}" in design for heading in llm_runtime.DESIGN_HEADINGS
        ),
        "spec_lock_parseable": all(
            f"## {section}" in lock
            for section in (
                "canvas",
                "colors",
                "typography",
                "icons",
                "images",
                "page_rhythm",
                "page_layouts",
                "page_charts",
            )
        ),
        "template_design_locked": _template_identity_matches(context, strategy),
        "template_role_mapping": roles == ["cover", "content", "ending"]
        and all(
            str((layouts.get(str(page)) or {}).get("path") or "")
            == str((bindings.get("bindings") or {}).get(str(page), {}).get("svg_path") or "")
            for page in range(1, expected_pages + 1)
        ),
        "preview_healthy": preview_index >= 0
        and first_write_index >= 0
        and preview_index < first_write_index,
        "template_references_preloaded": expected_refs == read_refs
        and len(read_events) == len(expected_refs)
        and reference_events_before_write,
        "sequential_pages": [event.get("page") for event in write_events]
        == list(range(1, expected_pages + 1)),
        "spec_lock_read_per_page": [event.get("page") for event in lock_events]
        == list(range(1, expected_pages + 1)),
        "template_chrome_preserved": not template_findings,
        "template_sample_content_replaced": "SAMPLE TEMPLATE" not in svg_text.upper(),
        "svg_quality": bool((stages["review"].get("review") or {}).get("passed")),
        "notes_after_quality": report_index >= 0 and notes_index > report_index,
        "postprocess_order": all(index >= 0 for index in export_indices)
        and export_indices == sorted(export_indices),
        "pptx_readable": output.is_file() and zipfile.is_zipfile(output),
        "pptx_page_count": _pptx_slide_count(output) == expected_pages,
    }


def _template_identity_matches(
    context: dict[str, Any], strategy: dict[str, Any]
) -> bool:
    theme = context.get("theme") or {}
    colors = theme.get("colors") or {}
    fonts = theme.get("fonts") or {}
    confirmed = strategy.get("confirmations") or {}
    actual_colors = confirmed.get("color_scheme") or {}
    actual_type = confirmed.get("typography") or {}
    expected_colors = {
        "background": _first(colors, "background", "lt1", "bg1", default="#FFFFFF"),
        "primary": _first(colors, "primary", "accent1", "dk1", default="#202124"),
        "accent": _first(colors, "accent", "accent2", "accent1", default="#005587"),
        "body_text": _first(colors, "body_text", "dk1", "tx1", default="#202124"),
    }
    title = str(
        fonts.get("majorLatin")
        or fonts.get("majorEastAsia")
        or "Aptos Display"
    )
    body = str(fonts.get("minorLatin") or fonts.get("minorEastAsia") or "Aptos")
    return (
        actual_colors == expected_colors
        and actual_type.get("title_family") == title
        and actual_type.get("body_family") == body
    )


def _first(values: dict[str, Any], *keys: str, default: str) -> str:
    for key in keys:
        value = str(values.get(key) or "").strip()
        if value:
            return value.upper() if value.startswith("#") else value
    return default


def _accepted_actions(
    events: list[dict[str, Any]], action: str
) -> list[dict[str, Any]]:
    return [
        event
        for event in events
        if event.get("message") == "agent action"
        and event.get("action") == action
        and event.get("accepted") is True
    ]


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def _create_project_tree(project: Path) -> None:
    for relative in (
        "sources",
        "svg_output",
        "notes",
        "images",
        "exports",
        "repair",
        "templates",
    ):
        (project / relative).mkdir(parents=True, exist_ok=True)


def _build_template_pptx(spec_path: Path, output: Path) -> None:
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    presentation = Presentation()
    presentation.slide_width = Inches(float(spec["canvas"]["width_inches"]))
    presentation.slide_height = Inches(float(spec["canvas"]["height_inches"]))
    while presentation.slides:
        relationship_id = presentation.slides._sldIdLst[-1].rId
        presentation.part.drop_rel(relationship_id)
        del presentation.slides._sldIdLst[-1]
    theme = spec["theme"]
    for slide_spec in spec["slides"]:
        slide = presentation.slides.add_slide(presentation.slide_layouts[6])
        _add_rect(slide, 0, 0, 13.333333, 7.5, theme["background"])
        _add_template_chrome(slide, str(slide_spec["accent_side"]), theme)
        _add_text(
            slide,
            1.05,
            1.35,
            11.15,
            1.05,
            str(slide_spec["sample_title"]),
            theme["title_font"],
            30,
            theme["primary"],
        )
        _add_text(
            slide,
            1.05,
            2.65,
            11.15,
            2.1,
            str(slide_spec["sample_body"]),
            theme["body_font"],
            18,
            theme["body_text"],
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    presentation.save(output)


def _add_template_chrome(slide: Any, side: str, theme: dict[str, str]) -> None:
    if side == "left":
        _add_rect(slide, 0, 0, 0.18, 7.5, theme["accent"])
    elif side == "top":
        _add_rect(slide, 0, 0, 13.333333, 0.18, theme["accent"])
    else:
        _add_rect(slide, 0, 7.32, 13.333333, 0.18, theme["accent"])
    _add_rect(slide, 0.78, 6.82, 2.2, 0.08, theme["primary"])


def _add_rect(
    slide: Any, x: float, y: float, width: float, height: float, color: str
) -> None:
    shape = slide.shapes.add_shape(
        MSO_SHAPE.RECTANGLE,
        Inches(x),
        Inches(y),
        Inches(width),
        Inches(height),
    )
    shape.fill.solid()
    shape.fill.fore_color.rgb = RGBColor.from_string(color.lstrip("#"))
    shape.line.fill.background()


def _add_text(
    slide: Any,
    x: float,
    y: float,
    width: float,
    height: float,
    value: str,
    font_family: str,
    font_size: int,
    color: str,
) -> None:
    frame = slide.shapes.add_textbox(
        Inches(x), Inches(y), Inches(width), Inches(height)
    ).text_frame
    frame.clear()
    paragraph = frame.paragraphs[0]
    run = paragraph.add_run()
    run.text = value
    run.font.name = font_family
    run.font.size = Pt(font_size)
    run.font.color.rgb = RGBColor.from_string(color.lstrip("#"))


def _implementation_checksums() -> dict[str, str]:
    files = [
        "ppt_master_service/provider_config.py",
        "ppt_master_service/runtime.py",
        "ppt_master_service/phase_runner_router.py",
        "ppt_master_service/template_guided_phase_runner.py",
        "ppt_master_service/agent_phase_runner.py",
        "ppt_master_service/interaction_coordinator.py",
        "ppt_master_service/preview_runtime.py",
        "ppt_master_service/llm_runtime.py",
        "ppt_master_service/workflows/document_to_ppt.py",
        "ppt_master_service/workflows/template_guided_svg.py",
        "ppt_master_service/tools/quality_tools.py",
        "ppt_master_service/tools/template_tools.py",
        "ppt_master_service/tools/template_guidance.py",
        "ppt_master_service/tools/template_guided_policy.py",
        "ppt_master_service/conformance/acceptance.py",
        "ppt_master_service/conformance/acceptance_case_runner.py",
        "ppt_master_service/conformance/acceptance_cases/template_guided_svg.py",
    ]
    return {relative: sha256_file(REPO_ROOT / relative) for relative in files}
