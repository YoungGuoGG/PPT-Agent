from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from ..llm_client import call_llm_json
from ..repair.engine import RepairEngine
from ..repair.finding_compiler import compile_findings
from ..repair.model import OpenAICompatibleRepairModel
from ..repair.models import QualityFinding
from ..repair.recipes import REPAIR_RECIPES
from ..schemas import ReviewResult
from .project_tools import SCRIPT_DIR, ToolResult, run_script
from . import svg_compatibility


FILE_ERROR_RE = re.compile(r"^\[ERROR\]\s+([^\s]+\.svg)\s+-\s+Failed\s*$", re.MULTILINE)
FILE_SECTION_RE = re.compile(
    r"^\[(?:ERROR|WARN|OK)\]\s+([^\s]+\.svg)\s+-.*?(?=^\[(?:ERROR|WARN|OK)\]\s+[^\s]+\.svg\s+-|\Z)",
    re.MULTILINE | re.DOTALL,
)


def check_svg_page(project_path: str, svg_path: str) -> ReviewResult:
    """Check and repair one generated page before Executor advances."""
    project = Path(project_path)
    target = Path(svg_path)
    if not target.exists() or not target.is_file():
        return ReviewResult(passed=False, issues=[f"SVG file does not exist: {target}"])
    expected_parent = (project / "svg_output").resolve()
    if target.resolve().parent != expected_parent:
        return ReviewResult(passed=False, issues=[f"SVG file is outside project svg_output: {target}"])

    compatibility_stats = svg_compatibility.CompatibilityRepairStats(files_seen=1)
    if svg_compatibility.repair_svg_file(target, compatibility_stats):
        compatibility_stats.files_changed = 1
    result, report = _check_candidate_with_report(project, target, target.name)
    notes: list[str] = []
    if compatibility_stats.files_changed:
        notes.append("deterministic compatibility repair: " + compatibility_stats.summary())

    if result.returncode != 0:
        max_attempts = _env_int("PPT_MASTER_AI_REPAIR_MAX_ATTEMPTS", default=2, minimum=0, maximum=2)
        repair = repair_failed_svgs_with_ai(project, report, max_attempts=max_attempts)
        notes.extend(str(item) for item in repair["notes"])
        if repair["repaired"]:
            result, report = _check_candidate_with_report(project, target, target.name)

    if result.returncode != 0:
        findings = compile_findings(project, report)
        issues = [f"{finding.code}: {finding.message}" for finding in findings]
        if not issues:
            issues = [_tool_output(result) or "single-page SVG quality check failed"]
        return ReviewResult(passed=False, issues=issues, notes="\n".join(notes))
    return ReviewResult(passed=True, notes="\n".join(notes))


def check_svg_page_findings(
    project_path: str,
    svg_path: str,
) -> list[dict[str, Any]]:
    """Return deterministic, repair-ready findings without invoking AI repair."""
    project = Path(project_path)
    target = Path(svg_path)
    if not target.is_file():
        return [{
            "code": "SVG_FILE_MISSING",
            "severity": "error",
            "file": target.name,
            "message": "The generated SVG file does not exist.",
            "location": {"selector": "", "tag": "svg", "index": 0},
            "evidence": {"path": str(target)},
            "correction": {
                "reason": "No page artifact can be checked.",
                "required": ["Write exactly one SVG page at the expected path."],
                "allowed_operations": ["write_svg_page"],
                "must_preserve": [],
                "forbidden": [],
                "verification": ["the SVG file exists and parses"],
            },
        }]
    expected_parent = (project / "svg_output").resolve()
    if target.resolve().parent != expected_parent:
        return [{
            "code": "SVG_PATH_INVALID",
            "severity": "error",
            "file": target.name,
            "message": "The generated SVG is outside project/svg_output.",
            "location": {"selector": "", "tag": "svg", "index": 0},
            "evidence": {
                "path": str(target),
                "expected_parent": str(expected_parent),
            },
            "correction": {
                "reason": "The page path violates the project boundary.",
                "required": ["Write the page to project/svg_output/slide_NN.svg."],
                "allowed_operations": ["write_svg_page"],
                "must_preserve": [],
                "forbidden": ["outside_project_write"],
                "verification": ["the resolved parent equals project/svg_output"],
            },
        }]

    stats = svg_compatibility.CompatibilityRepairStats(files_seen=1)
    if svg_compatibility.repair_svg_file(target, stats):
        stats.files_changed = 1
    result, report = _check_candidate_with_report(
        project, target, target.name
    )
    if result.returncode == 0:
        return []
    return _serialize_findings_or_unclassified(
        project, report, result, default_file=target.name
    )


