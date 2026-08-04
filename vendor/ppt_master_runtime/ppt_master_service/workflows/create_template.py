from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from .scripted import REPO_ROOT, ScriptedWorkflow


class CreateTemplateWorkflow(ScriptedWorkflow):
    name = "create_template"
    is_stub = False

    def prepare(self, job: dict[str, Any]) -> dict[str, Any]:
        request = job.get("request") or {}
        template_id = str(request.get("template_id") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{1,63}", template_id):
            self.fail(step="analyze", code="template_id_invalid", message="template_id must be a 2-64 character filesystem-safe slug", location={"field": "template_id", "value": template_id}, correction={"action": "use_ascii_slug", "example": "strategy-blue"})
        kind = str(request.get("template_kind") or "deck")
        if kind not in {"deck", "layout"}:
            self.fail(step="analyze", code="template_kind_invalid", message="template_kind must be deck or layout", location={"field": "template_kind", "value": kind}, correction={"action": "choose_supported_kind", "allowed": ["deck", "layout"]})
        kind_dir = "decks" if kind == "deck" else "layouts"
        target = REPO_ROOT / "skills" / "ppt-master" / "templates" / kind_dir / template_id
        if target.exists() and not request.get("replace"):
            self.fail(step="analyze", code="template_exists", message=f"template already exists: {template_id}", location={"path": str(target)}, correction={"action": "set_replace_true_or_choose_new_id"})
        source_value = request.get("template_source")
        source = Path(source_value).expanduser().resolve() if source_value else None
        if source and not source.exists():
            self.fail(step="analyze", code="template_source_missing", message=f"template source does not exist: {source}", location={"path": str(source)}, correction={"action": "select_existing_pptx_svg_or_directory"})
        mode = str(request.get("replication_mode") or ("mirror" if source and source.suffix.lower() == ".pptx" else "standard"))
        if mode not in {"standard", "fidelity", "mirror"}:
            self.fail(step="analyze", code="replication_mode_invalid", message="replication_mode is invalid", location={"value": mode}, correction={"action": "choose_supported_mode", "allowed": ["standard", "fidelity", "mirror"]})
        commit = {
            "template_id": template_id,
            "display_name": str(request.get("display_name") or template_id.replace("-", " ").title()),
            "kind": kind,
            "kind_dir": kind_dir,
            "target": str(target),
            "source": str(source) if source else None,
            "replication_mode": mode,
            "canvas_format": str(request.get("canvas_format") or "ppt169"),
            "primary_color": str(request.get("primary_color") or "#0066CC"),
            "summary": str(request.get("summary") or "Reusable presentation template"),
            "assumptions": ["canvas defaults to ppt169", "missing style fields use restrained system defaults"],
        }
        path = self.write_json(Path(job["workspace"]) / "analysis" / "template_commit.json", commit)
        return {"ok": True, "stage": "prepare", "design_commit": str(path), **commit}

    def run(self, job: dict[str, Any]) -> dict[str, Any]:
        commit = self._commit(job)
        target = Path(commit["target"])
        target.mkdir(parents=True, exist_ok=True)
        source = Path(commit["source"]) if commit["source"] else None
        files: list[Path]
        if source and source.suffix.lower() == ".pptx":
            imported = Path(job["workspace"]) / "analysis" / "template_import"
            self.run_script(job, "pptx_template_import.py", str(source), "-o", str(imported), "--inheritance-mode", "both", step="import_template")
            source_dir = imported / "svg-flat"
            files = self._copy_svg_roster(source_dir, target, target / "assets", imported / "assets")
        elif source and source.is_dir():
            files = self._copy_svg_roster(source, target, target / "assets", None)
        elif source and source.suffix.lower() == ".svg":
            files = self._copy_svg_roster(source.parent, target, target / "assets", None, only=source)
        else:
            files = self._write_default_roster(target, commit["primary_color"])
        (target / "design_spec.md").write_text(self._design_spec(commit, files), encoding="utf-8")
        return {"ok": True, "stage": "run", "template_package": str(target), "svg_files": [path.name for path in files]}

    def review(self, job: dict[str, Any]) -> dict[str, Any]:
        commit = self._commit(job)
        result = self.run_script(job, "svg_quality_checker.py", commit["target"], "--template-mode", "--format", commit["canvas_format"], step="validate")
        report = self.write_json(Path(commit["target"]) / "validation_report.json", {"passed": True, "stdout": result["stdout"]})
        return {"ok": True, "stage": "review", "passed": True, "template_validation_report": str(report)}

    def export(self, job: dict[str, Any]) -> dict[str, Any]:
        commit = self._commit(job)
        self.run_script(job, "register_template.py", commit["template_id"], "--kind", commit["kind"], step="register")
        return {"ok": True, "stage": "export", "output_file": str(Path(commit["target"]) / "design_spec.md")}

    def _commit(self, job: dict[str, Any]) -> dict[str, Any]:
        return json.loads((Path(job["workspace"]) / "analysis" / "template_commit.json").read_text(encoding="utf-8"))

    @staticmethod
    def _copy_svg_roster(source_dir: Path, target: Path, asset_target: Path, asset_source: Path | None, only: Path | None = None) -> list[Path]:
        sources = [only] if only else sorted(source_dir.glob("*.svg"))
        if not sources:
            raise ValueError(f"template source has no SVG files: {source_dir}")
        if asset_source and asset_source.is_dir():
            shutil.copytree(asset_source, asset_target, dirs_exist_ok=True)
        files = []
        for index, source in enumerate(sources, start=1):
            page_type = "cover" if index == 1 else "ending" if index == len(sources) else "content"
            destination = target / f"{index:03d}_{page_type}.svg"
            text = source.read_text(encoding="utf-8", errors="replace")
            text = text.replace("../assets/", "assets/").replace("..\\assets\\", "assets/")
            destination.write_text(text, encoding="utf-8")
            files.append(destination)
        return files

    @staticmethod
    def _write_default_roster(target: Path, color: str) -> list[Path]:
        pages = [
            ("01_cover.svg", "{{TITLE}}", "{{SUBTITLE}}"),
            ("02_chapter.svg", "{{CHAPTER_TITLE}}", "{{CHAPTER_SUBTITLE}}"),
            ("03_content.svg", "{{PAGE_TITLE}}", "{{CONTENT_AREA}}"),
            ("04_ending.svg", "{{ENDING_TITLE}}", "{{CONTACT_INFO}}"),
        ]
        files = []
        for name, title, body in pages:
            svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720"><rect width="1280" height="720" fill="#FFFFFF"/><rect x="0" y="0" width="18" height="720" fill="{color}"/><text x="88" y="170" font-family="Arial" font-size="44" font-weight="600" fill="#1D1D1F">{title}</text><text x="88" y="250" font-family="Arial" font-size="24" fill="#6E6E73">{body}</text></svg>'''
            path = target / name
            path.write_text(svg, encoding="utf-8")
            files.append(path)
        return files

    @staticmethod
    def _design_spec(commit: dict[str, Any], files: list[Path]) -> str:
        id_field = "deck_id" if commit["kind"] == "deck" else "layout_id"
        roster = "\n".join(f"| {path.name} | Reusable {path.stem.split('_', 1)[-1]} page |" for path in files)
        return f'''---
{id_field}: {commit["template_id"]}
kind: {commit["kind"]}
summary: "{commit["summary"]}"
canvas_format: {commit["canvas_format"]}
page_count: {len(files)}
primary_color: "{commit["primary_color"]}"
replication_mode: {commit["replication_mode"]}
---

# {commit["display_name"]}

## I. Template Overview
{commit["summary"]}

## II. Color Scheme
- Primary: {commit["primary_color"]}
- Text: #1D1D1F
- Background: #FFFFFF

## III. Signature Design Elements
- Restrained hierarchy, clear content zones, and reusable page roles.

## V. Page Roster
| File | Purpose |
|---|---|
{roster}
'''
