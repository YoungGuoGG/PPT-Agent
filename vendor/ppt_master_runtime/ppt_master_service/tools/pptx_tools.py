from __future__ import annotations

import json
import re
from pathlib import Path

from .project_tools import SCRIPT_DIR, ToolExecutionError, run_script


def template_fill_analyze(pptx_path: str, output_json: str):
    return run_script(
        [str(SCRIPT_DIR / "template_fill_pptx.py"), "analyze", pptx_path, "-o", output_json],
        timeout=300,
    )


def template_fill_scaffold(
    library_json: str,
    output_json: str,
    slides: str | None = None,
    include_empty: bool = False,
):
    args = [str(SCRIPT_DIR / "template_fill_pptx.py"), "scaffold", library_json, "-o", output_json]
    if slides:
        args.extend(["--slides", slides])
    if include_empty:
        args.append("--include-empty")
    return run_script(args, timeout=300)


def template_fill_check(library_json: str, plan_json: str, output_json: str | None = None):
    args = [str(SCRIPT_DIR / "template_fill_pptx.py"), "check-plan", library_json, plan_json]
    if output_json:
        args.extend(["-o", output_json])
    return run_script(args, timeout=300)


def template_fill_apply(
    pptx_path: str,
    plan_json: str,
    output_pptx: str,
    transition: str | None = None,
    transition_duration: float | None = None,
):
    args = [str(SCRIPT_DIR / "template_fill_pptx.py"), "apply", pptx_path, plan_json, "-o", output_pptx]
    if transition:
        args.extend(["--transition", transition])
    if transition_duration is not None:
        args.extend(["--transition-duration", str(transition_duration)])
    result = run_script(args, timeout=900)
    return result


def template_fill_find_output(requested_output: str, result_stderr: str = "") -> str:
    match = re.search(r"Template-filled PPTX -> (.+\.pptx)", result_stderr)
    if match:
        candidate = Path(match.group(1).strip())
        if candidate.exists():
            return str(candidate)

    requested = Path(requested_output)
    if requested.exists():
        return str(requested)

    exports_dir = requested.parent
    if exports_dir.exists():
        candidates = sorted(exports_dir.glob(f"{requested.stem}_*.pptx"), key=lambda path: path.stat().st_mtime)
        if candidates:
            return str(candidates[-1])

    raise ToolExecutionError(f"template_fill_pptx apply did not produce output PPTX: {requested_output}")


def template_fill_validate(output_pptx: str, validation_md: str):
    return run_script(
        [str(SCRIPT_DIR / "source_to_md" / "ppt_to_md.py"), output_pptx, "-o", validation_md],
        timeout=900,
        check=False,
    )


def load_json(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: str | Path, payload: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