def check_svg_project_findings(project_path: str) -> list[dict[str, Any]]:
    """Return full-project findings without using the legacy stateless AI repair."""
    project = Path(project_path)
    svg_output = project / "svg_output"
    if not svg_output.is_dir() or not list(svg_output.glob("*.svg")):
        return [{
            "code": "SVG_PROJECT_EMPTY",
            "severity": "error",
            "file": "",
            "message": "The project has no SVG pages to validate.",
            "location": {"selector": "", "tag": "svg", "index": 0},
            "evidence": {"svg_output": str(svg_output)},
            "correction": {
                "reason": "Full-project validation requires generated pages.",
                "required": ["Generate the exact requested SVG page set first."],
                "allowed_operations": ["write_svg_page"],
                "must_preserve": [],
                "forbidden": [],
                "verification": ["rerun project quality"],
            },
        }]
    svg_compatibility.repair_project(project)
    result, report = _run_checker_with_report(project)
    if result.returncode == 0:
        return []
    return _serialize_findings_or_unclassified(
        project, report, result, default_file=""
    )


def _serialize_findings_or_unclassified(
    project: Path,
    report: dict[str, object],
    result: ToolResult,
    *,
    default_file: str,
) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for finding in compile_findings(project, report):
        recipe = REPAIR_RECIPES[finding.code].to_instruction()
        payload = finding.to_dict()
        payload["correction"] = {
            "reason": recipe["diagnosis"],
            "required": recipe["steps"],
            "allowed_operations": recipe["allowed_operations"],
            "must_preserve": recipe["must_preserve"],
            "forbidden": recipe["forbidden"],
            "verification": recipe["verification"],
        }
        payloads.append(payload)
    if payloads:
        return payloads
    first_result = next(
        (item for item in report.get("results") or [] if isinstance(item, dict)),
        {},
    )
    reported_file = default_file or Path(
        str(first_result.get("file") or "")
    ).name
    return [{
        "code": "SVG_QUALITY_UNCLASSIFIED",
        "severity": "error",
        "file": reported_file,
        "message": "The checker rejected this SVG scope but no repair recipe matched.",
        "location": {"selector": "", "tag": "svg", "index": 0},
        "evidence": {"checker_output": _tool_output(result)},
        "correction": {
            "reason": "The checker output is not yet classified.",
            "required": [
                "Inspect the attached checker output and regenerate only the reported page."
            ],
            "allowed_operations": ["repair_svg_page"],
            "must_preserve": ["facts", "page_number", "viewBox"],
            "forbidden": ["change_other_pages"],
            "verification": ["rerun the applicable SVG quality checker"],
        },
    }]


