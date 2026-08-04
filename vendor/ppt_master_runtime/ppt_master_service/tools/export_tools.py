from __future__ import annotations

from pathlib import Path
from typing import Callable

from .project_tools import SCRIPT_DIR, ToolExecutionError, ToolResult, run_script


LogCallback = Callable[[str, ToolResult], None]


def _run_step(name: str, args: list[str], log: LogCallback | None, timeout: int = 900) -> ToolResult:
    result = run_script(args, timeout=timeout)
    if log:
        log(name, result)
    return result


def export_project_to_pptx(project_path: str, log: LogCallback | None = None) -> str:
    project = Path(project_path)
    if not project.exists() or not project.is_dir():
        raise ToolExecutionError(f"project path does not exist: {project_path}")

    _run_step(
        "total_md_split",
        [str(SCRIPT_DIR / "total_md_split.py"), project_path],
        log,
        timeout=300,
    )
    _run_step(
        "finalize_svg",
        [str(SCRIPT_DIR / "finalize_svg.py"), project_path],
        log,
        timeout=900,
    )
    _run_step(
        "svg_to_pptx",
        [str(SCRIPT_DIR / "svg_to_pptx.py"), project_path],
        log,
        timeout=1200,
    )

    exports_dir = project / "exports"
    candidates = sorted(exports_dir.glob("*.pptx"), key=lambda path: path.stat().st_mtime)
    if not candidates:
        raise ToolExecutionError(f"svg_to_pptx.py completed but no PPTX was found under {exports_dir}")
    return str(candidates[-1])


def split_notes(project_path: str) -> ToolResult:
    return run_script([str(SCRIPT_DIR / "total_md_split.py"), project_path])


def finalize_svg(project_path: str) -> ToolResult:
    return run_script([str(SCRIPT_DIR / "finalize_svg.py"), project_path])


def export_pptx(project_path: str, *extra_args: str) -> ToolResult:
    return run_script([str(SCRIPT_DIR / "svg_to_pptx.py"), project_path, *extra_args], timeout=900)
