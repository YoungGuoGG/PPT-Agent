from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..tools import export_tools, quality_tools
from .scripted import ScriptedWorkflow


CHART_TERMS = {
    "bar": "direct-calc",
    "line": "direct-calc",
    "area": "direct-calc",
    "scatter": "direct-calc",
    "pie": "direct-calc",
    "donut": "direct-calc",
    "radar": "direct-calc",
    "stacked": "decomposable-calc",
    "pareto": "decomposable-calc",
    "waterfall": "decomposable-calc",
    "bubble": "partial-calc",
    "gauge": "formula-verify",
    "funnel": "formula-verify",
    "sankey": "manual-verify",
    "heatmap": "manual-verify",
    "treemap": "manual-verify",
}


class VerifyChartsWorkflow(ScriptedWorkflow):
    name = "verify_charts"
    is_stub = False

    def prepare(self, job: dict[str, Any]) -> dict[str, Any]:
        project = self.project_path(job)
        spec = project / "design_spec.md"
        if not spec.is_file():
            self.fail(
                step="discover_pages",
                code="design_spec_missing",
                message="chart discovery requires design_spec.md",
                location={"path": str(spec)},
                correction={"action": "restore_design_spec", "required_section": "Visualization Reference List"},
            )
        text = spec.read_text(encoding="utf-8", errors="replace")
        pages = []
        for svg in sorted((project / "svg_output").glob("*.svg")):
            stem_pattern = re.escape(svg.stem)
            context = "\n".join(line for line in text.splitlines() if re.search(stem_pattern, line, re.I))
            lower = f"{svg.stem} {context}".lower()
            matched = next(((term, mode) for term, mode in CHART_TERMS.items() if term in lower), None)
            if matched:
                pages.append({"page": svg.name, "type": matched[0], "mode": matched[1]})
        payload = {"schema": "pptmaster.chart-verification-plan.v1", "pages": pages}
        plan = self.write_json(project / "reports" / "chart_verification_plan.json", payload)
        return {"ok": True, "stage": "prepare", "plan": str(plan), "page_count": len(pages)}

    def run(self, job: dict[str, Any]) -> dict[str, Any]:
        project = self.project_path(job)
        plan = json.loads((project / "reports" / "chart_verification_plan.json").read_text(encoding="utf-8"))
        receipts = []
        for page in plan["pages"]:
            svg = project / "svg_output" / page["page"]
            result = self.run_script(job, "svg_position_calculator.py", "analyze", str(svg), step=f"analyze_{svg.stem}")
            receipts.append({**page, "status": "inspected", "calculator_output": result["stdout"][-3000:]})
        report = self.write_json(
            project / "reports" / "chart_verification_report.json",
            {"schema": "pptmaster.chart-verification-report.v1", "receipts": receipts, "receipt_count": len(receipts)},
        )
        return {"ok": True, "stage": "run", "chart_verification_report": str(report), "receipts": receipts}

    def review(self, job: dict[str, Any]) -> dict[str, Any]:
        project = self.project_path(job)
        quality = quality_tools.check_svg_quality(str(project))
        if not quality.passed:
            self.fail(
                step="quality",
                code="chart_svg_quality_failed",
                message="chart verification left SVG quality findings",
                location={"issues": quality.issues},
                correction={"action": "repair_exact_svg_findings", "issues": quality.issues},
                retryable=True,
            )
        return {"ok": True, "stage": "review", "passed": True, "quality_report": quality.to_dict()}

    def export(self, job: dict[str, Any]) -> dict[str, Any]:
        output = export_tools.export_project_to_pptx(str(self.project_path(job)))
        self.runtime.store.update_job(job["job_id"], output_file=output)
        return {"ok": True, "stage": "export", "output_file": output}