def check_svg_quality(project_path: str) -> ReviewResult:
    project = Path(project_path)
    svg_output = project / "svg_output"
    if not svg_output.exists() or not svg_output.is_dir():
        return ReviewResult(
            passed=False,
            issues=[f"svg_output directory does not exist: {svg_output}"],
        )

    svg_files = sorted(svg_output.glob("*.svg"))
    if not svg_files:
        return ReviewResult(
            passed=False,
            issues=[f"no SVG files found in: {svg_output}"],
        )

    empty_files = [str(path) for path in svg_files if path.stat().st_size <= 0]
    if empty_files:
        return ReviewResult(
            passed=False,
            issues=[f"empty SVG file: {path}" for path in empty_files],
            notes=f"checked {len(svg_files)} SVG file(s)",
        )

    checker_runs: list[str] = []
    repair_notes: list[str] = []
    compatibility = svg_compatibility.repair_project(project)
    result, checker_report = _run_checker_with_report(project)
    checker_output = _tool_output(result)
    checker_runs.append(checker_output)

    max_attempts = _env_int("PPT_MASTER_AI_REPAIR_MAX_ATTEMPTS", default=2, minimum=0, maximum=2)
    if result.returncode != 0 and max_attempts > 0:
        repair = repair_failed_svgs_with_ai(
            project,
            checker_report,
            max_attempts=max_attempts,
        )
        repair_notes.extend(repair["notes"])
        if repair["repaired"]:
            compatibility_after_ai = svg_compatibility.repair_project(project)
            if compatibility_after_ai.files_changed:
                repair_notes.append("post-AI deterministic repair: " + compatibility_after_ai.summary())
            result, checker_report = _run_checker_with_report(project)
            checker_output = _tool_output(result)
            checker_runs.append(checker_output)

    issues: list[str] = []
    if result.returncode != 0:
        issues.append("svg_quality_checker.py failed after AI repair")
        if checker_output:
            issues.append(checker_output)

    notes_parts = [f"checked {len(svg_files)} SVG file(s)"]
    if compatibility.files_changed:
        notes_parts.append("deterministic compatibility repair: " + compatibility.summary())
    if compatibility.warnings:
        notes_parts.append("compatibility repair warnings:\n" + "\n".join(compatibility.warnings))
    if repair_notes:
        notes_parts.append("AI repair:\n" + "\n".join(repair_notes))
    for index, output in enumerate(checker_runs, start=1):
        if output:
            notes_parts.append(f"quality run {index}:\n{output}")

    return ReviewResult(
        passed=not issues,
        issues=issues,
        notes="\n".join(notes_parts).strip(),
    )


def repair_failed_svgs_with_ai(
    project: Path,
    checker_report: dict[str, object],
    *,
    max_attempts: int,
) -> dict[str, object]:
    findings = compile_findings(project, checker_report)
    if not findings:
        return {
            "repaired": [],
            "notes": ["checker failure did not compile to a supported repair recipe; AI repair skipped"],
        }

    repaired: list[str] = []
    notes: list[str] = []
    grouped: dict[str, list[QualityFinding]] = {}
    for finding in findings:
        grouped.setdefault(Path(finding.file).name, []).append(finding)

    engine = RepairEngine(
        model=OpenAICompatibleRepairModel(call_llm_json),
        checker=_check_candidate_findings,
    )
    for filename, file_findings in grouped.items():
        source = project / "svg_output" / filename
        if not source.exists() or not source.is_file():
            notes.append(f"{filename}: skipped because the failed SVG file was not found")
            continue
        if source.stat().st_size > _env_int(
            "PPT_MASTER_AI_REPAIR_MAX_SVG_CHARS",
            default=80000,
            minimum=1000,
            maximum=300000,
        ):
            notes.append(f"{filename}: skipped because it exceeds the AI repair size limit")
            continue
        outcome = engine.repair_page(
            project,
            source,
            file_findings,
            page_context=_repair_page_context(project, filename),
            max_attempts=max_attempts,
        )
        if outcome.status == "repaired":
            repaired.append(filename)
            notes.append(f"{filename}: constrained AI patch passed validation and replaced the original")
        else:
            reasons = [attempt.rejection for attempt in outcome.attempts if attempt.rejection]
            notes.append(f"{filename}: AI repair rejected: " + " | ".join(reasons))
    return {"repaired": repaired, "notes": notes}


def _repair_page_context(project: Path, filename: str) -> dict[str, object]:
    return {
        "file": filename,
        "spec_lock": _read_optional(project / "spec_lock.md", limit=12000),
        "design_spec": _read_optional(project / "design_spec.md", limit=12000),
    }


