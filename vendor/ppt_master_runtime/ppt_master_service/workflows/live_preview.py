from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..tools import export_tools, quality_tools
from .scripted import ScriptedWorkflow


class LivePreviewWorkflow(ScriptedWorkflow):
    name = "live_preview"
    is_stub = False

    def prepare(self, job: dict[str, Any]) -> dict[str, Any]:
        project = self.project_path(job)
        session = self.runtime.preview_runtime.start(job["job_id"], project)
        payload = {
            "schema": "pptmaster.preview-session.v1",
            "url": session.url,
            "project_path": str(project),
            "status": "ready",
        }
        path = self.write_json(project / "reports" / "preview_session.json", payload)
        return {"ok": True, "stage": "prepare", "preview_session": str(path), **payload}

    def run(self, job: dict[str, Any]) -> dict[str, Any]:
        project = self.project_path(job)
        request = job.get("request") or {}
        changes = request.get("changes") or []
        if not isinstance(changes, list):
            self.fail(
                step="apply_changes",
                code="changes_invalid",
                message="changes must be a list of page-scoped edits",
                location={"field": "changes"},
                correction={"action": "use_change_list", "example": [{"page": "03_content.svg", "svg": "<svg>...</svg>"}]},
            )
        applied = []
        for index, change in enumerate(changes):
            if not isinstance(change, dict) or not change.get("page") or not change.get("svg"):
                self.fail(
                    step="apply_changes",
                    code="change_invalid",
                    message="each preview change needs page and complete svg",
                    location={"index": index},
                    correction={"action": "supply_page_and_complete_svg"},
                )
            target = (project / "svg_output" / Path(str(change["page"])).name).resolve()
            if target.parent != (project / "svg_output").resolve() or not target.exists():
                self.fail(
                    step="apply_changes",
                    code="change_page_unknown",
                    message=f"preview page does not exist: {change['page']}",
                    location={"index": index, "page": change["page"]},
                    correction={"action": "select_existing_svg_page"},
                )
            target.write_text(str(change["svg"]), encoding="utf-8")
            applied.append(target.name)
        receipt = self.write_json(project / "reports" / "preview_change_receipt.json", {"applied_pages": applied, "count": len(applied)})
        return {"ok": True, "stage": "run", "change_receipt": str(receipt), "applied_pages": applied}

    def review(self, job: dict[str, Any]) -> dict[str, Any]:
        quality = quality_tools.check_svg_quality(str(self.project_path(job)))
        if not quality.passed:
            self.fail(
                step="quality",
                code="preview_change_quality_failed",
                message="preview edits introduced SVG quality findings",
                location={"issues": quality.issues},
                correction={"action": "repair_changed_pages", "issues": quality.issues},
                retryable=True,
            )
        return {"ok": True, "stage": "review", "passed": True, "quality_report": quality.to_dict()}

    def export(self, job: dict[str, Any]) -> dict[str, Any]:
        output = export_tools.export_project_to_pptx(str(self.project_path(job)))
        self.runtime.store.update_job(job["job_id"], output_file=output)
        return {"ok": True, "stage": "export", "output_file": output}
