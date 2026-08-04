from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StringEnum(str, Enum):
    pass


class SessionStatus(StringEnum):
    QUEUED = "queued"
    RUNNING = "running"
    REVIEW_READY = "review_ready"
    REVISING = "revising"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepStatus(StringEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class ReviewScope(StringEnum):
    WHOLE_DECK = "whole_deck"
    SELECTED_PAGES = "selected_pages"


@dataclass
class PlanStep:
    id: str
    workflow_id: str
    title: str
    status: StepStatus = StepStatus.PENDING
    depends_on: list[str] = field(default_factory=list)
    input_bindings: dict[str, Any] = field(default_factory=dict)
    output_artifacts: list[str] = field(default_factory=list)
    error: dict[str, Any] | None = None


@dataclass
class ExecutionPlan:
    id: str
    session_id: str
    workflow_id: str
    steps: list[PlanStep]
    goal: str = ""
    assumptions: list[str] = field(default_factory=list)
    style_selection: dict[str, Any] = field(default_factory=dict)
    schema: str = "ppt-agent.execution-plan.v1"
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


@dataclass
class AgentSession:
    id: str
    conversation_id: str
    user_message_id: str
    status: SessionStatus
    workflow_id: str
    prompt: str
    attachments: list[str]
    options: dict[str, Any]
    plan_id: str
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    error: dict[str, Any] | None = None

    @classmethod
    def create(cls, *, conversation_id: str, user_message_id: str, workflow_id: str, prompt: str, attachments: list[str], options: dict[str, Any], plan_id: str) -> "AgentSession":
        return cls(uuid4().hex, conversation_id, user_message_id, SessionStatus.QUEUED, workflow_id, prompt, attachments, options, plan_id)

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


@dataclass
class DeckVersion:
    id: str
    session_id: str
    version: int
    pptx_path: str
    project_path: str | None
    parent_id: str | None
    scope: ReviewScope
    pages: list[int]
    instruction: str
    status: str = "candidate"
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value