def _check_candidate_findings(project: Path, candidate_path: Path) -> list[QualityFinding]:
    result, report = _check_candidate_with_report(project, candidate_path, candidate_path.name)
    if result.returncode == 0:
        return []
    for item in report.get("results") or []:
        if isinstance(item, dict):
            item["path"] = str(candidate_path)
    return compile_findings(project, report)


def _run_checker(project: Path) -> ToolResult:
    return run_script(
        [str(SCRIPT_DIR / "svg_quality_checker.py"), str(project)],
        timeout=300,
        check=False,
    )


def _run_checker_with_report(project: Path) -> tuple[ToolResult, dict[str, object]]:
    report_dir = project / "repair" / "findings"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / "quality_latest.json"
    result = run_script(
        [
            str(SCRIPT_DIR / "svg_quality_checker.py"),
            str(project),
            "--json-output",
            str(report_path),
        ],
        timeout=300,
        check=False,
    )
    if report_path.exists():
        try:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                return result, payload
        except json.JSONDecodeError:
            pass
    return result, _report_from_checker_output(_tool_output(result))


def _report_from_checker_output(output: str) -> dict[str, object]:
    results: list[dict[str, object]] = []
    for match in FILE_SECTION_RE.finditer(output):
        section = match.group(0)
        filename = match.group(1)
        errors = [
            line.strip()[len("[ERROR]") :].strip()
            for line in section.splitlines()
            if line.strip().startswith("[ERROR]") and not line.strip().endswith("- Failed")
        ]
        warnings = [
            line.strip()[len("[WARN]") :].strip()
            for line in section.splitlines()
            if line.strip().startswith("[WARN]") and not line.strip().endswith("with warnings)")
        ]
        results.append({"file": filename, "errors": errors, "warnings": warnings})
    return {"schema": "pptmaster.quality.v1", "summary": {}, "results": results}


def _check_candidate(project: Path, candidate_path: Path, filename: str) -> ToolResult:
    with tempfile.TemporaryDirectory(prefix="pptmaster-ai-svg-check-") as tmp:
        candidate_project = Path(tmp)
        svg_output = candidate_project / "svg_output"
        svg_output.mkdir(parents=True)
        shutil.copy2(candidate_path, svg_output / filename)
        for name in ("spec_lock.md", "design_spec.md"):
            source = project / name
            if source.exists() and source.is_file():
                shutil.copy2(source, candidate_project / name)
        for name in ("images", "assets", "templates", "sources"):
            source = project / name
            if source.exists() and source.is_dir():
                shutil.copytree(source, candidate_project / name)
        return _run_checker(candidate_project)


def _check_candidate_with_report(
    project: Path,
    candidate_path: Path,
    filename: str,
) -> tuple[ToolResult, dict[str, object]]:
    with tempfile.TemporaryDirectory(prefix="pptmaster-ai-svg-check-") as tmp:
        candidate_project = Path(tmp)
        svg_output = candidate_project / "svg_output"
        svg_output.mkdir(parents=True)
        shutil.copy2(candidate_path, svg_output / filename)
        for name in ("spec_lock.md", "design_spec.md"):
            source = project / name
            if source.exists() and source.is_file():
                shutil.copy2(source, candidate_project / name)
        for name in ("images", "assets", "templates", "sources"):
            source = project / name
            if source.exists() and source.is_dir():
                shutil.copytree(source, candidate_project / name)
        return _run_checker_with_report(candidate_project)


def _failed_svg_errors(output: str) -> dict[str, str]:
    failed_names = set(FILE_ERROR_RE.findall(output))
    sections = {
        match.group(1): match.group(0).strip()
        for match in FILE_SECTION_RE.finditer(output)
        if match.group(1) in failed_names
    }
    return {name: sections.get(name, f"[ERROR] {name} - Failed") for name in sorted(failed_names)}


def _tool_output(result: ToolResult) -> str:
    return "\n".join(part for part in (result.stdout, result.stderr) if part).strip()


def _read_optional(path: Path, *, limit: int) -> str:
    if not path.exists() or not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")[:limit]


def _env_int(name: str, *, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))
