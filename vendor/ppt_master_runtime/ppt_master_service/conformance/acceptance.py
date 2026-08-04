from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .models import WorkflowDefinition, WorkflowMaturity


EVIDENCE_SCHEMA = "pptmaster.workflow-acceptance.v1"
REPO_ROOT = Path(os.environ.get("PPT_MASTER_ROOT") or Path(__file__).resolve().parents[2]).resolve()


@dataclass(frozen=True)
class AcceptanceArtifact:
    path: str
    checksum: str


@dataclass(frozen=True)
class AcceptanceEvidence:
    workflow_id: str
    contract_version: str
    fixture_version: str
    layers: dict[str, bool]
    artifacts: list[AcceptanceArtifact | str]
    verified_at: str
    provider: dict[str, str] = field(default_factory=dict)
    fixture_path: str = ""
    fixture_checksum: str = ""
    gates: dict[str, bool] = field(default_factory=dict)
    implementation_checksums: dict[str, str] = field(default_factory=dict)
    started_at: str = ""
    duration_seconds: float = 0.0
    schema: str = EVIDENCE_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "AcceptanceEvidence":
        if payload.get("schema") != EVIDENCE_SCHEMA:
            raise ValueError(f"unsupported acceptance evidence schema: {payload.get('schema')}")
        raw_artifacts = payload.get("artifacts")
        if not isinstance(raw_artifacts, list):
            raise ValueError("acceptance evidence artifacts must be a list")
        artifacts: list[AcceptanceArtifact | str] = []
        for item in raw_artifacts:
            if isinstance(item, str):
                artifacts.append(item)
            elif isinstance(item, dict):
                path = str(item.get("path") or "")
                checksum = str(item.get("checksum") or "")
                if not path or not checksum.startswith("sha256:"):
                    raise ValueError("acceptance artifact requires path and sha256 checksum")
                artifacts.append(AcceptanceArtifact(path=path, checksum=checksum))
            else:
                raise ValueError("acceptance artifact must be a string or object")
        provider = payload.get("provider") or {}
        if not isinstance(provider, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in provider.items()):
            raise ValueError("acceptance provider metadata must be a string map")
        forbidden = {"api_key", "authorization", "token", "secret"}
        if any(any(marker in key.lower() for marker in forbidden) for key in provider):
            raise ValueError("acceptance provider metadata contains a secret-like key")
        return cls(
            workflow_id=str(payload.get("workflow_id") or ""),
            contract_version=str(payload.get("contract_version") or ""),
            fixture_version=str(payload.get("fixture_version") or ""),
            layers=_bool_map(payload.get("layers"), "layers"),
            artifacts=artifacts,
            verified_at=str(payload.get("verified_at") or ""),
            provider=dict(provider),
            fixture_path=str(payload.get("fixture_path") or ""),
            fixture_checksum=str(payload.get("fixture_checksum") or ""),
            gates=_bool_map(payload.get("gates") or {}, "gates"),
            implementation_checksums=_string_map(
                payload.get("implementation_checksums") or {},
                "implementation_checksums",
            ),
            started_at=str(payload.get("started_at") or ""),
            duration_seconds=float(payload.get("duration_seconds") or 0.0),
        )


