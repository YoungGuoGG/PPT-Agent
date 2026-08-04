from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..conformance import AgentPhaseRequest
from ..tools import export_tools, quality_tools
from .scripted import ScriptedWorkflow


class ResumeExecuteWorkflow(ScriptedWorkflow):
    name = "resume_execute"
    is_stub = False

    def prepare(self, job: dict[str, Any]) -> dict[str, Any]:
        project = self.project_path(job)
        required = [project / "design_spec.md", project / "spec_lock.md", project / "svg_output"]
        missing = [str(path) for path in required if not path.exists()]
        if missing:
            self.fail(
                step="validate_checkpoint",
                code="checkpoint_incomplete",
                message="resume checkpoint is missing required project artifacts",
                location={"missing": missing},
                correction={"action": "restore_checkpoint_artifacts", "required": ["design_spec.md", "spec_lock.md", "svg_output/"]},
            )
        expected = self._expected_pages(project / "design_spec.md")
        existing = sorted(path.name for path in (project / "svg_output").glob("*.svg"))
        report = {
            "schema": "pptmaster.resume-checkpoint.v1",
            "project_path": str(project),
            "expected_pages": expected,
            "existing_pages": existing,
            "missing_count": max(0, expected - len(existing)),
        }
        report_path = self.write_json(project / "reports" / "resume_checkpoint.json", report)
        self.runtime.store.update_job(job["job_id"], project_path=str(project))
        return {"ok": True, "stage": "prepare", "checkpoint_report": str(report_path), **report}

    def run(self, job: dict[str, Any]) -> dict[str, Any]:
        project = self.project_path(job)
        report = json.loads((project / "reports" / "resume_checkpoint.json").read_text(encoding="utf-8"))
        if report["missing_count"]:
            result = self.runtime.phase_runner.run_phase(
                AgentPhaseRequest(
                    job_id=job["job_id"],
                    phase="executor",
                    project_path=str(project),
                    checkpoint={
                        "page_count": report["expected_pages"],
                        "resume": True,
                        "existing_pages": report["existing_pages"],
                    },
                )
            )
            if result.status not in {"completed", "success"}:
                return {"ok": True, "stage": "run", "status": result.status, **result.output}
        return {"ok": True, "stage": "run", "project_path": str(project)}

    def review(self, job: dict[str, Any]) -> dict[str, Any]:
        project = self.project_path(job)
        result = quality_tools.check_svg_quality(str(project))
        if not result.passed:
            self.fail(
                step="quality",
                code="resumed_svg_quality_failed",
                message="resumed SVG pages failed quality checks",
                location={"project_path": str(project), "issues": result.issues},
                correction={"action": "repair_reported_svg_locations", "issues": result.issues},
                retryable=True,
            )
        return {"ok": True, "stage": "review", "passed": True, "quality_report": result.to_dict()}

    def export(self, job: dict[str, Any]) -> dict[str, Any]:
        output = export_tools.export_project_to_pptx(str(self.project_path(job)))
        self.runtime.store.update_job(job["job_id"], output_file=output)
        return {"ok": True, "stage": "export", "output_file": output}

    @staticmethod
    def _expected_pages(spec: Path) -> int:
        text = spec.read_text(encoding="utf-8", errors="replace")
        matches = re.findall(r"(?m)^\s*(?:P|Page\s*)?0*([1-9]\d*)\s*[|.:：-]", text)
        return max((int(value) for value in matches), default=0)
