from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class ElementLocation:
    element_id: str = ""
    group_id: str = ""
    tag: str = ""
    index: int = 0
    selector: str = ""
    snippet: str = ""
    attributes: dict[str, str] = field(default_factory=dict)
    related_elements: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class QualityFinding:
    code: str
    severity: str
    file: str
    message: str
    location: ElementLocation = field(default_factory=ElementLocation)
    evidence: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def simple(cls, code: str, file: str, message: str = "") -> "QualityFinding":
        return cls(code=code, severity="error", file=file, message=message or code)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["location"] = self.location.to_dict()
        return payload


@dataclass
class RepairRecipe:
    code: str
    mode: str
    diagnosis: str
    steps: list[str]
    allowed_operations: set[str]
    must_preserve: set[str]
    forbidden: set[str]
    verification: list[str]

    def to_instruction(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "mode": self.mode,
            "diagnosis": self.diagnosis,
            "steps": self.steps,
            "allowed_operations": sorted(self.allowed_operations),
            "must_preserve": sorted(self.must_preserve),
            "forbidden": sorted(self.forbidden),
            "verification": self.verification,
        }


@dataclass
class RepairAttempt:
    attempt: int
    patch: dict[str, Any] | None
    status: str
    rejection: str = ""
    remaining_findings: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RepairOutcome:
    status: str
    repaired_file: str | None
    remaining_findings: list[QualityFinding]
    attempts: list[RepairAttempt]
    requires_regeneration: bool = False
    requires_human: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "repaired_file": self.repaired_file,
            "remaining_findings": [finding.to_dict() for finding in self.remaining_findings],
            "attempts": [attempt.to_dict() for attempt in self.attempts],
            "requires_regeneration": self.requires_regeneration,
            "requires_human": self.requires_human,
        }