@dataclass(frozen=True)
class ConformanceIssue:
    code: str
    message: str


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def sha256_tree(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(relative)
        digest.update(b"\0")
        digest.update(sha256_file(path).encode("ascii"))
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def write_acceptance_evidence(path: Path, evidence: AcceptanceEvidence) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(evidence.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def load_acceptance_evidence(path: Path) -> AcceptanceEvidence:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("acceptance evidence must be a JSON object")
    return AcceptanceEvidence.from_dict(payload)


def audit_maturity(
    definition: WorkflowDefinition,
    evidence: AcceptanceEvidence | None = None,
    *,
    repo_root: Path | None = None,
) -> list[ConformanceIssue]:
    if definition.maturity in {
        WorkflowMaturity.STUB,
        WorkflowMaturity.PARTIAL,
        WorkflowMaturity.UNAVAILABLE,
    }:
        return []

    root = (repo_root or REPO_ROOT).resolve()
    issues: list[ConformanceIssue] = []
    if definition.adapter is None:
        issues.append(ConformanceIssue("adapter_missing", f"{definition.id} requires an adapter at {definition.maturity.value} maturity"))
    if definition.fixture is None:
        issues.append(ConformanceIssue("fixture_missing", f"{definition.id} requires a fixture at {definition.maturity.value} maturity"))

    linked = definition.acceptance_evidence is not None
    if evidence is None and linked:
        evidence_path = _safe_repo_path(root, definition.acceptance_evidence or "")
        if not evidence_path.is_file():
            issues.append(ConformanceIssue("acceptance_evidence_missing", f"linked evidence does not exist: {definition.acceptance_evidence}"))
            return issues
        try:
            evidence = load_acceptance_evidence(evidence_path)
        except (ValueError, json.JSONDecodeError) as exc:
            issues.append(ConformanceIssue("acceptance_evidence_invalid", str(exc)))
            return issues
    if evidence is None:
        issues.append(ConformanceIssue("acceptance_evidence_missing", f"{definition.id} has no acceptance evidence"))
        return issues

    if evidence.workflow_id != definition.id:
        issues.append(ConformanceIssue("evidence_workflow_mismatch", f"evidence belongs to {evidence.workflow_id}, expected {definition.id}"))
    for layer in ("contract", "adapter", "offline_e2e"):
        if not evidence.layers.get(layer, False):
            issues.append(ConformanceIssue(f"{layer}_acceptance_missing", f"{definition.id} is missing passing {layer} acceptance"))
    if not evidence.artifacts:
        issues.append(ConformanceIssue("artifact_evidence_missing", f"{definition.id} has no accepted artifact evidence"))
    for gate, passed in evidence.gates.items():
        if not passed:
            issues.append(ConformanceIssue("quality_gate_failed", f"{definition.id} failed quality gate: {gate}"))
    missing_gates = (
        sorted(set(definition.quality_gates) - set(evidence.gates))
        if definition.maturity is WorkflowMaturity.COMPLETE
        else []
    )
    if missing_gates:
        issues.append(
            ConformanceIssue(
                "quality_gate_evidence_missing",
                f"{definition.id} has no acceptance result for declared gates: {', '.join(missing_gates)}",
            )
        )
    if definition.maturity is WorkflowMaturity.COMPLETE and definition.provider_required:
        if not evidence.layers.get("provider", False):
            issues.append(ConformanceIssue("provider_acceptance_missing", f"{definition.id} requires passing provider acceptance"))
        elif not evidence.provider:
            issues.append(ConformanceIssue("provider_metadata_missing", f"{definition.id} requires provider metadata"))

    if linked:
        _audit_linked_checksums(definition, evidence, root, issues)
    return issues


def audit_runtime_bindings(
    definitions: dict[str, WorkflowDefinition],
    runtime_registry: dict[str, type[Any]],
) -> list[ConformanceIssue]:
    issues: list[ConformanceIssue] = []
    for workflow_id, workflow_class in runtime_registry.items():
        definition = definitions.get(workflow_id)
        if definition is None:
            issues.append(ConformanceIssue("runtime_definition_missing", f"runtime workflow has no registry definition: {workflow_id}"))
            continue
        if workflow_class.is_stub is True:
            if definition.maturity is not WorkflowMaturity.STUB:
                issues.append(ConformanceIssue("runtime_stub_maturity_mismatch", f"stub runtime workflow must declare stub maturity: {workflow_id}"))
        elif definition.adapter != workflow_id:
            issues.append(ConformanceIssue("runtime_adapter_mismatch", f"runtime workflow adapter must match its id: {workflow_id}"))
    return issues


def _audit_linked_checksums(
    definition: WorkflowDefinition,
    evidence: AcceptanceEvidence,
    root: Path,
    issues: list[ConformanceIssue],
) -> None:
    if definition.fixture:
        fixture = _safe_repo_path(root, definition.fixture)
        if not fixture.is_dir() or not evidence.fixture_checksum:
            issues.append(ConformanceIssue("fixture_evidence_missing", f"fixture or checksum is missing: {definition.fixture}"))
        elif sha256_tree(fixture) != evidence.fixture_checksum:
            issues.append(ConformanceIssue("fixture_checksum_mismatch", f"fixture changed after acceptance: {definition.fixture}"))
    for relative, checksum in evidence.implementation_checksums.items():
        path = _safe_repo_path(root, relative)
        if not path.is_file() or sha256_file(path) != checksum:
            issues.append(ConformanceIssue("implementation_checksum_mismatch", f"implementation changed after acceptance: {relative}"))
    for artifact in evidence.artifacts:
        if not isinstance(artifact, AcceptanceArtifact):
            issues.append(ConformanceIssue("artifact_checksum_missing", f"linked evidence has an unchecked artifact: {artifact}"))
            continue
        path = Path(artifact.path)
        if not path.is_absolute():
            path = root / path
        if not path.is_file():
            issues.append(ConformanceIssue("artifact_missing", f"accepted artifact is missing: {artifact.path}"))
        elif sha256_file(path) != artifact.checksum:
            issues.append(ConformanceIssue("artifact_checksum_mismatch", f"accepted artifact changed: {artifact.path}"))


def _safe_repo_path(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"acceptance path is outside repository: {relative}") from exc
    return candidate


def _bool_map(value: Any, field_name: str) -> dict[str, bool]:
    if not isinstance(value, dict) or not all(isinstance(key, str) and isinstance(item, bool) for key, item in value.items()):
        raise ValueError(f"acceptance {field_name} must be a boolean map")
    return dict(value)


def _string_map(value: Any, field_name: str) -> dict[str, str]:
    if not isinstance(value, dict) or not all(isinstance(key, str) and isinstance(item, str) for key, item in value.items()):
        raise ValueError(f"acceptance {field_name} must be a string map")
    return dict(value)
