from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from ..conformance.models import (
    ErrorKind,
    SuggestedAction,
    WorkflowError,
    WorkflowExecutionError,
)
from .base import BaseWorkflow


REPO_ROOT = Path(os.environ.get("PPT_MASTER_ROOT") or Path(__file__).resolve().parents[2]).resolve()
SCRIPT_ROOT = REPO_ROOT / "skills" / "ppt-master" / "scripts"


class ScriptedWorkflow(BaseWorkflow):
    """Small adapter around the stable ppt-master script boundary."""

    def project_path(self, job: dict[str, Any]) -> Path:
        value = job.get("project_path") or (job.get("request") or {}).get("project_path")
        if not value:
            self.fail(
                step="prepare",
                code="project_path_missing",
                message=f"{self.name} requires project_path",
                location={"field": "project_path"},
                correction={"action": "supply_project_path", "expected": "existing PPT Master project directory"},
            )
        path = Path(str(value)).expanduser().resolve()
        if not path.is_dir():
            self.fail(
                step="prepare",
                code="project_path_invalid",
                message=f"project directory does not exist: {path}",
                location={"path": str(path)},
                correction={"action": "select_existing_directory"},
            )
        return path

    def run_script(self, job: dict[str, Any], script: str, *args: str, step: str) -> dict[str, Any]:
        command = [sys.executable, str(SCRIPT_ROOT / script), *map(str, args)]
        completed = subprocess.run(
            command,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        result = {
            "command": command,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
        self.runtime.log(job["job_id"], f"{self.name}: {step}", tool=result)
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "script failed").strip()
            self.fail(
                step=step,
                code=f"{self.name}_{step}_failed",
                message=detail,
                kind=ErrorKind.DEPENDENCY,
                location={"script": script, "project_path": str((job.get("request") or {}).get("project_path") or "")},
                correction={
                    "action": "repair_inputs_then_retry",
                    "command": command,
                    "stderr_tail": detail[-2000:],
                },
                retryable=True,
            )
        return result

    def write_json(self, path: Path, payload: Any) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def fail(
        self,
        *,
        step: str,
        code: str,
        message: str,
        location: dict[str, Any],
        correction: dict[str, Any],
        kind: ErrorKind = ErrorKind.ARTIFACT,
        retryable: bool = False,
    ) -> None:
        raise WorkflowExecutionError(
            WorkflowError(
                kind=kind,
                code=code,
                step=step,
                message=message,
                location=location,
                correction=correction,
                retryable=retryable,
                suggested_action=(
                    SuggestedAction.REPAIR if retryable else SuggestedAction.STOP
                ),
            )
        )

    @staticmethod
    def latest_pptx(project: Path) -> Path | None:
        files = sorted(
            (path for path in (project / "exports").glob("*.pptx") if path.is_file()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        return files[0] if files else None
