from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .job_store import OUTPUTS_DIR, PACKAGE_DIR
from .tools.project_tools import REPO_ROOT


POWERPOINT_SUFFIXES = {".pptx", ".pptm", ".ppsx", ".ppsm", ".potx", ".potm"}


def local_dev_paths_enabled() -> bool:
    return os.environ.get("PPT_MASTER_LOCAL_DEV_PATHS", "1").lower() not in {"0", "false", "no"}


def allowed_input_roots() -> list[Path]:
    roots = [
        REPO_ROOT,
        PACKAGE_DIR / "jobs",
        OUTPUTS_DIR,
        REPO_ROOT / ".scratch",
    ]
    configured = os.environ.get("PPT_MASTER_ALLOWED_INPUT_DIRS", "")
    for item in configured.split(os.pathsep):
        if item.strip():
            roots.append(Path(item.strip()).expanduser())
    resolved: list[Path] = []
    for root in roots:
        try:
            resolved.append(root.resolve())
        except OSError:
            continue
    return resolved


def validate_request_paths(request: Any) -> None:
    payload = request.model_dump() if hasattr(request, "model_dump") else dict(request)
    for field in ("source_files", "brand_assets"):
        for value in payload.get(field) or []:
            validate_read_path(str(value), field=field)
    for field in ("template_pptx", "reference_pptx", "project_path"):
        value = payload.get(field)
        if value:
            validate_read_path(str(value), field=field)


def validate_read_path(value: str, field: str = "path") -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = REPO_ROOT / path
    resolved = path.resolve()
    if not _is_under_allowed_root(resolved):
        allowed = ", ".join(str(root) for root in allowed_input_roots())
        raise ValueError(f"{field} is outside allowed input directories: {value}. Allowed roots: {allowed}")
    return resolved


def safe_response_path(value: str | None) -> str | None:
    if not value:
        return value
    if local_dev_paths_enabled():
        return value
    path = Path(value)
    return path.name if path.suffix else "<hidden>"


def _is_under_allowed_root(path: Path) -> bool:
    for root in allowed_input_roots():
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False
