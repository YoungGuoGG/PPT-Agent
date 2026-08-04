from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from ..conformance import (
    ErrorKind,
    SuggestedAction,
    WorkflowError,
    WorkflowExecutionError,
    sha256_file,
)
from ..llm_client import call_llm_json
from ..tools import export_tools, project_tools, quality_tools, source_tools, template_tools
from .base import BaseWorkflow
from .mirror_template_adapter import (
    MirrorRoster,
    analyze_mirror_roster,
    apply_mirror_plan,
    plan_mirror_content,
    resolve_deck_template,
    structural_fingerprint,
    write_mirror_roster,
)


class MirrorTemplateWorkflow(BaseWorkflow):
    name = "mirror_template"
    is_stub = False

    def prepare(self, job: dict[str, Any]) -> dict[str, Any]:
        job_id = job["job_id"]
        self.runtime.store.update_job(job_id, status="loading_mirror_template")
        request = job.get("request", {})
        template_id = request.get("mirror_template_id") or request.get("template_id")
        if not template_id:
            raise self._error(
                ErrorKind.PROTOCOL,
                "mirror.template.id_missing",
                "load_template",
                "mirror_template requires template_id or mirror_template_id",
                {},
            )

        metadata_dir = Path(getattr(self.runtime, "template_metadata_dir", template_tools.METADATA_DIR))
        template_root = Path(getattr(self.runtime, "template_root", template_tools.TEMPLATE_ROOT))
        resolved = resolve_deck_template(
            str(template_id),
            metadata_dir=metadata_dir,
            template_root=template_root,
        )
        projects_dir = Path(job["workspace"]) / "projects" if job.get("workspace") else None
        project_path = project_tools.create_project(
            job_id,
            request.get("canvas_format") or "ppt169",
            projects_dir,
        )
        project = Path(project_path)
        self.runtime.store.update_job(job_id, project_path=str(project))

        markdown_files = source_tools.convert_sources_to_markdown(
            list(request.get("source_files") or []),
            list(request.get("source_urls") or []),
            str(project),
        )
        mirror_dir = project / "templates" / "mirror"
        mirror_dir.mkdir(parents=True, exist_ok=True)
        copied_svgs: list[Path] = []
        source_checksums: dict[str, str] = {}
        for svg in resolved.svg_pages:
            target = mirror_dir / svg.name
            shutil.copy2(svg, target)
            copied_svgs.append(target)
            source_checksums[svg.name] = sha256_file(svg)

        for filename in ("design_spec.md", "manifest.json"):
            source = resolved.root / filename
            if source.is_file():
                shutil.copy2(source, mirror_dir / filename)
        assets = resolved.root / "assets"
        if assets.is_dir():
            shutil.copytree(assets, project / "svg_output" / "assets", dirs_exist_ok=True)

        roster = analyze_mirror_roster(copied_svgs)
        roster_path = project / "analysis" / "mirror_roster.json"
        write_mirror_roster(roster_path, roster)
        source_record = {
            "schema": "pptmaster.mirror-source.v1",
            "template_id": resolved.template_id,
            "source_root": str(resolved.root),
            "source_checksums": source_checksums,
            "copied_svgs": [str(path) for path in copied_svgs],
        }
        source_record_path = project / "mirror_source.json"
        source_record_path.write_text(
            json.dumps(source_record, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (project / "request.json").write_text(
            json.dumps(request, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self.runtime.log(
            job_id,
            "mirror template prepared",
            project_path=str(project),
            template_id=resolved.template_id,
            roster=str(roster_path),
        )
        return {
            "ok": True,
            "stage": "prepare",
            "project_path": str(project),
            "roster": str(roster_path),
            "source_record": str(source_record_path),
            "markdown_files": markdown_files,
        }

    def run(self, job: dict[str, Any]) -> dict[str, Any]:
        project = self._project_path(job)
        request = job.get("request", {})
        roster_path = project / "analysis" / "mirror_roster.json"
        roster = MirrorRoster.from_dict(json.loads(roster_path.read_text(encoding="utf-8")))
        source_markdown = "\n\n".join(
            path.read_text(encoding="utf-8", errors="replace")
            for path in sorted((project / "sources").glob("*.md"))
        )
        expected_pages = int(request.get("pages") or len(roster.pages))
        callback = getattr(self.runtime, "call_llm_json", call_llm_json)
        plan, report = plan_mirror_content(
            source_markdown,
            roster,
            expected_pages,
            call_json=callback,
        )
        plan_path = project / "mirror_plan.json"
        report_path = project / "mirror_plan_report.json"
        plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        report_path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if not report.passed:
            first = report.issues[0]
            raise self._error(
                ErrorKind.PROTOCOL,
                "mirror.plan.invalid",
                "map_roster",
                f"mirror plan remained invalid after three attempts: {first['code']} at page {first.get('page')} slot {first.get('slot_id')}; required {first['required']}",
                {"report": str(report_path), "issue": first},
                suggested_action=SuggestedAction.REGENERATE_STEP,
            )

        patch_result = apply_mirror_plan(plan, roster, project)
        patch_report = project / "analysis" / "mirror_patch_report.json"
        patch_report.write_text(
            json.dumps(patch_result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self.runtime.log(
            job["job_id"],
            "mirror template pages generated",
            pages=len(patch_result["svg_files"]),
            plan=str(plan_path),
        )
        return {
            "ok": True,
            "stage": "run",
            "project_path": str(project),
            "plan": str(plan_path),
            "plan_report": str(report_path),
            "patch_report": str(patch_report),
            "execution": patch_result,
        }

    def review(self, job: dict[str, Any]) -> dict[str, Any]:
        self.runtime.store.update_job(job["job_id"], status="checking_quality")
        project = self._project_path(job)
        review = quality_tools.check_svg_quality(str(project))
        if not review.passed:
            raise self._error(
                ErrorKind.QUALITY,
                "mirror.quality.svg",
                "quality",
                "mirror SVG quality check failed: " + " | ".join(review.issues),
                {"project_path": str(project)},
                suggested_action=SuggestedAction.REPAIR,
            )
        plan = json.loads((project / "mirror_plan.json").read_text(encoding="utf-8"))
        roster = MirrorRoster.from_dict(
            json.loads((project / "analysis" / "mirror_roster.json").read_text(encoding="utf-8"))
        )
        roster_pages = {page.source_svg: page for page in roster.pages}
        mismatches: list[str] = []
        for page_plan in plan["pages"]:
            output = project / "svg_output" / f"slide_{int(page_plan['output_index']):02d}.svg"
            expected = roster_pages[str(page_plan["source_svg"])].fingerprint
            if not output.is_file() or structural_fingerprint(output) != expected:
                mismatches.append(output.name)
        expected_pages = int((job.get("request") or {}).get("pages") or len(plan["pages"]))
        svg_count = len(list((project / "svg_output").glob("*.svg")))
        if svg_count != expected_pages or mismatches:
            raise self._error(
                ErrorKind.QUALITY,
                "mirror.quality.structure",
                "quality",
                f"mirror structure gate failed: expected {expected_pages} pages, found {svg_count}, mismatches={mismatches}",
                {"mismatches": mismatches, "svg_count": svg_count},
            )
        review_payload = review.model_dump()
        return {
            "ok": True,
            "stage": "review",
            "passed": True,
            "svg_count": svg_count,
            "structural_fingerprints_preserved": True,
            "review": review_payload,
        }

    def export(self, job: dict[str, Any]) -> dict[str, Any]:
        self.runtime.store.update_job(job["job_id"], status="exporting")
        project = self._project_path(job)
        exporter = getattr(self.runtime, "export_project", export_tools.export_project_to_pptx)
        output_file = exporter(str(project))
        output = Path(output_file)
        if not output.is_file() or output.stat().st_size == 0:
            raise self._error(
                ErrorKind.ARTIFACT,
                "mirror.export.missing",
                "export",
                f"mirror export did not create a non-empty PPTX: {output}",
                {"output_file": str(output)},
            )
        self.runtime.store.update_job(job["job_id"], output_file=str(output))
        return {"ok": True, "stage": "export", "output_file": str(output)}

    def _project_path(self, job: dict[str, Any]) -> Path:
        project_path = job.get("project_path") or (self.runtime.store.get_job(job["job_id"]) or {}).get("project_path")
        if not project_path:
            raise self._error(
                ErrorKind.ARTIFACT,
                "mirror.project.missing",
                "prepare",
                "mirror workflow job has no project_path",
                {"job_id": job["job_id"]},
            )
        return Path(project_path)

    @staticmethod
    def _error(
        kind: ErrorKind,
        code: str,
        step: str,
        message: str,
        location: dict[str, Any],
        *,
        suggested_action: SuggestedAction = SuggestedAction.STOP,
    ) -> WorkflowExecutionError:
        return WorkflowExecutionError(
            WorkflowError(
                kind=kind,
                code=code,
                step=step,
                message=message,
                location=location,
                retryable=False,
                suggested_action=suggested_action,
            )
        )
