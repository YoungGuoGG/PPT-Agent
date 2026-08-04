from __future__ import annotations

import json
import traceback
from pathlib import Path
from typing import Any

from .agent_phase_runner import AgentPhaseRunner
from .conformance import (
    AgentPhaseRequest,
    InteractionRequest,
    InteractionSubmission,
    WorkflowExecutionError,
    WorkflowMaturity,
    audit_maturity,
    get_workflow_definition,
)
from .interaction_coordinator import InteractionCoordinator
from .job_store import JobStore, OUTPUTS_DIR, PACKAGE_DIR
from .llm_runtime import build_design_interaction
from .intake_classifier import classify_auto_request
from .provider_config import initialize_provider_environment
from .preview_runtime import PreviewRuntime
from .phase_runner_router import PhaseRunnerRouter
from .schemas import GeneratePPTRequest, InteractionResponseRequest
from .security import validate_request_paths
from .workflow_router import route_workflow
from .workflows import get_workflow_class


class Runtime:
    def __init__(
        self,
        store: JobStore | None = None,
        *,
        interaction_coordinator: InteractionCoordinator | None = None,
        phase_runner: Any = None,
        preview_runtime: Any = None,
        enforce_maturity: bool = True,
    ) -> None:
        self.provider_settings = initialize_provider_environment(Path(__file__).resolve())
        self.store = store or JobStore()
        self.interactions = interaction_coordinator or InteractionCoordinator(self.store)
        self.phase_runner = (
            phase_runner
            if phase_runner is not None
            else PhaseRunnerRouter(AgentPhaseRunner(self.store))
        )
        self.preview_runtime = preview_runtime or PreviewRuntime(self.store)
        self.enforce_maturity = enforce_maturity
        self.workspace_root = self.store.jobs_dir
        self.outputs_root = OUTPUTS_DIR
        self.outputs_root.mkdir(parents=True, exist_ok=True)

    def create_job(self, request: GeneratePPTRequest) -> dict[str, Any]:
        validate_request_paths(request)
        request = classify_auto_request(request)
        validate_request_paths(request)
        workflow = route_workflow(request)
        job = self.store.create_job(request, workflow=workflow)
        self.store.append_log(job["job_id"], "workflow routed", workflow=workflow)
        if request.intake_plan:
            self.store.append_log(job["job_id"], "intake classified", intake_plan=request.intake_plan)
        return job

    def run_job(self, job_id: str) -> None:
        job = self.store.get_job(job_id)
        if job is None:
            raise KeyError(f"job not found: {job_id}")

        workflow_name = job.get("workflow") or "document_to_ppt"
        checkpoint = dict(job.get("checkpoint") or {})
        completed_runtime_steps = set(checkpoint.get("completed_runtime_steps") or [])
        workspace = self._create_workspace(job_id)
        request_path = workspace / "request.json"
        request_path.write_text(
            json.dumps(job.get("request", {}), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        self.store.update_job(
            job_id,
            status="running",
            progress=5,
            current_step="initializing workspace",
            workflow=workflow_name,
            workspace=str(workspace),
            error=None,
            error_traceback=None,
        )
        self.log(job_id, "job running", workflow=workflow_name, workspace=str(workspace))

        try:
            definition = get_workflow_definition(workflow_name)
            maturity_issues = audit_maturity(definition)
            if self.enforce_maturity and (
                definition.maturity is not WorkflowMaturity.COMPLETE or maturity_issues
            ):
                issue_codes = ", ".join(issue.code for issue in maturity_issues)
                raise RuntimeError(
                    f"workflow is not executable: {workflow_name} "
                    f"({definition.maturity.value}{'; ' + issue_codes if issue_codes else ''})"
                )
            workflow_class = get_workflow_class(workflow_name)
            workflow = workflow_class(self)
            current_job = self.store.get_job(job_id) or job

            progress_by_step = {
                "prepare": 10,
                "run": 30,
                "review": 82,
                "export": 92,
            }
            for step_name in ("prepare", "run", "review", "export"):
                if step_name in completed_runtime_steps:
                    self.log(job_id, f"workflow step skipped from checkpoint: {step_name}")
                    current_job = self.store.get_job(job_id) or current_job
                    continue
                self.store.update_job(
                    job_id,
                    progress=progress_by_step[step_name],
                    current_step=step_name,
                )
                self.log(job_id, f"workflow step start: {step_name}")
                result = validate_stage_result(
                    step_name,
                    getattr(workflow, step_name)(current_job),
                )
                self.log(job_id, f"workflow step complete: {step_name}", result=result)
                self.store.sync_project_artifacts(job_id)
                current_job = self.store.get_job(job_id) or current_job
                if result.get("status") == "waiting_user":
                    interaction = InteractionRequest(**result["interaction"])
                    self.interactions.pause(
                        job_id,
                        interaction,
                        dict(result["checkpoint"]),
                    )
                    self.log(
                        job_id,
                        "job waiting for user",
                        interaction_id=interaction.interaction_id,
                        interaction_type=interaction.type,
                    )
                    return
                if step_name == "review":
                    self.store.update_job(job_id, review=result)
                    current_job = self.store.get_job(job_id) or current_job
                if result.get("status") == "waiting_for_llm_svg_generation":
                    self.store.update_job(
                        job_id,
                        status="waiting_for_llm_svg_generation",
                        current_step="waiting_for_llm_svg_generation",
                        error=result.get("message"),
                    )
                    self.log(job_id, "job waiting for LLM SVG generation", reason=result.get("message"))
                    return
                if result.get("status") == "waiting_for_image_generation":
                    self.store.update_job(
                        job_id,
                        status="waiting_for_image_generation",
                        current_step="waiting_for_image_generation",
                        error=result.get("message"),
                        error_detail=result.get("error_detail"),
                    )
                    self.log(
                        job_id,
                        "job waiting for image generation",
                        reason=result.get("message"),
                    )
                    return
                completed_runtime_steps.add(step_name)
                checkpoint = dict((self.store.get_job(job_id) or {}).get("checkpoint") or {})
                checkpoint["completed_runtime_steps"] = [
                    name
                    for name in ("prepare", "run", "review", "export")
                    if name in completed_runtime_steps
                ]
                self.store.update_job(job_id, checkpoint=checkpoint)
                if step_name == "export" and result.get("output_file"):
                    self.store.update_job(job_id, output_file=result["output_file"])
                    current_job = self.store.get_job(job_id) or current_job

            final_job = self.store.get_job(job_id) or current_job
            self.store.update_job(
                job_id,
                status="success",
                progress=100,
                current_step="complete",
                output_file=final_job.get("output_file"),
                error=None,
                error_traceback=None,
            )
            self.log(job_id, "job success")
        except WorkflowExecutionError as exc:
            trace = traceback.format_exc()
            self.store.update_job(
                job_id,
                status="failed",
                current_step="failed",
                error=str(exc),
                error_detail=exc.detail.to_dict(),
                error_traceback=trace,
            )
            self.log(
                job_id,
                "job failed",
                error=str(exc),
                error_detail=exc.detail.to_dict(),
            )
        except Exception as exc:
            trace = traceback.format_exc()
            self.store.update_job(
                job_id,
                status="failed",
                current_step="failed",
                error=str(exc),
                error_detail=None,
                error_traceback=trace,
            )
            self.log(
                job_id,
                "job failed",
                error=str(exc),
                traceback=trace,
            )

    def submit_interaction(
        self,
        job_id: str,
        interaction_id: str,
        response: InteractionResponseRequest,
    ) -> InteractionSubmission:
        job = self.store.get_job(job_id)
        if job is None:
            raise KeyError(f"job not found: {job_id}")

        def revise(
            current: dict[str, Any],
            feedback: str,
        ) -> InteractionRequest:
            if self.phase_runner is None:
                raise RuntimeError("Agent phase runner is not configured")
            project_value = job.get("project_path")
            if not project_value:
                raise RuntimeError("revision requires an initialized project path")
            project = Path(str(project_value))
            previous = dict(current["checkpoint"]["strategy_draft"])
            phase = self.phase_runner.run_phase(
                AgentPhaseRequest(
                    job_id=job_id,
                    phase="strategist_draft",
                    project_path=str(project),
                    checkpoint={
                        "request": job["request"],
                        "previous_strategy": previous,
                        "revision_feedback": feedback,
                        "revision": int(current["revision"]) + 1,
                    },
                )
            )
            strategy = dict(phase.output["strategy_draft"])
            replacement = build_design_interaction(
                strategy,
                revision=int(current["revision"]) + 1,
            )
            revised_checkpoint = {
                **current["checkpoint"],
                "strategy_draft": strategy,
                "strategy_digest": replacement.strategy_digest,
                "interaction_id": replacement.interaction_id,
            }
            current["checkpoint"] = revised_checkpoint
            return replacement

        return self.interactions.submit(
            job_id,
            interaction_id,
            response,
            revise=revise,
        )

    def log(self, job_id: str, message: str, **fields: Any) -> None:
        self.store.append_log(job_id, message, **fields)

    def _create_workspace(self, job_id: str) -> Path:
        workspace = self.workspace_root / job_id / "workspace"
        for rel in ("inputs", "projects", "outputs", "logs"):
            (workspace / rel).mkdir(parents=True, exist_ok=True)
        return workspace


def validate_stage_result(step_name: str, result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise RuntimeError(f"stage {step_name} returned a non-object result")
    if result.get("stub") is True:
        raise RuntimeError(f"stage {step_name} returned a stub result")
    if result.get("ok") is False:
        message = str(result.get("message") or "no failure message")
        raise RuntimeError(f"stage {step_name} reported failure: {message}")
    return result
