from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from ..providers import ModelProvider
from .contracts import EventSink, WorkflowExecutor, WorkflowOutput


COMPOSED_DECK_WORKFLOWS = {
    "document_to_ppt",
    "topic_research",
    "brand_style",
    "image_heavy",
    "template_guided_svg",
}
COMPOSED_STEPS = [
    "source_ingest",
    "design_commit",
    "svg_generation",
    "quality_repair",
    "pptx_export",
    "review_prepare",
]


class LegacyPptMasterExecutor(WorkflowExecutor):
    """Adapter that hides the legacy four-stage runtime and its internal confirmation gate."""

    def __init__(self, ppt_master_root: Path, data_root: Path, provider: ModelProvider | None = None) -> None:
        self.ppt_master_root = ppt_master_root.resolve()
        if str(self.ppt_master_root) not in sys.path:
            sys.path.insert(0, str(self.ppt_master_root))
        from ppt_master_service.job_store import JobStore
        from ppt_master_service.runtime import Runtime

        store = JobStore(data_root / "legacy_jobs", data_root / "legacy_logs")
        self.runtime = Runtime(store)
        if provider is not None:
            from ppt_master_service.agent_phase_runner import AgentPhaseRunner
            from ppt_master_service.phase_runner_router import PhaseRunnerRouter

            self.runtime.call_llm_json = lambda prompt, system_prompt=None: provider.complete_json(
                system=system_prompt or "",
                prompt=prompt,
            )
            self.runtime.phase_runner = PhaseRunnerRouter(
                AgentPhaseRunner(store, call_messages=provider.complete_messages)
            )
        self.store = store

    def execute(self, workflow_id: str, inputs: dict[str, Any], emit: EventSink) -> WorkflowOutput:
        job = self.store.create_job(inputs, workflow=workflow_id)
        emit("legacy_job_created", {"job_id": job["job_id"], "workflow_id": workflow_id})
        for _ in range(4):
            self.runtime.run_job(job["job_id"])
            current = self.store.get_job(job["job_id"])
            if current is None:
                raise RuntimeError("legacy job disappeared")
            emit("legacy_job_status", {"job_id": job["job_id"], "status": current["status"], "step": current.get("current_step"), "progress": current.get("progress")})
            if current["status"] == "waiting_user":
                self._accept_internal_design(current)
                emit("design_committed", {"job_id": job["job_id"], "source": "agent_defaults", "review_deferred": True})
                continue
            break
        current = self.store.get_job(job["job_id"]) or {}
        if current.get("status") != "success":
            detail = current.get("error_detail") or {
                "code": "legacy_execution_failed",
                "message": current.get("error") or f"legacy job stopped with status {current.get('status')}",
                "location": {"job_id": job["job_id"], "step": current.get("current_step")},
                "correction": {"action": "inspect_legacy_job_log", "job_id": job["job_id"]},
            }
            raise LegacyWorkflowError(detail)
        output = current.get("output_file")
        artifacts = {"pptx": str(output)} if output else {}
        if current.get("project_path"):
            artifacts["project"] = str(current["project_path"])
        return WorkflowOutput(workflow_id, artifacts, {"legacy_job_id": job["job_id"], "review": current.get("review")})

    def execute_plan(
        self,
        workflow_id: str,
        inputs: dict[str, Any],
        steps: list[str],
        emit: EventSink,
    ) -> WorkflowOutput:
        if workflow_id not in COMPOSED_DECK_WORKFLOWS or steps != COMPOSED_STEPS:
            return self.execute(workflow_id, inputs, emit)
        return self._execute_composed_deck(workflow_id, inputs, emit)

    def _execute_composed_deck(
        self,
        workflow_id: str,
        inputs: dict[str, Any],
        emit: EventSink,
    ) -> WorkflowOutput:
        import json

        from ppt_master_service.conformance import InteractionRequest, WorkflowExecutionError
        from ppt_master_service.runtime import validate_stage_result
        from ppt_master_service.workflows import get_workflow_class

        job = self.store.create_job(inputs, workflow=workflow_id)
        workspace = self.runtime._create_workspace(job["job_id"])
        (workspace / "request.json").write_text(
            json.dumps(inputs, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.store.update_job(
            job["job_id"],
            status="running",
            progress=2,
            current_step="source_ingest",
            workspace=str(workspace),
            error=None,
            error_detail=None,
        )
        workflow = get_workflow_class(workflow_id)(self.runtime)
        emit("legacy_job_created", {"job_id": job["job_id"], "workflow_id": workflow_id})

        def current() -> dict[str, Any]:
            return self.store.get_job(job["job_id"]) or job

        def run_step(step: str, operation) -> dict[str, Any]:
            emit("subworkflow_started", {"workflow_id": step, "legacy_job_id": job["job_id"]})
            self.store.update_job(job["job_id"], current_step=step)
            try:
                result = validate_stage_result(step, operation())
            except WorkflowExecutionError as exc:
                detail = exc.detail.to_dict()
                emit("subworkflow_failed", {"workflow_id": step, "error": detail})
                raise LegacyWorkflowError(detail) from exc
            except Exception as exc:
                structured = getattr(exc, "detail", None)
                if isinstance(structured, dict):
                    detail = {
                        **structured,
                        "location": {
                            **dict(structured.get("location") or {}),
                            "workflow_id": step,
                            "legacy_job_id": job["job_id"],
                        },
                    }
                else:
                    detail = {
                        "code": f"{step}_failed",
                        "message": str(exc),
                        "location": {"workflow_id": step, "legacy_job_id": job["job_id"]},
                        "correction": {"action": "inspect_step_artifacts_and_retry", "step": step},
                    }
                emit("subworkflow_failed", {"workflow_id": step, "error": detail})
                raise LegacyWorkflowError(detail) from exc
            self.store.sync_project_artifacts(job["job_id"])
            emit("subworkflow_succeeded", {"workflow_id": step, "result": result})
            return result

        run_step("source_ingest", lambda: workflow.prepare(current()))

        def commit_design() -> dict[str, Any]:
            result = workflow.run(current())
            if result.get("status") != "waiting_user":
                raise RuntimeError("design stage did not produce an internal design commit")
            interaction = InteractionRequest(**result["interaction"])
            self.runtime.interactions.pause(job["job_id"], interaction, dict(result["checkpoint"]))
            self._accept_internal_design(current())
            committed = current()
            return {
                "ok": True,
                "stage": "design_commit",
                "interaction_id": interaction.interaction_id,
                "strategy_digest": interaction.strategy_digest,
                "source": "agent_defaults",
                "review_deferred": True,
            }

        run_step("design_commit", commit_design)

        def generate_svg() -> dict[str, Any]:
            result = workflow.run(current())
            if result.get("status") in {
                "waiting_user",
                "waiting_for_image_generation",
                "waiting_for_llm_svg_generation",
            }:
                raise RuntimeError(
                    result.get("message")
                    or f"SVG generation stopped with {result.get('status')}"
                )
            return result

        run_step("svg_generation", generate_svg)

        def review_deck() -> dict[str, Any]:
            review = validate_stage_result("review", workflow.review(current()))
            self.store.update_job(job["job_id"], review=review)
            return {"ok": True, "stage": "quality_repair", "review": review}

        run_step("quality_repair", review_deck)

        def export_deck() -> dict[str, Any]:
            result = validate_stage_result("export", workflow.export(current()))
            if result.get("output_file"):
                self.store.update_job(job["job_id"], output_file=result["output_file"])
            return result

        run_step("pptx_export", export_deck)
        run_step("review_prepare", lambda: {"ok": True, "stage": "review_prepare"})

        self.store.update_job(
            job["job_id"], status="success", progress=100, current_step="complete"
        )
        finished = current()
        output = finished.get("output_file")
        if not output:
            raise LegacyWorkflowError(
                {
                    "code": "pptx_artifact_missing",
                    "message": "composed workflow completed without a PPTX artifact",
                    "location": {"legacy_job_id": job["job_id"]},
                    "correction": {"action": "rerun_pptx_export"},
                }
            )
        artifacts = {"pptx": str(output)}
        if finished.get("project_path"):
            artifacts["project"] = str(finished["project_path"])
        return WorkflowOutput(
            workflow_id,
            artifacts,
            {"legacy_job_id": job["job_id"], "review": finished.get("review")},
        )

    def cancel(self, execution_id: str) -> None:
        job = self.store.get_job(execution_id)
        if job:
            self.store.update_job(execution_id, status="cancelled", current_step="cancelled")

    def _accept_internal_design(self, job: dict[str, Any]) -> None:
        from ppt_master_service.schemas import InteractionResponseRequest

        interaction = job.get("interaction") or {}
        response = InteractionResponseRequest(
            action="confirm",
            revision=int(interaction["revision"]),
            strategy_digest=str(interaction["strategy_digest"]),
            confirmations=dict(interaction["confirmations"]),
            generation_mode="continuous",
        )
        self.runtime.submit_interaction(job["job_id"], interaction["interaction_id"], response)


class LegacyWorkflowError(RuntimeError):
    def __init__(self, detail: dict[str, Any]) -> None:
        self.detail = detail
        super().__init__(str(detail.get("message") or detail.get("code") or "legacy workflow failed"))
