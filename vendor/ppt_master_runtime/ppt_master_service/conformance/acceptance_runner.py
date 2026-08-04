from __future__ import annotations

import argparse
import base64
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Callable
from unittest import mock
import uuid
import zipfile

from .. import llm_runtime
from ..agent_phase_runner import AgentPhaseRunner
from ..job_store import JobStore
from ..provider_config import ProviderSettings, initialize_provider_environment
from ..runtime import Runtime, validate_stage_result
from ..schemas import InteractionResponseRequest, ReviewResult
from ..tools import image_tools
from ..tools.project_tools import ToolResult
from ..workflows import get_workflow_class
from .acceptance import (
    AcceptanceArtifact,
    AcceptanceEvidence,
    sha256_file,
    sha256_tree,
    write_acceptance_evidence,
)
from .registry import REGISTRY_SCHEMA, REPO_ROOT, get_workflow_definition


FIXTURE_ROOT = REPO_ROOT / "ppt_master_service" / "fixtures" / "workflows"
CANONICAL_EVIDENCE_ROOT = REPO_ROOT / "workflows" / "acceptance"
VALID_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


@dataclass(frozen=True)
class AcceptanceDependencies:
    call_json: Callable[[str, str | None], dict[str, Any]] | None = None
    image_provider: Callable[[str], Any] | None = None
    run_executor: Callable[..., dict[str, Any]] | None = None
    export_project: Callable[[str], str] | None = None


def build_provider_metadata(settings: ProviderSettings) -> dict[str, str]:
    return settings.to_acceptance_metadata()


def run_acceptance(
    workflow_id: str,
    *,
    provider: bool,
    output_root: Path | None = None,
    dependencies: AcceptanceDependencies | None = None,
) -> AcceptanceEvidence:
    if workflow_id not in {
        "mirror_template",
        "image_heavy",
        "document_to_ppt",
    }:
        raise ValueError(f"unsupported workflow: {workflow_id}")
    settings = initialize_provider_environment(REPO_ROOT)
    if provider:
        _require_provider_configuration(workflow_id, settings)
    fixture = FIXTURE_ROOT / workflow_id
    if not fixture.is_dir():
        raise FileNotFoundError(f"acceptance fixture does not exist: {fixture}")
    root = _resolve_output_root(output_root)
    run_root = root / f"{workflow_id}-{uuid.uuid4().hex[:8]}"
    run_root.mkdir(parents=True, exist_ok=False)
    active = dependencies or (_provider_dependencies() if provider else _offline_dependencies(workflow_id))
    started = datetime.now(timezone.utc)
    stage_results, artifacts = _run_fixture(workflow_id, fixture, run_root, active, provider=provider)
    verified = datetime.now(timezone.utc)
    gates = _quality_gates(workflow_id, stage_results, artifacts)
    if not all(gates.values()):
        failed = [name for name, passed in gates.items() if not passed]
        raise RuntimeError(f"acceptance quality gates failed for {workflow_id}: {failed}")
    definition = get_workflow_definition(workflow_id)
    layers = {
        "contract": definition.adapter == workflow_id,
        "adapter": get_workflow_class(workflow_id).is_stub is False,
        "offline_e2e": True,
        "provider": provider,
    }
    return AcceptanceEvidence(
        workflow_id=workflow_id,
        contract_version=REGISTRY_SCHEMA,
        fixture_version="v1",
        fixture_path=fixture.relative_to(REPO_ROOT).as_posix(),
        fixture_checksum=sha256_tree(fixture),
        layers=layers,
        gates=gates,
        artifacts=[AcceptanceArtifact(str(path.resolve()), sha256_file(path)) for path in artifacts],
        implementation_checksums=_implementation_checksums(workflow_id),
        started_at=started.isoformat(),
        verified_at=verified.isoformat(),
        duration_seconds=(verified - started).total_seconds(),
        provider=build_provider_metadata(settings) if provider else {},
    )


def _resolve_output_root(output_root: Path | None) -> Path:
    root = output_root or (REPO_ROOT / ".scratch" / "workflow-acceptance")
    return root.expanduser().resolve()


