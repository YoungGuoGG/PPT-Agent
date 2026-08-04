from __future__ import annotations

from pathlib import Path

from .agent_phase_runner import AgentPhaseRunner
from .conformance import AgentPhaseRequest, AgentPhaseResult
from .template_guided_phase_runner import TemplateGuidedPhaseRunner
from .tools.template_guided_policy import load_template_context


class PhaseRunnerRouter:
    """Select a job-local Agent phase profile from deterministic project context."""

    def __init__(self, base: AgentPhaseRunner) -> None:
        self.base = base
        self._template_runners: dict[tuple[str, str], TemplateGuidedPhaseRunner] = {}

    def run_phase(
        self,
        request: AgentPhaseRequest,
        *,
        stop_after_page: int | None = None,
    ) -> AgentPhaseResult:
        runner = self._runner_for(request)
        return runner.run_phase(request, stop_after_page=stop_after_page)

    def materialize_notes(self, job_id: str, project: Path) -> Path:
        return self.base.materialize_notes(job_id, project)

    def _runner_for(self, request: AgentPhaseRequest) -> AgentPhaseRunner:
        project = Path(request.project_path).expanduser().resolve()
        context_path = project / "templates" / "template_context.json"
        if not context_path.is_file():
            return self.base
        load_template_context(project)
        key = (request.job_id, str(project))
        runner = self._template_runners.get(key)
        if runner is None:
            runner = TemplateGuidedPhaseRunner.from_base(self.base)
            self._template_runners[key] = runner
        return runner
