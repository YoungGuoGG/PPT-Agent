from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any
from uuid import uuid4

from .models import ArtifactRecord, ArtifactVisibility, RunStatus, WorkflowRun


_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


class FileRunStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def create_run(self, workflow_id: str, inputs: dict[str, Any]) -> WorkflowRun:
        run_id = uuid4().hex
        now = _now()
        run = WorkflowRun(
            run_id=run_id,
            workflow_id=workflow_id,
            status=RunStatus.QUEUED,
            current_step="",
            inputs=dict(inputs),
            created_at=now,
            updated_at=now,
        )
        run_dir = self._run_dir(run_id)
        run_dir.mkdir(parents=False, exist_ok=False)
        _write_json_atomic(run_dir / "run.json", run.to_dict())
        _write_json_atomic(run_dir / "artifacts.json", [])
        self.append_event(run_id, "run_created", {"workflow_id": workflow_id})
        return run

    def load_run(self, run_id: str) -> WorkflowRun:
        run_path = self._run_dir(run_id) / "run.json"
        payload = json.loads(run_path.read_text(encoding="utf-8"))
        return WorkflowRun.from_dict(payload)

    def save_checkpoint(self, run: WorkflowRun) -> None:
        run_path = self._run_dir(run.run_id) / "run.json"
        if not run_path.is_file():
            raise FileNotFoundError(f"unknown run: {run.run_id}")
        run.updated_at = _now()
        _write_json_atomic(run_path, run.to_dict())

    def append_event(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        run_dir = self._run_dir(run_id)
        if not (run_dir / "run.json").is_file():
            raise FileNotFoundError(f"unknown run: {run_id}")
        event = {
            "type": event_type,
            "timestamp": _now(),
            "payload": dict(payload),
        }
        with (run_dir / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        return event

    def load_events(self, run_id: str) -> list[dict[str, Any]]:
        events_path = self._run_dir(run_id) / "events.jsonl"
        if not events_path.exists():
            return []
        return [
            json.loads(line)
            for line in events_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def register_artifact(self, run_id: str, artifact: ArtifactRecord) -> None:
        _validate_artifact_path(artifact.path)
        run = self.load_run(run_id)
        artifacts = self.load_artifacts(run_id)
        existing = next(
            (item for item in artifacts if item.artifact_id == artifact.artifact_id),
            None,
        )
        if existing is not None:
            if existing != artifact:
                raise ValueError(
                    f"artifact id already registered with different content: {artifact.artifact_id}"
                )
            return

        artifacts.append(artifact)
        manifest_path = self._run_dir(run_id) / "artifacts.json"
        _write_json_atomic(manifest_path, [item.to_dict() for item in artifacts])
        run.artifacts.append(artifact.artifact_id)
        self.save_checkpoint(run)

    def load_artifacts(self, run_id: str) -> list[ArtifactRecord]:
        manifest_path = self._run_dir(run_id) / "artifacts.json"
        if not manifest_path.is_file():
            raise FileNotFoundError(f"artifact manifest missing for run: {run_id}")
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        artifacts: list[ArtifactRecord] = []
        for raw in payload:
            data = dict(raw)
            data["visibility"] = ArtifactVisibility(str(data["visibility"]))
            artifacts.append(ArtifactRecord(**data))
        return artifacts

    def _run_dir(self, run_id: str) -> Path:
        if not _RUN_ID_PATTERN.fullmatch(run_id):
            raise ValueError(f"invalid run id: {run_id}")
        return self.root / run_id


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json_atomic(path: Path, payload: Any) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _validate_artifact_path(path: str) -> None:
    relative = PurePosixPath(path)
    if not path or "\\" in path or relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"artifact path must be workspace-relative: {path}")
