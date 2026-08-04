from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .models import InitialInteractionPolicy, WorkflowDefinition, WorkflowMaturity


REGISTRY_SCHEMA = "pptmaster.workflow-registry.v1"
REPO_ROOT = Path(os.environ.get("PPT_MASTER_ROOT") or Path(__file__).resolve().parents[2]).resolve()
DEFAULT_REGISTRY_PATH = REPO_ROOT / "workflows" / "registry.json"


class RegistryValidationError(ValueError):
    pass


def load_registry(
    path: Path | None = None,
    repo_root: Path | None = None,
) -> dict[str, WorkflowDefinition]:
    root = (repo_root or REPO_ROOT).resolve()
    registry_path = (path or DEFAULT_REGISTRY_PATH).resolve()
    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    if payload.get("schema") != REGISTRY_SCHEMA:
        raise RegistryValidationError(
            f"unsupported workflow registry schema: {payload.get('schema')}"
        )
    raw_items = payload.get("workflows")
    if not isinstance(raw_items, list):
        raise RegistryValidationError("workflow registry 'workflows' must be a list")

    seen_ids: set[str] = set()
    for raw in raw_items:
        if not isinstance(raw, dict):
            raise RegistryValidationError("each workflow definition must be an object")
        workflow_id = str(raw.get("id") or "")
        if not workflow_id:
            raise RegistryValidationError("workflow id is required")
        if workflow_id in seen_ids:
            raise RegistryValidationError(f"duplicate workflow id: {workflow_id}")
        seen_ids.add(workflow_id)

    definitions: dict[str, WorkflowDefinition] = {}
    for raw in raw_items:
        workflow_id = str(raw["id"])
        source = _safe_source(root, str(raw.get("source_workflow") or ""))
        if not source.is_file():
            raise RegistryValidationError(f"source workflow does not exist: {source}")
        definitions[workflow_id] = WorkflowDefinition(
            id=workflow_id,
            source_workflow=str(source.relative_to(root)).replace("\\", "/"),
            maturity=WorkflowMaturity(str(raw.get("maturity"))),
            required_inputs=_string_list(raw, "required_inputs"),
            stages=_nonempty_string_list(raw, "stages"),
            interactions=_string_list(raw, "interactions"),
            required_artifacts=_nonempty_string_list(raw, "required_artifacts"),
            quality_gates=_nonempty_string_list(raw, "quality_gates"),
            recovery_points=_string_list(raw, "recovery_points"),
            adapter=str(raw["adapter"]) if raw.get("adapter") else None,
            fixture=str(raw["fixture"]) if raw.get("fixture") else None,
            provider_required=bool(raw.get("provider_required", False)),
            acceptance_evidence=(
                str(_safe_source(root, str(raw["acceptance_evidence"])).relative_to(root)).replace("\\", "/")
                if raw.get("acceptance_evidence")
                else None
            ),
            contract_version=str(raw.get("contract_version") or "1.0"),
            input_schema=_object(raw, "input_schema") or _derived_schema(raw.get("required_inputs") or []),
            output_schema=_object(raw, "output_schema") or _derived_schema(raw.get("required_artifacts") or []),
            artifact_types=_string_list(raw, "artifact_types") if "artifact_types" in raw else list(raw.get("required_artifacts") or []),
            capabilities=_string_list(raw, "capabilities") if "capabilities" in raw else [],
            initial_interaction_policy=InitialInteractionPolicy(
                str(raw.get("initial_interaction_policy") or "never")
            ),
        )
    return definitions


def get_workflow_definition(workflow_id: str) -> WorkflowDefinition:
    definitions = load_registry()
    try:
        return definitions[workflow_id]
    except KeyError as exc:
        raise KeyError(f"unknown workflow: {workflow_id}") from exc


def _safe_source(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise RegistryValidationError(
            f"source workflow is outside repository: {relative}"
        ) from exc
    return candidate


def _string_list(payload: dict[str, Any], field: str) -> list[str]:
    value = payload.get(field)
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise RegistryValidationError(f"{field} must be a list of non-empty strings")
    return list(value)


def _nonempty_string_list(payload: dict[str, Any], field: str) -> list[str]:
    value = _string_list(payload, field)
    if not value:
        raise RegistryValidationError(f"{field} must not be empty")
    return value


def _object(payload: dict[str, Any], field: str) -> dict[str, Any]:
    value = payload.get(field, {})
    if not isinstance(value, dict):
        raise RegistryValidationError(f"{field} must be an object")
    return dict(value)


def _derived_schema(fields: list[str]) -> dict[str, Any]:
    names = [str(item) for item in fields]
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": names,
        "properties": {name: {} for name in names},
        "additionalProperties": True,
    }
