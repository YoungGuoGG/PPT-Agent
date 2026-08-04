from __future__ import annotations

from contextvars import ContextVar
from pathlib import Path
from typing import Any

from ..providers import ModelProvider
from ..skills import SkillLoader
from ..workflows.contracts import EventSink, WorkflowOutput
from ..workflows.legacy_executor import COMPOSED_STEPS, LegacyPptMasterExecutor


PLAN_TO_LEGACY = {
    "source.ingest": "source_ingest",
    "design.compose": "design_commit",
    "svg.generate": "svg_generation",
    "quality.check_repair": "quality_repair",
    "pptx.export": "pptx_export",
    "review.prepare": "review_prepare",
}
LEGACY_TO_PLAN = {value: key for key, value in PLAN_TO_LEGACY.items()}


class PptMasterToolExecutor(LegacyPptMasterExecutor):
    """V2 Tool Executor backed by selected, vendored PPT Master algorithms."""

    def __init__(
        self,
        ppt_runtime_root: Path,
        data_root: Path,
        *,
        skills: SkillLoader,
        provider: ModelProvider | None,
    ) -> None:
        self.skills = skills
        self.provider = provider
        self._active_style_contract: ContextVar[str] = ContextVar(
            "ppt_agent_style_contract", default=""
        )
        super().__init__(ppt_runtime_root, data_root, provider=provider)
        if provider is not None:
            from ppt_master_service.agent_phase_runner import AgentPhaseRunner
            from ppt_master_service.phase_runner_router import PhaseRunnerRouter

            self.runtime.phase_runner = PhaseRunnerRouter(
                AgentPhaseRunner(self.store, call_messages=self._complete_design_messages)
            )

    def execute_plan(
        self,
        workflow_id: str,
        inputs: dict[str, Any],
        steps: list[str],
        emit: EventSink,
    ) -> WorkflowOutput:
        requested = set(steps)
        missing = set(PLAN_TO_LEGACY) - requested
        if missing:
            raise ValueError(f"plan is missing required generation tools: {sorted(missing)}")

        def mapped_emit(event: str, data: dict[str, Any]) -> None:
            legacy_id = str(data.get("workflow_id") or "")
            plan_id = LEGACY_TO_PLAN.get(legacy_id)
            if not plan_id:
                emit(event, data)
                return
            payload = {**data, "workflow_id": plan_id, "adapter_workflow_id": legacy_id}
            emit(event, payload)
            if event == "subworkflow_succeeded" and legacy_id == "source_ingest":
                if "style.retrieve" in requested:
                    emit("subworkflow_started", {"workflow_id": "style.retrieve", "adapter_workflow_id": legacy_id})
                    emit("subworkflow_succeeded", {"workflow_id": "style.retrieve", "adapter_workflow_id": legacy_id})
            if event == "subworkflow_succeeded" and legacy_id == "design_commit":
                for synthetic in ("assets.prepare",):
                    if synthetic in requested:
                        emit("subworkflow_started", {"workflow_id": synthetic, "adapter_workflow_id": legacy_id})
                        emit("subworkflow_succeeded", {"workflow_id": synthetic, "adapter_workflow_id": legacy_id})

        token = self._active_style_contract.set(str(inputs.get("style_contract") or ""))
        try:
            return super().execute_plan(workflow_id, inputs, list(COMPOSED_STEPS), mapped_emit)
        finally:
            self._active_style_contract.reset(token)

    def _complete_design_messages(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        if self.provider is None:
            raise RuntimeError("model provider is not configured")
        system = str(messages[0].get("content") or "") if messages else ""
        tool_id = "design.compose" if "strateg" in system.lower() else "svg.generate"
        skill_prompt = self.skills.design_for(tool_id).prompt()
        style_contract = self._active_style_contract.get()
        if style_contract:
            skill_prompt += "\n\n" + style_contract
        augmented = [dict(message) for message in messages]
        if augmented and augmented[0].get("role") == "system":
            augmented[0]["content"] = skill_prompt + "\n\n" + system
        else:
            augmented.insert(0, {"role": "system", "content": skill_prompt})
        return self.provider.complete_messages(augmented)
