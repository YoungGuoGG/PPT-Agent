from __future__ import annotations

import os
import runpy
import sys
import traceback
from pathlib import Path


def configure_paths() -> None:
    resource_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2])).resolve()
    backend = resource_root / "backend"
    ppt_master = resource_root / "vendor" / "ppt_master_runtime"
    script_root = ppt_master / "skills" / "ppt-master" / "scripts"
    for path in (backend, ppt_master, script_root):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    os.environ.setdefault("PPT_AGENT_RESOURCE_ROOT", str(resource_root))
    os.environ.setdefault("PPT_MASTER_ROOT", str(ppt_master))
    os.environ.setdefault("PPT_AGENT_PACKAGED", "1")
    local_app_data = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    os.environ.setdefault("PPT_AGENT_DATA_ROOT", str(local_app_data / "OfficeAgent" / "PPTAgentV2"))


def run_bundled_tool_if_requested() -> bool:
    if len(sys.argv) < 2 or not sys.argv[1].lower().endswith(".py"):
        return False
    scripts_root = (
        Path(os.environ["PPT_MASTER_ROOT"])
        / "skills"
        / "ppt-master"
        / "scripts"
    ).resolve()
    script = Path(sys.argv[1]).expanduser().resolve()
    try:
        script.relative_to(scripts_root)
    except ValueError as exc:
        raise RuntimeError(f"refusing to run a script outside the bundled tool root: {script}") from exc
    if not script.is_file():
        raise FileNotFoundError(f"bundled tool does not exist: {script}")
    sys.argv = [str(script), *sys.argv[2:]]
    runpy.run_path(str(script), run_name="__main__")
    return True


def main() -> None:
    configure_paths()
    if run_bundled_tool_if_requested():
        return
    import uvicorn
    from ppt_agent.gateway.app import app

    uvicorn.run(
        app,
        host="127.0.0.1",
        port=int(os.environ.get("PPT_AGENT_PORT", "8765")),
        log_level="warning",
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        data_root = Path(
            os.environ.get("PPT_AGENT_DATA_ROOT")
            or Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
            / "OfficeAgent"
            / "PPTAgentV2"
        )
        log_dir = data_root / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "sidecar-crash.log").write_text(traceback.format_exc(), encoding="utf-8")
        raise