def _run_fixture(
    workflow_id: str,
    fixture: Path,
    run_root: Path,
    dependencies: AcceptanceDependencies,
    *,
    provider: bool,
) -> tuple[dict[str, dict[str, Any]], list[Path]]:
    if workflow_id == "document_to_ppt":
        return _run_document_fixture(
            fixture, run_root, provider=provider
        )
    request = json.loads((fixture / "request.json").read_text(encoding="utf-8"))
    request["source_files"] = [str((fixture / item).resolve()) for item in request.get("source_files") or []]
    store = JobStore(run_root / "jobs", run_root / "logs")
    job = store.create_job(request, workflow=workflow_id)
    workspace = run_root / "workspace"
    workspace.mkdir(parents=True)
    job = store.update_job(job["job_id"], workspace=str(workspace))
    runtime = Runtime(store, enforce_maturity=False)
    runtime.workspace_root = run_root / "runtime-workspaces"
    runtime.outputs_root = run_root / "outputs"
    if workflow_id == "mirror_template":
        runtime.template_metadata_dir = fixture / "template"
        runtime.template_root = fixture / "template"
    _install_dependencies(runtime, dependencies)
    workflow = get_workflow_class(workflow_id)(runtime)

    with ExitStack() as stack:
        if not provider:
            project = run_root / "project"
            _install_offline_external_patches(stack, workflow_id, project)
        stage_results: dict[str, dict[str, Any]] = {}
        current = job
        for stage in ("prepare", "run", "review", "export"):
            result = validate_stage_result(stage, getattr(workflow, stage)(current))
            if result.get("status") == "waiting_for_image_generation":
                raise RuntimeError(f"offline acceptance unexpectedly waited for images: {result.get('message')}")
            stage_results[stage] = result
            current = store.get_job(job["job_id"]) or current

    project_path = Path((store.get_job(job["job_id"]) or current)["project_path"])
    output = Path(stage_results["export"]["output_file"])
    artifacts = [output]
    for relative in (
        "analysis/mirror_roster.json",
        "mirror_plan.json",
        "mirror_plan_report.json",
        "images/image_prompts.json",
        "images/image_prompts.md",
        "visual_assets.json",
    ):
        candidate = project_path / relative
        if candidate.is_file():
            artifacts.append(candidate)
    return stage_results, artifacts


