from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


EventSink = Callable[[str, dict[str, Any]], None]


@dataclass(frozen=True)
class WorkflowOutput:
    workflow_id: str
    artifacts: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


class WorkflowExecutor(Protocol):
    """Deep seam: orchestration knows this interface, never ppt-master scripts."""

    def execute(self, workflow_id: str, inputs: dict[str, Any], emit: EventSink) -> WorkflowOutput:
        ...

    def cancel(self, execution_id: str) -> None:
        ...


class PlanWorkflowExecutor(WorkflowExecutor, Protocol):
    """Optional richer boundary for executors that dispatch composed subworkflows."""

    def execute_plan(
        self,
        workflow_id: str,
        inputs: dict[str, Any],
        steps: list[str],
        emit: EventSink,
    ) -> WorkflowOutput:
        ...
