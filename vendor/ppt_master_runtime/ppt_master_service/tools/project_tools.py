from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path


REPO_ROOT = Path(os.environ.get("PPT_MASTER_ROOT") or Path(__file__).resolve().parents[2]).resolve()
SERVICE_DIR = REPO_ROOT / "ppt_master_service"
SCRIPT_DIR = REPO_ROOT / "skills" / "ppt-master" / "scripts"


@dataclass
class ToolResult:
    command: list[str]
    cwd: str
    returncode: int
    stdout: str
    stderr: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class ToolExecutionError(RuntimeError):
    def __init__(self, message: str, result: ToolResult | None = None) -> None:
        super().__init__(message)
        self.result = result


def run_script(
    args: list[str],
    cwd: Path | None = None,
    timeout: int = 300,
    check: bool = True,
) -> ToolResult:
    command = [sys.executable, *args]
    run_cwd = cwd or SCRIPT_DIR
    completed = subprocess.run(
        command,
        cwd=str(run_cwd),
        text=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    result = ToolResult(
        command=command,
        cwd=str(run_cwd),
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )
    if check and completed.returncode != 0:
        details = (completed.stderr or completed.stdout or "").strip()
        raise ToolExecutionError(details or f"tool failed: {' '.join(command)}", result)
    return result


def create_project(
    job_id: str,
    canvas_format: str = "ppt169",
    projects_dir: Path | None = None,
) -> str:
    target_projects = Path(projects_dir or (SERVICE_DIR / "jobs" / job_id / "workspace" / "projects")).expanduser()
    if not target_projects.is_absolute():
        target_projects = Path.cwd() / target_projects
    target_projects = target_projects.resolve()
    target_projects.mkdir(parents=True, exist_ok=True)
    before = {path.resolve() for path in target_projects.glob("*") if path.is_dir()}
    project_name = f"job_{job_id[:8]}"
    result = run_script(
        [
            str(SCRIPT_DIR / "project_manager.py"),
            "init",
            project_name,
            "--format",
            canvas_format,
            "--dir",
            str(target_projects),
        ],
        timeout=120,
    )
    after = [
        path
        for path in target_projects.glob("*")
        if path.is_dir() and path.resolve() not in before
    ]
    if not after:
        match = re.search(r"Project initialized:\s*(.+)", result.stdout)
        if match:
            candidate = Path(match.group(1).strip())
            if candidate.exists():
                return str(candidate)
        raise ToolExecutionError("project_manager.py did not create a project directory", result)
    return str(max(after, key=lambda path: path.stat().st_mtime))


def import_sources(project_path: str, source_files: list[str]) -> dict[str, object]:
    if not source_files:
        return {"project_path": project_path, "sources": [], "tool": None}
    result = run_script(
        [
            str(SCRIPT_DIR / "project_manager.py"),
            "import-sources",
            project_path,
            *source_files,
            "--copy",
        ],
        timeout=900,
    )
    return {
        "project_path": project_path,
        "sources": source_files,
        "tool": result.to_dict(),
    }


def validate_project(project_path: str) -> dict[str, object]:
    result = run_script(
        [str(SCRIPT_DIR / "project_manager.py"), "validate", project_path],
        timeout=120,
        check=False,
    )
    return {
        "project_path": project_path,
        "passed": result.returncode == 0,
        "tool": result.to_dict(),
    }


def write_tool_result(path: Path, result: ToolResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
