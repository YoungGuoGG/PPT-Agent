from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class StringEnum(str, Enum):
    pass


class WorkflowMaturity(StringEnum):
    STUB = "stub"
    PARTIAL = "partial"
    CANDIDATE = "candidate"
    COMPLETE = "complete"
    UNAVAILABLE = "unavailable"


class InitialInteractionPolicy(StringEnum):
    """Controls whether a workflow may interrupt the first generation pass."""

    NEVER = "never"
    EXPLICIT_ONLY = "explicit_only"
    REVIEW_ONLY = "review_only"


class RunStatus(StringEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_USER = "waiting_user"
    WAITING_EXTERNAL = "waiting_external"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepStatus(StringEnum):
    COMPLETED = "completed"
    WAITING_USER = "waiting_user"
    WAITING_EXTERNAL = "waiting_external"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ErrorKind(StringEnum):
    TRANSPORT = "transport"
    PROTOCOL = "protocol"
    DEPENDENCY = "dependency"
    PERMISSION = "permission"
    ARTIFACT = "artifact"
    QUALITY = "quality"
    CANCELLED = "cancelled"


class SuggestedAction(StringEnum):
    RETRY = "retry"
    REPAIR = "repair"
    REGENERATE_STEP = "regenerate_step"
    REQUEST_USER = "request_user"
    INSTALL_DEPENDENCY = "install_dependency"
    STOP = "stop"


class ArtifactVisibility(StringEnum):
    USER = "user"
    INTERNAL = "internal"
    DEBUG = "debug"


@dataclass(frozen=True)
class WorkflowDefinition:
    id: str
    source_workflow: str
    maturity: WorkflowMaturity
    required_inputs: list[str]
    stages: list[str]
    interactions: list[str]
    required_artifacts: list[str]
    quality_gates: list[str]
    recovery_points: list[str]
    adapter: str | None
    fixture: str | None
    provider_required: bool = False
    acceptance_evidence: str | None = None
    contract_version: str = "1.0"
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    artifact_types: list[str] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=list)
    initial_interaction_policy: InitialInteractionPolicy = InitialInteractionPolicy.NEVER


@dataclass(frozen=True)
class WorkflowError:
    kind: ErrorKind
    code: str
    step: str
    message: str
    location: dict[str, Any] = field(default_factory=dict)
    correction: dict[str, Any] = field(default_factory=dict)
    retryable: bool = False
    suggested_action: SuggestedAction = SuggestedAction.STOP
    attempt: int = 1

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


class WorkflowExecutionError(RuntimeError):
    def __init__(self, detail: WorkflowError) -> None:
        super().__init__(detail.message)
        self.detail = detail


@dataclass(frozen=True)
class InteractionRequest:
    type: str
    title: str
    message: str
    fields: list[dict[str, Any]] = field(default_factory=list)
    interaction_id: str = ""
    revision: int = 1
    status: str = "pending"
    strategy_digest: str = ""
    confirmations: dict[str, dict[str, Any]] = field(default_factory=dict)
    split_mode_note: dict[str, Any] = field(default_factory=dict)
    actions: list[str] = field(default_factory=lambda: ["confirm", "revise"])

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


@dataclass(frozen=True)
class InteractionSubmission:
    accepted: bool
    action: str
    resume_required: bool
    interaction: InteractionRequest | None = None
    receipt: dict[str, Any] | None = None


@dataclass(frozen=True)
class AgentPhaseRequest:
    job_id: str
    phase: str
    project_path: str
    checkpoint: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentPhaseResult:
    status: str
    phase: str
    checkpoint: dict[str, Any] = field(default_factory=dict)
    output: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ArtifactRecord:
    artifact_id: str
    type: str
    role: str
    path: str
    mime_type: str
    producer_step: str
    checksum: str
    visibility: ArtifactVisibility
    verified: bool

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


@dataclass(frozen=True)
class StepResult:
    status: StepStatus
    step: str
    artifacts: list[ArtifactRecord] = field(default_factory=list)
    error: WorkflowError | None = None
    interaction: InteractionRequest | None = None
    metrics: dict[str, int | float | str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


@dataclass
class WorkflowRun:
    run_id: str
    workflow_id: str
    status: RunStatus
    current_step: str
    completed_steps: list[str] = field(default_factory=list)
    checkpoint: dict[str, Any] = field(default_factory=dict)
    inputs: dict[str, Any] = field(default_factory=dict)
    artifacts: list[str] = field(default_factory=list)
    last_error: dict[str, Any] | None = None
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "WorkflowRun":
        data = dict(payload)
        data["status"] = RunStatus(str(data["status"]))
        return cls(**data)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value
