from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..tools import export_tools, quality_tools
from .scripted import ScriptedWorkflow


class VisualReviewWorkflow(ScriptedWorkflow):
    name = "visual_review"
    is_stub = False

    def prepare(self, job: dict[str, Any]) -> dict[str, Any]:
        project = self.project_path(job)
        svgs = sorted((project / "svg_output").glob("*.svg"))
        if not svgs:
            self.fail(
                step="render",
                code="svg_pages_missing",
                message="visual review requires generated SVG pages",
                location={"path": str(project / "svg_output")},
                correction={"action": "complete_svg_generation_first"},
            )
        self.run_script(job, "visual_review.py", str(project), step="render")
        rendered = sorted(path for path in project.rglob("*.png") if "preview" in str(path).lower() or "render" in str(path).lower())
        manifest = self.write_json(
            project / "reports" / "visual_review_manifest.json",
            {"svg_pages": [path.name for path in svgs], "rendered_pages": [str(path) for path in rendered]},
        )
        return {"ok": True, "stage": "prepare", "rendered_pages": [str(path) for path in rendered], "manifest": str(manifest)}

    def run(self, job: dict[str, Any]) -> dict[str, Any]:
        project = self.project_path(job)
        quality = quality_tools.check_svg_quality(str(project))
        issues = [{"severity": "blocking", "message": issue} for issue in quality.issues]
        page_reviews = []
        for svg in sorted((project / "svg_output").glob("*.svg")):
            text = svg.read_text(encoding="utf-8", errors="replace")
            page_issues = []
            if "<text" not in text:
                page_issues.append({"severity": "warning", "message": "page has no editable text"})
            if "viewBox" not in text:
                page_issues.append({"severity": "blocking", "message": "root SVG has no viewBox"})
            page_reviews.append({"page": svg.name, "issues": page_issues, "status": "reviewed"})
        report_payload = {
            "schema": "pptmaster.visual-review.v1",
            "all_pages_reviewed": True,
            "page_reviews": page_reviews,
            "aggregate_findings": issues,
            "blocking_count": sum(1 for issue in issues if issue["severity"] == "blocking") + sum(1 for page in page_reviews for issue in page["issues"] if issue["severity"] == "blocking"),
        }
        report = self.write_json(project / "reports" / "visual_review.json", report_payload)
        return {"ok": True, "stage": "run", "aggregate_report": str(report), **report_payload}

    def review(self, job: dict[str, Any]) -> dict[str, Any]:
        project = self.project_path(job)
        report = json.loads((project / "reports" / "visual_review.json").read_text(encoding="utf-8"))
        if report["blocking_count"]:
            self.fail(
                step="repair",
                code="visual_review_blocking_findings",
                message="visual review found blocking issues",
                location={"report": str(project / "reports" / "visual_review.json")},
                correction={"action": "repair_each_blocking_finding", "findings": report["aggregate_findings"]},
                retryable=True,
            )
        return {"ok": True, "stage": "review", "passed": True, "reviewed_pages": len(report["page_reviews"])}

    def export(self, job: dict[str, Any]) -> dict[str, Any]:
        output = export_tools.export_project_to_pptx(str(self.project_path(job)))
        self.runtime.store.update_job(job["job_id"], output_file=output)
        return {"ok": True, "stage": "export", "output_file": output}
