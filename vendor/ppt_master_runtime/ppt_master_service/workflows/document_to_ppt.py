from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

from .base import BaseWorkflow
from ..image_understanding import understand_project_images
from .. import llm_runtime
from .. import visual_assets
from ..conformance import AgentPhaseRequest
from ..tools import export_tools, project_tools, quality_tools, source_tools


class DocumentToPPTWorkflow(BaseWorkflow):
    name = "document_to_ppt"
    is_stub = False

    def prepare(self, job: dict[str, Any]) -> dict[str, Any]:
        request = job.get("request", {})
        job_id = job["job_id"]
        project_path = (
            job.get("project_path")
            or request.get("project_path")
            or self._project_from_sources(request.get("source_files") or [])
        )

        if project_path:
            project = Path(project_path)
            if not project.exists() or not project.is_dir():
                raise RuntimeError(f"project_path does not exist: {project_path}")
            self.runtime.store.update_job(job_id, project_path=str(project))
            self.runtime.log(job_id, "using existing project", project_path=str(project))
        else:
            projects_dir = Path(job["workspace"]) / "projects" if job.get("workspace") else None
            project_path = project_tools.create_project(
                job_id,
                request.get("canvas_format") or "ppt169",
                projects_dir,
            )
            self.runtime.store.update_job(job_id, project_path=project_path)
            self.runtime.log(job_id, "created ppt-master project", project_path=project_path)
            markdown_files = source_tools.convert_sources_to_markdown(
                list(request.get("source_files") or []),
                list(request.get("source_urls") or []),
                project_path,
            )
            self.runtime.log(job_id, "converted sources to markdown", markdown_files=markdown_files)
            image_files = [str(path) for path in sorted((Path(project_path) / "images" / "user").glob("*")) if path.is_file()]
            if image_files:
                understanding = understand_project_images(Path(project_path), image_files, request)
                self.runtime.log(
                    job_id,
                    "image assets understood",
                    image_asset_count=len(understanding.get("assets") or []),
                    research_count=len(understanding.get("research") or []),
                )

        project = Path(project_path)
        (project / "request.json").write_text(
            json.dumps(request, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        validation = project_tools.validate_project(str(project))
        self.runtime.log(job_id, "validated project", result=validation)
        return {"ok": True, "stage": "prepare", "project_path": str(project)}

    def run(self, job: dict[str, Any]) -> dict[str, Any]:
        project = self._job_project_path(job)
        request = job.get("request", {})
        if self._existing_project_export_only(job):
            self.runtime.log(
                job["job_id"],
                "explicit existing-project export-only path selected",
            )
            return {
                "ok": True,
                "stage": "run",
                "project_path": str(project),
                "llm_skipped": True,
            }

        if not any((project / "sources").glob("*.md")):
            prompt_source = project / "sources" / "request.md"
            prompt_source.parent.mkdir(parents=True, exist_ok=True)
            prompt_source.write_text(
                f"# Request\n\n{request.get('prompt') or request.get('topic') or 'Presentation request'}\n",
                encoding="utf-8",
            )
        checkpoint = dict(job.get("checkpoint") or {})
        phase = str(checkpoint.get("resume_phase") or "strategy")

        if phase == "strategy":
            self.runtime.store.update_job(
                job["job_id"], status="generating_strategy"
            )
            strategy_phase = self.runtime.phase_runner.run_phase(
                AgentPhaseRequest(
                    job_id=job["job_id"],
                    phase="strategist_draft",
                    project_path=str(project),
                    checkpoint={
                        "request": request,
                        "page_count": int(request.get("pages") or 6),
                    },
                )
            )
            draft = dict(strategy_phase.output["strategy_draft"])
            interaction = llm_runtime.build_design_interaction(
                draft, revision=1
            )
            return {
                "ok": True,
                "status": "waiting_user",
                "stage": "run",
                "interaction": interaction.to_dict(),
                "checkpoint": {
                    "completed_runtime_steps": ["prepare"],
                    "resume_step": "run",
                    "resume_phase": "acquire_assets",
                    "strategy_draft": draft,
                    "strategy_digest": interaction.strategy_digest,
                    "interaction_id": interaction.interaction_id,
                },
            }

        def advance(next_phase: str, **updates: Any) -> None:
            nonlocal checkpoint, phase
            checkpoint = {
                **checkpoint,
                **updates,
                "resume_phase": next_phase,
            }
            self.runtime.store.update_job(
                job["job_id"], checkpoint=checkpoint
            )
            phase = next_phase

        receipt = self.runtime.store.get_receipt(
            job["job_id"], str(checkpoint["interaction_id"])
        )
        if receipt is None:
            raise RuntimeError("confirmed resume has no interaction receipt")
        if (
            llm_runtime.strategy_digest(dict(checkpoint["strategy_draft"]))
            != receipt["strategy_digest"]
        ):
            raise RuntimeError(
                "confirmed strategy digest changed before resume"
            )

        if phase == "acquire_assets":
            confirmed = llm_runtime.apply_confirmations(
                dict(checkpoint["strategy_draft"]),
                receipt["confirmations"],
            )
            advance("prepare_formula", confirmed_strategy=confirmed)
        else:
            confirmed = dict(checkpoint["confirmed_strategy"])

        if phase == "prepare_formula":
            self._prepare_formula_assets(project, confirmed, job["job_id"])
            advance("materialize_design")
        if phase == "materialize_design":
            llm_runtime.materialize_confirmed_strategy(project, confirmed)
            advance("materialize_visual_assets")
        if phase == "materialize_visual_assets":
            result = visual_assets.materialize_visual_assets(
                project,
                confirmed,
                log=self._asset_log(job),
                resume=True,
            )
            # visual_assets keeps legacy JSON lock compatibility; restore the
            # canonical Markdown lock before the persistent Executor reads it.
            llm_runtime.materialize_confirmed_strategy(project, confirmed)
            self.runtime.log(
                job["job_id"], "visual assets prepared", result=result
            )
            self._log_design_parameters(job, confirmed)
            advance(
                "execute",
                confirmed_strategy=confirmed,
                expected_references=self._collect_references(
                    project, confirmed
                ),
                declared_chart_pages=self._declared_chart_pages(confirmed),
            )
        if phase == "start_preview":
            # Migration for checkpoints written by the legacy workflow. V2
            # previews registered SVG artifacts inside the application and
            # never starts the PPT Master browser editor.
            self.runtime.log(
                job["job_id"],
                "legacy preview checkpoint skipped",
                preview_mode="application_artifact",
            )
            self._log_design_parameters(job, confirmed)
            advance(
                "execute",
                expected_references=self._collect_references(
                    project, confirmed
                ),
                declared_chart_pages=self._declared_chart_pages(confirmed),
            )
        if phase == "executor_complete":
            session = self.runtime.store.load_agent_session(
                job["job_id"], "executor"
            ) or {}
            if not session.get("completed"):
                raise RuntimeError(
                    "executor_complete checkpoint has no completed Agent session"
                )
            return {
                "ok": True,
                "stage": "run",
                "execution": session.get("output") or {},
                "project_path": str(project),
            }
        if phase != "execute":
            raise RuntimeError(
                f"unsupported document run phase: {phase}"
            )
        self.runtime.store.update_job(job["job_id"], status="generating_svg")
        result = self.runtime.phase_runner.run_phase(
            AgentPhaseRequest(
                job_id=job["job_id"],
                phase="executor",
                project_path=str(project),
                checkpoint={
                    "page_count": len(confirmed["outline"]),
                    "strategy_digest": receipt["strategy_digest"],
                    "expected_references": list(
                        checkpoint["expected_references"]
                    ),
                    "declared_chart_pages": list(
                        checkpoint["declared_chart_pages"]
                    ),
                },
            )
        )
        advance("executor_complete", executor_checkpoint=result.checkpoint)
        return {
            "ok": True,
            "stage": "run",
            "execution": result.output,
            "project_path": str(project),
        }

    def review(self, job: dict[str, Any]) -> dict[str, Any]:
        self.runtime.store.update_job(job["job_id"], status="checking_quality")
        project_path = self._job_project_path(job)
        if self._existing_project_export_only(job) or not (
            (job.get("checkpoint") or {}).get("interaction_id")
        ):
            legacy = quality_tools.check_svg_quality(str(project_path))
            if not legacy.passed:
                raise RuntimeError(
                    "SVG quality check failed: " + " | ".join(legacy.issues)
                )
            requested = int((job.get("request") or {}).get("pages") or 0)
            svg_count = len(
                list(project_path.joinpath("svg_output").glob("*.svg"))
            )
            if requested and svg_count != requested:
                raise RuntimeError(
                    f"SVG file count {svg_count} does not equal requested pages {requested}"
                )
            return {
                "ok": True,
                "stage": "review",
                "svg_count": svg_count,
                "review": legacy.model_dump(),
            }
        svg_files = sorted(project_path.joinpath("svg_output").glob("*.svg"))
        requested = int((job.get("request") or {}).get("pages") or 0)
        if requested and len(svg_files) != requested:
            raise RuntimeError(
                f"SVG file count {len(svg_files)} does not equal requested pages {requested}"
            )
        findings = quality_tools.check_svg_project_findings(str(project_path))
        if findings:
            raise RuntimeError(
                "SVG quality changed after executor completion: "
                + json.dumps(findings, ensure_ascii=False)
            )
        self.runtime.phase_runner.materialize_notes(
            job["job_id"], project_path
        )
        self.runtime.log(job["job_id"], "notes_materialized")
        session = self.runtime.store.load_agent_session(
            job["job_id"], "executor"
        ) or {}
        chart_receipt = (session.get("output") or {}).get("chart_receipt")
        if chart_receipt is None:
            raise RuntimeError(
                "executor session has no chart verification receipt"
            )
        review = {"passed": True, "findings": []}
        self.runtime.log(
            job["job_id"], "quality check complete", result=review
        )
        return {
            "ok": True,
            "stage": "review",
            "svg_count": len(svg_files),
            "chart_review": chart_receipt,
            "review": review,
        }

    def export(self, job: dict[str, Any]) -> dict[str, Any]:
        self.runtime.store.update_job(job["job_id"], status="exporting")
        project_path = self._job_project_path(job)

        def log_step(step_name, result):
            self.runtime.log(job["job_id"], f"export tool step complete: {step_name}", tool=result.to_dict())

        output_file = export_tools.export_project_to_pptx(str(project_path), log=log_step)
        output = Path(output_file)
        if not zipfile.is_zipfile(output):
            raise RuntimeError(f"export did not produce a readable PPTX: {output}")
        requested = int((job.get("request") or {}).get("pages") or 0)
        if requested:
            with zipfile.ZipFile(output) as archive:
                slide_count = len(
                    [
                        name
                        for name in archive.namelist()
                        if name.startswith("ppt/slides/slide")
                        and name.endswith(".xml")
                    ]
                )
            if slide_count != requested:
                raise RuntimeError(
                    f"PPTX slide count {slide_count} does not equal requested pages {requested}"
                )
        self.runtime.store.update_job(job["job_id"], output_file=output_file)
        return {"ok": True, "stage": "export", "output_file": output_file}

    def _prepare_formula_assets(
        self,
        project: Path,
        strategy: dict[str, Any],
        job_id: str,
    ) -> None:
        policy = str(
            strategy["confirmations"]["typography"]["formula_policy"]
        )
        items = list(
            (strategy.get("formula_manifest") or {}).get("items") or []
        )
        if policy == "text-only" or not items:
            self.runtime.log(
                job_id,
                "formula rendering skipped",
                policy=policy,
                formula_count=len(items),
            )
            return
        manifest = project / "images" / "formula_manifest.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            json.dumps({"items": items}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        script = (
            Path(os.environ.get("PPT_MASTER_ROOT") or Path(__file__).resolve().parents[2])
            / "skills"
            / "ppt-master"
            / "scripts"
            / "latex_render.py"
        )
        completed = subprocess.run(
            [sys.executable, str(script), str(project)],
            shell=False,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"formula renderer failed: {completed.stderr[-1000:]}"
            )

    def _collect_references(
        self, project: Path, strategy: dict[str, Any]
    ) -> list[str]:
        lock = strategy.get("spec_lock") or {}
        raw_values = [
            *(lock.get("page_layouts") or {}).values(),
            *(lock.get("page_charts") or {}).values(),
        ]
        raw_values.extend(
            item.get("path")
            for item in (strategy.get("visualization") or {}).get(
                "references", []
            )
        )
        values: list[str] = []
        for value in raw_values:
            if isinstance(value, dict):
                value = next(
                    (
                        value.get(key)
                        for key in ("path", "source_svg", "reference_svg")
                        if value.get(key)
                    ),
                    None,
                )
            if not isinstance(value, str) or Path(value).suffix.lower() != ".svg":
                continue
            values.append(value)
        repo_root = Path(os.environ.get("PPT_MASTER_ROOT") or Path(__file__).resolve().parents[2]).resolve()
        resolved: set[str] = set()
        for value in values:
            if not value:
                continue
            candidate = Path(str(value)).expanduser()
            if not candidate.is_absolute():
                project_candidate = project / candidate
                candidate = (
                    project_candidate
                    if project_candidate.exists()
                    else repo_root / candidate
                )
            candidate = candidate.resolve()
            if not candidate.is_file():
                raise RuntimeError(
                    f"declared SVG reference does not exist: {candidate}"
                )
            resolved.add(str(candidate))
        return sorted(resolved)

    def _declared_chart_pages(
        self, strategy: dict[str, Any]
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for item in (strategy.get("visualization") or {}).get(
            "references", []
        ):
            chart_type = str(
                item.get("chart_type") or item.get("type") or ""
            ).lower()
            if not item.get("data_driven") and chart_type not in {
                "bar",
                "line",
                "pie",
                "radar",
            }:
                continue
            match = re.fullmatch(
                r"(?:P|slide_)?0*([1-9]\d*)",
                str(item.get("page") or ""),
                flags=re.I,
            )
            if match is None:
                raise RuntimeError(
                    f"chart reference has invalid page: {item.get('page')!r}"
                )
            normalized = dict(item)
            normalized["page"] = int(match.group(1))
            normalized["chart_type"] = chart_type
            result.append(normalized)
        return result

    def _asset_log(self, job: dict[str, Any]):
        return lambda event, payload: self.runtime.log(
            job["job_id"], event, **payload
        )

    def _log_design_parameters(
        self,
        job: dict[str, Any],
        strategy: dict[str, Any],
    ) -> None:
        confirmations = strategy["confirmations"]
        self.runtime.log(
            job["job_id"],
            "executor design parameters",
            canvas=confirmations["canvas"],
            colors=confirmations["color_scheme"],
            typography=confirmations["typography"],
            preview_mode="application_artifact",
        )

    def _job_project_path(self, job: dict[str, Any]) -> Path:
        project_path = job.get("project_path") or (job.get("request") or {}).get("project_path")
        if not project_path:
            raise RuntimeError("job has no project_path")
        return Path(project_path)

    def _project_from_sources(self, source_files: list[str]) -> str | None:
        for item in source_files:
            path = Path(item)
            if path.exists() and path.is_dir() and (path / "svg_output").exists():
                return str(path)
        return None

    def _existing_project_export_only(self, job: dict[str, Any]) -> bool:
        intake = (job.get("request") or {}).get("intake_plan") or {}
        return intake.get("existing_project_export_only") is True
