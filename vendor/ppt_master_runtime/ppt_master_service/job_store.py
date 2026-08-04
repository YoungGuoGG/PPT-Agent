from __future__ import annotations

from contextlib import contextmanager
import json
import re
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_DIR = Path(__file__).resolve().parent
JOBS_DIR = PACKAGE_DIR / "jobs"
LOGS_DIR = PACKAGE_DIR / "logs"
OUTPUTS_DIR = PACKAGE_DIR / "outputs"
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "dict"):
        return value.dict()
    return value


class JobStore:
    def __init__(self, jobs_dir: Path = JOBS_DIR, logs_dir: Path = LOGS_DIR) -> None:
        self.jobs_dir = jobs_dir
        self.logs_dir = logs_dir
        self._locks_guard = threading.Lock()
        self._job_locks: dict[str, threading.RLock] = {}
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def locked_job(self, job_id: str):
        if not _SAFE_ID.fullmatch(job_id):
            raise ValueError(f"invalid job id: {job_id}")
        with self._locks_guard:
            lock = self._job_locks.setdefault(job_id, threading.RLock())
        with lock:
            yield

    def create_job(self, request: Any, workflow: str | None = None) -> dict[str, Any]:
        job_id = uuid.uuid4().hex
        now = utc_now()
        job = {
            "job_id": job_id,
            "workflow": workflow,
            "status": "queued",
            "progress": 0,
            "current_step": "queued",
            "created_at": now,
            "updated_at": now,
            "request": _jsonable(request),
            "workspace": None,
            "project_path": None,
            "output_file": None,
            "error": None,
            "error_detail": None,
            "review": None,
            "warnings": [],
            "pending_interaction_id": None,
            "interaction": None,
            "checkpoint": {},
            "preview": None,
        }
        self._write_job(job)
        self.append_log(job_id, "job created")
        return job

    def update_job(self, job_id: str, **updates: Any) -> dict[str, Any]:
        with self.locked_job(job_id):
            job = self.get_job(job_id)
            if job is None:
                raise KeyError(f"job not found: {job_id}")
            job.update({key: _jsonable(value) for key, value in updates.items()})
            if "status" in updates and "current_step" not in updates:
                job["current_step"] = str(updates["status"])
            job["updated_at"] = utc_now()
            self._write_job(job)
        self.sync_project_artifacts(job_id)
        return job

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        path = self._job_file(job_id)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def list_jobs(self) -> list[dict[str, Any]]:
        jobs: list[dict[str, Any]] = []
        for path in sorted(self.jobs_dir.glob("*/job.json")):
            try:
                jobs.append(json.loads(path.read_text(encoding="utf-8")))
            except json.JSONDecodeError:
                continue
        return jobs

    def append_log(self, job_id: str, message: str, **fields: Any) -> None:
        record = {"ts": utc_now(), "job_id": job_id, "message": message}
        record.update({key: _jsonable(value) for key, value in fields.items()})
        line = json.dumps(record, ensure_ascii=False)
        job_log = self._job_dir(job_id) / "logs.jsonl"
        events_log = self._job_dir(job_id) / "events.jsonl"
        global_log = self.logs_dir / f"{job_id}.jsonl"
        job_log.parent.mkdir(parents=True, exist_ok=True)
        with job_log.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        with events_log.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        with global_log.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def get_logs(self, job_id: str, limit: int = 50) -> list[str]:
        path = self._job_dir(job_id) / "events.jsonl"
        if not path.exists():
            path = self._job_dir(job_id) / "logs.jsonl"
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()
        return lines[-limit:]

    def get_last_event(self, job_id: str) -> dict[str, Any] | None:
        lines = self.get_logs(job_id, limit=1)
        if not lines:
            return None
        try:
            return json.loads(lines[-1])
        except json.JSONDecodeError:
            return {"message": lines[-1]}

    def save_interaction(
        self,
        job_id: str,
        interaction: dict[str, Any],
        *,
        pending: bool,
    ) -> dict[str, Any]:
        interaction_id = str(interaction.get("interaction_id") or "")
        if not _SAFE_ID.fullmatch(interaction_id):
            raise ValueError(f"invalid interaction id: {interaction_id}")
        with self.locked_job(job_id):
            job = self.get_job(job_id)
            if job is None:
                raise KeyError(f"job not found: {job_id}")
            path = self._job_dir(job_id) / "interactions" / f"{interaction_id}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            self._write_json_atomic(path, interaction)
            job["pending_interaction_id"] = interaction_id if pending else None
            job["interaction"] = interaction if pending else None
            job["updated_at"] = utc_now()
            self._write_job(job)
        return interaction

    def get_interaction(
        self,
        job_id: str,
        interaction_id: str,
    ) -> dict[str, Any] | None:
        if not _SAFE_ID.fullmatch(interaction_id):
            raise ValueError(f"invalid interaction id: {interaction_id}")
        path = self._job_dir(job_id) / "interactions" / f"{interaction_id}.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None

    def get_pending_interaction(self, job_id: str) -> dict[str, Any] | None:
        job = self.get_job(job_id)
        pending = str((job or {}).get("pending_interaction_id") or "")
        return self.get_interaction(job_id, pending) if pending else None

    def save_receipt(
        self,
        job_id: str,
        interaction_id: str,
        receipt: dict[str, Any],
    ) -> dict[str, Any]:
        if not _SAFE_ID.fullmatch(interaction_id):
            raise ValueError(f"invalid interaction id: {interaction_id}")
        path = self._job_dir(job_id) / "interactions" / f"{interaction_id}.receipt.json"
        with self.locked_job(job_id):
            if self.get_job(job_id) is None:
                raise KeyError(f"job not found: {job_id}")
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.is_file():
                existing = json.loads(path.read_text(encoding="utf-8"))
                if existing != receipt:
                    raise ValueError(
                        f"interaction {interaction_id} already has a different receipt"
                    )
                return existing
            self._write_json_atomic(path, receipt)
        return receipt

    def get_receipt(
        self,
        job_id: str,
        interaction_id: str,
    ) -> dict[str, Any] | None:
        if not _SAFE_ID.fullmatch(interaction_id):
            raise ValueError(f"invalid interaction id: {interaction_id}")
        path = self._job_dir(job_id) / "interactions" / f"{interaction_id}.receipt.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None

    def save_agent_session(
        self,
        job_id: str,
        phase: str,
        session: dict[str, Any],
    ) -> None:
        self._validate_agent_phase(phase)
        path = self._job_dir(job_id) / "agent_sessions" / f"{phase}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.locked_job(job_id):
            if self.get_job(job_id) is None:
                raise KeyError(f"job not found: {job_id}")
            self._write_json_atomic(path, session)

    def load_agent_session(self, job_id: str, phase: str) -> dict[str, Any] | None:
        self._validate_agent_phase(phase)
        path = self._job_dir(job_id) / "agent_sessions" / f"{phase}.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None

    def save_preview(self, job_id: str, preview: dict[str, Any]) -> None:
        self.update_job(job_id, preview=preview)

    def sync_project_artifacts(self, job_id: str) -> None:
        job = self.get_job(job_id)
        if not job:
            return
        project_path = job.get("project_path")
        if not project_path:
            return
        project = Path(project_path)
        if not project.exists():
            return
        job_dir = self._job_dir(job_id)
        for filename in ("design_spec.md", "spec_lock.md"):
            source = project / filename
            if source.exists() and source.is_file():
                shutil.copy2(source, job_dir / filename)

    def _write_job(self, job: dict[str, Any]) -> None:
        path = self._job_file(job["job_id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        self._write_json_atomic(path, job)
        self._write_artifact_files(job)

    def _write_artifact_files(self, job: dict[str, Any]) -> None:
        job_dir = self._job_dir(job["job_id"])
        job_dir.mkdir(parents=True, exist_ok=True)
        self._write_json(job_dir / "request.json", job.get("request") or {})
        self._write_json(
            job_dir / "workflow.json",
            {
                "job_id": job["job_id"],
                "workflow": job.get("workflow"),
                "project_path": job.get("project_path"),
                "workspace": job.get("workspace"),
            },
        )
        self._write_json(
            job_dir / "status.json",
            {
                "job_id": job["job_id"],
                "workflow": job.get("workflow"),
                "status": job.get("status"),
                "progress": job.get("progress", 0),
                "current_step": job.get("current_step"),
                "created_at": job.get("created_at"),
                "updated_at": job.get("updated_at"),
                "project_path": job.get("project_path"),
                "output_file": job.get("output_file"),
                "error": job.get("error"),
                "error_detail": job.get("error_detail"),
                "warnings": job.get("warnings") or [],
            },
        )
        if job.get("review") is not None:
            self._write_json(job_dir / "review.json", job["review"])
        if job.get("output_file"):
            (job_dir / "output_file").write_text(str(job["output_file"]), encoding="utf-8")
        if job.get("error"):
            content = str(job["error"]) + "\n"
            if job.get("error_traceback"):
                content += "\n" + str(job["error_traceback"])
            (job_dir / "error.log").write_text(content, encoding="utf-8")

    @staticmethod
    def _write_json(path: Path, payload: Any) -> None:
        JobStore._write_json_atomic(path, payload)

    @staticmethod
    def _write_json_atomic(path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(path)

    @staticmethod
    def _validate_agent_phase(phase: str) -> None:
        if phase not in {"strategist_draft", "executor"}:
            raise ValueError(f"unsupported agent phase: {phase}")

    def _job_dir(self, job_id: str) -> Path:
        if not _SAFE_ID.fullmatch(job_id):
            raise ValueError(f"invalid job id: {job_id}")
        return self.jobs_dir / job_id

    def _job_file(self, job_id: str) -> Path:
        return self._job_dir(job_id) / "job.json"