class _ScriptedActionCaller:
    def __init__(self, actions: list[dict[str, Any]]) -> None:
        self._actions = iter(actions)

    def __call__(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        action = json.loads(json.dumps(next(self._actions)))
        arguments = action.get("arguments") or {}
        if arguments.get("spec_digest") == "$CURRENT":
            observation = json.loads(messages[-1]["content"])["observation"]
            arguments["spec_digest"] = observation["spec_digest"]
        return action


class _OfflinePreview:
    def __init__(self, store: JobStore) -> None:
        self.store = store

    def start(self, job_id: str, _project_path: Path):
        from ..preview_runtime import PreviewSession

        session = PreviewSession(
            url="http://127.0.0.1:5050",
            port=5050,
            pid=0,
            status="healthy",
            started_at=datetime.now(timezone.utc).isoformat(),
        )
        self.store.save_preview(job_id, {
            "url": session.url,
            "port": session.port,
            "pid": session.pid,
            "status": session.status,
            "started_at": session.started_at,
        })
        return session


def _run_document_fixture(
    fixture: Path,
    run_root: Path,
    *,
    provider: bool,
) -> tuple[dict[str, dict[str, Any]], list[Path]]:
    request = json.loads((fixture / "request.json").read_text(encoding="utf-8"))
    request["source_files"] = [
        str((fixture / item).resolve())
        for item in request.get("source_files") or []
    ]
    expected_pages = int(request["pages"])
    store = JobStore(run_root / "jobs", run_root / "logs")
    runtime = Runtime(store, enforce_maturity=False)
    project = run_root / "project"
    for relative in (
        "sources",
        "svg_output",
        "notes",
        "images",
        "exports",
        "repair",
    ):
        (project / relative).mkdir(parents=True, exist_ok=True)

    if not provider:
        actions = [
            {
                "action": "submit_strategy_draft",
                "arguments": {
                    "strategy": _offline_document_strategy(expected_pages)
                },
            },
            *_offline_document_executor_actions(expected_pages),
        ]
        runtime.phase_runner = AgentPhaseRunner(
            store,
            call_messages=_ScriptedActionCaller(actions),
            page_checker=lambda *_: [],
            project_checker=lambda *_: [],
        )
        runtime.preview_runtime = _OfflinePreview(store)

    def fake_create_project(*_args, **_kwargs) -> str:
        return str(project)

    def fake_convert_sources(files, _urls, project_path):
        outputs: list[str] = []
        for source in files:
            target = Path(project_path) / "sources" / Path(source).name
            target.write_text(
                Path(source).read_text(encoding="utf-8"), encoding="utf-8"
            )
            outputs.append(str(target))
        return outputs

    def fake_export(project_path: str, log=None) -> str:
        result = ToolResult(
            command=["offline-export"],
            cwd=project_path,
            returncode=0,
            stdout="offline acceptance",
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

        job = store.create_job(request, workflow="document_to_ppt")
        workspace = run_root / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        store.update_job(job["job_id"], workspace=str(workspace))
        runtime.run_job(job["job_id"])
        waiting = store.get_job(job["job_id"]) or {}
        if waiting.get("status") != "waiting_user":
            raise RuntimeError(
                "document acceptance did not reach waiting_user: "
                + str(waiting.get("error") or waiting.get("status"))
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
            raise RuntimeError("acceptance confirmation did not schedule resume")
        runtime.run_job(job["job_id"])

    final = store.get_job(job["job_id"]) or {}
    if final.get("status") != "success":
        raise RuntimeError(
            "document acceptance failed: "
            + str(final.get("error_detail") or final.get("error"))
        )
    job_dir = store.jobs_dir / job["job_id"]
    receipt_files = sorted((job_dir / "interactions").glob("*.receipt.json"))
    if len(receipt_files) != 1:
        raise RuntimeError("document acceptance expected one interaction receipt")
    event_path = job_dir / "events.jsonl"
    events = [
        json.loads(line)
        for line in event_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    event_digest = project / "acceptance_event_digest.json"
    event_digest.write_text(
        json.dumps(
            {
                "events": [
                    {
                        key: event.get(key)
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
    executor_session = store.load_agent_session(job["job_id"], "executor") or {}
    stage_results: dict[str, dict[str, Any]] = {
        "prepare": {"ok": project.is_dir()},
        "run": {"ok": True, "execution": executor_session.get("output") or {}},
        "review": dict(final.get("review") or {}),
        "export": {"ok": True, "output_file": final["output_file"]},
        "_acceptance": {
            "project_path": str(project),
            "source_file": request["source_files"][0],
            "receipt_file": str(receipt_files[0]),
            "events": events,
            "expected_pages": expected_pages,
            "expected_references": list(
                (final.get("checkpoint") or {}).get("expected_references") or []
            ),
            "read_references": list(executor_session.get("read_references") or []),
        },
    }
    artifacts: list[Path] = [
        fixture / "request.json",
        fixture / "source.md",
        receipt_files[0],
        project / "design_spec.md",
        project / "spec_lock.md",
        *sorted((project / "svg_output").glob("*.svg")),
        project / "notes" / "total.md",
        quality_report,
        Path(final["output_file"]),
        event_digest,
    ]
    return stage_results, [path for path in artifacts if path.is_file()]


def _offline_document_strategy(pages: int) -> dict[str, Any]:
    confirmations = {
        "canvas": {"format": "ppt169"},
        "page_count": {
            "minimum": pages,
            "maximum": pages,
            "recommended": pages,
        },
        "target_audience": {"description": "technical leaders"},
        "style_objective": {"description": "clear technical consulting"},
        "color_scheme": {
            "background": "#F7F8FA",
            "primary": "#174EA6",
            "accent": "#00A6A6",
            "body_text": "#202124",
        },
        "icon_usage": {"enabled": True, "style": "outline"},
        "typography": {
            "title_family": "Microsoft YaHei",
            "body_family": "Microsoft YaHei",
            "body_size_px": 20,
            "formula_policy": "mixed",
        },
        "image_usage": {
            "mode": "none",
            "rendering": "vector-illustration",
            "palette": "cool-corporate",
        },
    }
    outline = [
        {
            "index": page,
            "title": f"Acceptance {page}",
            "purpose": "Prove the interactive workflow",
            "key_points": [f"Verified point {page}"],
            "speaker_note": f"Explain verified point {page}.",
        }
        for page in range(1, pages + 1)
    ]
    return {
        "project_info": {
            "project_name": "Interactive Acceptance",
            "target_audience": "technical leaders",
        },
        "canvas": {"format": "ppt169", "width": 1280, "height": 720},
        "visual_theme": {
            "style_objective": "clear technical consulting",
            "colors": confirmations["color_scheme"],
        },
        "typography": dict(confirmations["typography"]),
        "layout": {"safe_margin": 48},
        "icon": dict(confirmations["icon_usage"]),
        "visualization": {"references": [], "runners_up": []},
        "image": {
            **confirmations["image_usage"],
            "image_prompts": {"items": []},
        },
        "outline": outline,
        "speaker_notes": {"required": True},
        "tech_constraints": {"foreignObject": False},
        "confirmations": confirmations,
        "split_mode_note": {
            "recommendation": "continuous",
            "message": "acceptance deck is small",
        },
        "spec_lock": {
            "canvas": {"format": "ppt169", "width": 1280, "height": 720},
            "colors": confirmations["color_scheme"],
            "typography": confirmations["typography"],
            "icons": confirmations["icon_usage"],
            "images": confirmations["image_usage"],
            "page_rhythm": {
                f"slide_{page:02d}": "anchor" if page == 1 else "dense"
                for page in range(1, pages + 1)
            },
            "page_layouts": {},
            "page_charts": {},
            "outline": outline,
        },
    }


def _offline_document_executor_actions(pages: int) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for page in range(1, pages + 1):
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" '
            'viewBox="0 0 1280 720" width="1280" height="720">'
            '<rect width="1280" height="720" fill="#F7F8FA"/>'
            f'<text x="80" y="120" font-size="40">Acceptance {page}</text>'
            "</svg>"
        )
        actions.extend([
            {"action": "read_spec_lock", "arguments": {"page": page}},
            {
                "action": "write_svg_page",
                "arguments": {
                    "page": page,
                    "spec_digest": "$CURRENT",
                    "svg": svg,
                    "notes": f"Acceptance note {page}",
                    "summary": f"Acceptance page {page}",
                },
            },
        ])
    actions.extend([
        {
            "action": "submit_chart_receipt",
            "arguments": {
                "receipt": {
                    "skipped": True,
                    "reason": "design spec declares no data-driven chart geometry",
                }
            },
        },
        {"action": "report_phase_complete", "arguments": {}},
    ])
    return actions


def _install_dependencies(runtime: Runtime, dependencies: AcceptanceDependencies) -> None:
    if dependencies.call_json is not None:
        runtime.call_llm_json = dependencies.call_json
    if dependencies.image_provider is not None:
        runtime.image_provider = dependencies.image_provider
    if dependencies.run_executor is not None:
        runtime.run_executor = dependencies.run_executor
    if dependencies.export_project is not None:
        runtime.export_project = dependencies.export_project


def _install_offline_external_patches(stack: ExitStack, workflow_id: str, project: Path) -> None:
    project.mkdir(parents=True, exist_ok=True)
    for relative in ("sources", "svg_output", "notes", "images", "exports", "analysis", "templates"):
        (project / relative).mkdir(parents=True, exist_ok=True)

    def fake_create_project(*_args, **_kwargs) -> str:
        return str(project)

    def fake_convert_sources(files, _urls, project_path):
        outputs: list[str] = []
        for source in files:
            target = Path(project_path) / "sources" / Path(source).name
            target.write_text(Path(source).read_text(encoding="utf-8"), encoding="utf-8")
            outputs.append(str(target))
        return outputs

    quality_result = ReviewResult(passed=True)
    if workflow_id == "mirror_template":
        stack.enter_context(mock.patch("ppt_master_service.workflows.mirror_template.project_tools.create_project", side_effect=fake_create_project))
        stack.enter_context(mock.patch("ppt_master_service.workflows.mirror_template.source_tools.convert_sources_to_markdown", side_effect=fake_convert_sources))
        stack.enter_context(mock.patch("ppt_master_service.workflows.mirror_template.quality_tools.check_svg_quality", return_value=quality_result))
    else:
        stack.enter_context(mock.patch("ppt_master_service.workflows.document_to_ppt.project_tools.create_project", side_effect=fake_create_project))
        stack.enter_context(mock.patch("ppt_master_service.workflows.document_to_ppt.source_tools.convert_sources_to_markdown", side_effect=fake_convert_sources))
        stack.enter_context(mock.patch("ppt_master_service.workflows.document_to_ppt.quality_tools.check_svg_quality", return_value=quality_result))


def _offline_dependencies(workflow_id: str) -> AcceptanceDependencies:
    return AcceptanceDependencies(
        call_json=_offline_mirror_plan if workflow_id == "mirror_template" else _offline_image_strategy,
        image_provider=_offline_image_provider if workflow_id == "image_heavy" else None,
        run_executor=_offline_image_executor if workflow_id == "image_heavy" else None,
        export_project=_offline_export_project,
    )


def _provider_dependencies() -> AcceptanceDependencies:
    return AcceptanceDependencies()


def _offline_mirror_plan(_prompt: str, _system: str | None) -> dict[str, Any]:
    return {
        "pages": [
            {
                "output_index": 1,
                "source_svg": "slide_01.svg",
                "replacements": [
                    {"slot_id": "slide_01:text:001", "lines": ["Interactive PPT Agent"]},
                    {"slot_id": "slide_01:text:002", "lines": ["Conversation becomes a validated presentation workflow"]},
                ],
                "speaker_note": "Introduce the interactive and modular product direction.",
            },
            {
                "output_index": 2,
                "source_svg": "slide_02.svg",
                "replacements": [
                    {"slot_id": "slide_02:text:001", "lines": ["Finish Workflows First"]},
                    {"slot_id": "slide_02:text:002", "lines": ["Verify every workflow before the Agent Core conversion"]},
                ],
                "speaker_note": "Close with the verified workflow-first next step.",
            },
        ]
    }


def _offline_image_strategy(_prompt: str, _system: str | None) -> dict[str, Any]:
    return {
        "outline": [
            {"index": 1, "role": "cover", "title": "Visual Workflow"},
            {"index": 2, "role": "content", "title": "Conversation to Deck"},
            {"index": 3, "role": "ending", "title": "Validated Output"},
        ],
        "image": {
            "image_prompts": {
                "items": [
                    {
                        "asset_id": "acceptance-hero",
                        "filename": "acceptance-hero.png",
                        "prompt": "A professional illustration of conversation flowing into a structured plan and presentation deck",
                        "purpose": "content workflow hero",
                        "aspect_ratio": "16:9",
                        "status": "Pending",
                        "slide_index": 2,
                    }
                ]
            }
        },
    }


def _offline_image_provider(manifest_path: str) -> None:
    path = Path(manifest_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    for item in payload["items"]:
        if item["status"] == "Pending":
            (path.parent / item["filename"]).write_bytes(VALID_PNG)
            item["status"] = "Generated"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _offline_image_executor(
    project_path: Path,
    _request: dict[str, Any],
    _strategy: dict[str, Any],
    log=None,
) -> dict[str, Any]:
    svg_dir = project_path / "svg_output"
    notes_dir = project_path / "notes"
    svg_dir.mkdir(parents=True, exist_ok=True)
    notes_dir.mkdir(parents=True, exist_ok=True)
    pages = {
        1: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720"><rect width="1280" height="720" fill="#F7F9FC"/><text x="100" y="180">Visual Workflow</text></svg>',
        2: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720"><rect width="1280" height="720" fill="#FFFFFF"/><image href="../images/acceptance-hero.png" x="160" y="120" width="960" height="480"/></svg>',
        3: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720"><rect width="1280" height="720" fill="#F7F9FC"/><text x="100" y="180">Validated Output</text></svg>',
    }
    generated: list[str] = []
    for index, svg in pages.items():
        path = svg_dir / f"slide_{index:02d}.svg"
        path.write_text(svg, encoding="utf-8")
        generated.append(str(path))
    (notes_dir / "total.md").write_text("# slide_01\n\nOffline acceptance\n", encoding="utf-8")
    return {"svg_files": generated, "notes_total": str(notes_dir / "total.md")}


def _offline_export_project(project_path: str) -> str:
    project = Path(project_path)
    output = project / "exports" / "acceptance.pptx"
    output.parent.mkdir(parents=True, exist_ok=True)
    slides = sorted((project / "svg_output").glob("*.svg"))
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        for index, _slide in enumerate(slides, start=1):
            archive.writestr(f"ppt/slides/slide{index}.xml", "<p:sld/>")
        for image in sorted((project / "images").glob("*")):
            if image.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
                archive.write(image, f"ppt/media/{image.name}")
    return str(output)


def _quality_gates(
    workflow_id: str,
    stages: dict[str, dict[str, Any]],
    artifacts: list[Path],
) -> dict[str, bool]:
    output = Path(stages["export"]["output_file"])
    readable = output.is_file() and zipfile.is_zipfile(output)
    if workflow_id == "document_to_ppt":
        meta = stages["_acceptance"]
        project = Path(meta["project_path"])
        events = list(meta["events"])
        expected_pages = int(meta["expected_pages"])
        expected_references = set(meta.get("expected_references") or [])
        read_references = set(meta.get("read_references") or [])
        write_events = [
            event
            for event in events
            if event.get("message") == "agent action"
            and event.get("action") == "write_svg_page"
            and event.get("accepted") is True
        ]
        lock_events = [
            event
            for event in events
            if event.get("message") == "agent action"
            and event.get("action") == "read_spec_lock"
            and event.get("accepted") is True
        ]
        report_index = _event_index(
            events,
            lambda event: event.get("message") == "agent action"
            and event.get("action") == "report_phase_complete"
            and event.get("accepted") is True,
        )
        notes_index = _event_index(
            events,
            lambda event: event.get("message") == "notes_materialized",
        )
        preview_index = _event_index(
            events,
            lambda event: event.get("message")
            == "executor design parameters",
        )
        first_page_index = _event_index(
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
        design = (project / "design_spec.md").read_text(
            encoding="utf-8", errors="replace"
        ) if (project / "design_spec.md").is_file() else ""
        lock = (project / "spec_lock.md").read_text(
            encoding="utf-8", errors="replace"
        ) if (project / "spec_lock.md").is_file() else ""
        return {
            "source_ready": Path(meta["source_file"]).is_file(),
            "design_confirmed": Path(meta["receipt_file"]).is_file()
            and any(
                event.get("message") == "job waiting for user"
                for event in events
            ),
            "design_spec_canonical": all(
                f"## {heading}" in design
                for heading in llm_runtime.DESIGN_HEADINGS
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
            "preview_healthy": preview_index >= 0
            and first_page_index >= 0
            and preview_index < first_page_index,
            "references_preloaded": expected_references == read_references,
            "sequential_pages": [event.get("page") for event in write_events]
            == list(range(1, expected_pages + 1)),
            "spec_lock_read_per_page": [
                event.get("page") for event in lock_events
            ]
            == list(range(1, expected_pages + 1)),
            "svg_quality": bool(
                (stages["review"].get("review") or {}).get("passed")
            ),
            "notes_after_quality": report_index >= 0
            and notes_index > report_index,
            "postprocess_order": all(index >= 0 for index in export_indices)
            and export_indices == sorted(export_indices),
            "pptx_readable": readable,
            "pptx_page_count": _pptx_slide_count(output) == expected_pages,
        }
    if workflow_id == "mirror_template":
        review = stages["review"]
        return {
            "roster_mapping_valid": Path(stages["run"]["plan_report"]).is_file(),
            "structural_fingerprint_preserved": bool(review.get("structural_fingerprints_preserved")),
            "svg_quality": bool(review.get("passed")),
            "pptx_readable": readable,
            "pptx_page_count": _pptx_slide_count(output) == 2,
        }
    image_review = stages["review"].get("image_review") or {}
    with zipfile.ZipFile(output) as archive:
        media = [name for name in archive.namelist() if name.startswith("ppt/media/")]
    return {
        "image_assets_ready": bool(image_review.get("verified_images")),
        "image_page_coverage": len(image_review.get("covered_pages") or []) >= int(image_review.get("required_raster_pages") or 1),
        "svg_quality": bool(stages["review"].get("ok")),
        "pptx_readable": readable,
        "pptx_media_present": bool(media),
    }


def _event_index(
    events: list[dict[str, Any]],
    predicate: Callable[[dict[str, Any]], bool],
) -> int:
    return next(
        (index for index, event in enumerate(events) if predicate(event)), -1
    )


def _pptx_slide_count(path: Path) -> int:
    if not zipfile.is_zipfile(path):
        return 0
    with zipfile.ZipFile(path) as archive:
        return len([name for name in archive.namelist() if name.startswith("ppt/slides/slide") and name.endswith(".xml")])


def _implementation_checksums(workflow_id: str) -> dict[str, str]:
    common = [
        "ppt_master_service/provider_config.py",
        "ppt_master_service/conformance/acceptance.py",
        "ppt_master_service/conformance/acceptance_runner.py",
        "ppt_master_service/tools/project_tools.py",
    ]
    if workflow_id == "mirror_template":
        specific = [
            "ppt_master_service/workflows/mirror_template.py",
            "ppt_master_service/workflows/mirror_template_adapter.py",
        ]
    elif workflow_id == "document_to_ppt":
        specific = [
            "ppt_master_service/workflows/document_to_ppt.py",
            "ppt_master_service/agent_phase_runner.py",
            "ppt_master_service/interaction_coordinator.py",
            "ppt_master_service/preview_runtime.py",
            "ppt_master_service/llm_runtime.py",
            "ppt_master_service/tools/quality_tools.py",
        ]
    else:
        specific = [
            "ppt_master_service/workflows/image_heavy.py",
            "ppt_master_service/workflows/image_heavy_policy.py",
            "ppt_master_service/workflows/document_to_ppt.py",
            "ppt_master_service/llm_runtime.py",
            "ppt_master_service/visual_assets.py",
            "ppt_master_service/tools/image_tools.py",
        ]
    return {relative: sha256_file(REPO_ROOT / relative) for relative in [*common, *specific]}


def _require_provider_configuration(workflow_id: str, settings: ProviderSettings) -> None:
    if not settings.llm_provider or not settings.llm_model:
        raise RuntimeError("provider acceptance requires configured LLM credentials and model")
    if workflow_id == "image_heavy" and not settings.image_provider:
        raise RuntimeError("image-heavy provider acceptance requires IMAGE_BACKEND or IMAGE_PROVIDER")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run workflow acceptance")
    parser.add_argument(
        "--workflow",
        required=True,
        choices=("mirror_template", "image_heavy", "document_to_ppt"),
    )
    parser.add_argument("--provider", action="store_true")
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--evidence-path", type=Path)
    args = parser.parse_args()
    if args.evidence_path and not args.provider:
        candidate = args.evidence_path.resolve()
        try:
            candidate.relative_to(CANONICAL_EVIDENCE_ROOT.resolve())
        except ValueError:
            pass
        else:
            raise SystemExit("canonical acceptance evidence requires --provider")
    if args.provider:
        print("Provider acceptance uses network access and may consume configured quota.")
    evidence = run_acceptance(args.workflow, provider=args.provider, output_root=args.output_root)
    if args.evidence_path:
        write_acceptance_evidence(args.evidence_path, evidence)
    print(json.dumps(evidence.to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
