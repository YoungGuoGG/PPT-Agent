from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from ..llm_client import call_llm_json
from ..conformance import (
    ErrorKind,
    SuggestedAction,
    WorkflowError,
    WorkflowExecutionError,
)
from ..image_understanding import understand_project_images
from ..tools import pptx_tools
from ..tools.project_tools import ToolExecutionError
from ..tools.source_tools import convert_sources_to_markdown
from .base import BaseWorkflow


class TemplateFillPPTXWorkflow(BaseWorkflow):
    name = "template_fill_pptx"
    is_stub = False

    def prepare(self, job: dict[str, Any]) -> dict[str, Any]:
        request = job.get("request", {})
        template_pptx = request.get("template_pptx")
        if not template_pptx:
            raise ValueError("template_fill_pptx requires template_pptx")

        template_path = Path(template_pptx).expanduser().resolve()
        if not template_path.exists() or not template_path.is_file():
            raise FileNotFoundError(f"template_pptx does not exist: {template_pptx}")
        if template_path.suffix.lower() not in {".pptx", ".pptm", ".ppsx", ".ppsm", ".potx", ".potm"}:
            raise ValueError(f"template_pptx must be a PowerPoint file, got: {template_path.suffix}")

        workspace = Path(job["workspace"])
        sources_dir = workspace / "sources"
        analysis_dir = workspace / "analysis"
        exports_dir = workspace / "exports"
        validation_dir = workspace / "validation"
        for directory in (sources_dir, analysis_dir, exports_dir, validation_dir):
            directory.mkdir(parents=True, exist_ok=True)

        local_template = sources_dir / template_path.name
        if local_template.resolve() != template_path:
            shutil.copy2(template_path, local_template)

        source_files = [
            source
            for source in request.get("source_files", [])
            if Path(source).expanduser().resolve() != template_path
        ]
        converted_sources = convert_sources_to_markdown(
            source_files,
            request.get("source_urls", []),
            str(workspace),
        )
        image_files = [str(path) for path in sorted((workspace / "images" / "user").glob("*")) if path.is_file()]
        if image_files:
            understand_project_images(workspace, image_files, request)
        if request.get("topic") or request.get("prompt") or request.get("extra_requirements"):
            brief_path = sources_dir / "brief.md"
            brief_path.write_text(self._brief_markdown(request), encoding="utf-8")
            converted_sources.append(str(brief_path))

        if not converted_sources:
            raise ValueError(
                "template_fill_pptx requires source material: provide source_files, source_urls, topic, prompt, or extra_requirements"
            )

        runtime_payload = {
            "template_pptx": str(local_template),
            "source_markdown": converted_sources,
            "analysis_dir": str(analysis_dir),
            "exports_dir": str(exports_dir),
            "validation_dir": str(validation_dir),
        }
        (analysis_dir / "runtime_inputs.json").write_text(
            json.dumps(runtime_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.runtime.log(job["job_id"], "template_fill_pptx prepared", **runtime_payload)
        return {"ok": True, "stage": "prepare", **runtime_payload}

    def run(self, job: dict[str, Any]) -> dict[str, Any]:
        paths = self._paths(job)
        request = job.get("request", {})
        job_id = job["job_id"]

        self.runtime.store.update_job(job_id, status="analyzing_template")
        self.runtime.log(job_id, "template fill analyze start", template_pptx=str(paths["template_pptx"]))
        analyze_result = pptx_tools.template_fill_analyze(
            str(paths["template_pptx"]),
            str(paths["slide_library"]),
        )
        self.runtime.log(job_id, "template fill analyze complete", tool=analyze_result.to_dict())

        library = pptx_tools.load_json(paths["slide_library"])
        selected_slides = self._slides_arg(request, library)
        self.runtime.log(job_id, "template fill scaffold start", slides=selected_slides)
        scaffold_result = pptx_tools.template_fill_scaffold(
            str(paths["slide_library"]),
            str(paths["fill_plan_scaffold"]),
            slides=selected_slides,
        )
        self.runtime.log(job_id, "template fill scaffold complete", tool=scaffold_result.to_dict())

        self.runtime.store.update_job(job_id, status="planning_fill")
        fill_plan, check_report = self._generate_checked_fill_plan(
            job,
            library,
            pptx_tools.load_json(paths["fill_plan_scaffold"]),
            slide_library_path=paths["slide_library"],
            fill_plan_path=paths["fill_plan"],
            check_report_path=paths["check_report"],
        )
        self.runtime.log(
            job_id,
            "template fill plan generated",
            slide_count=len(fill_plan.get("slides", [])),
            fill_plan=str(paths["fill_plan"]),
        )

        self.runtime.store.update_job(job_id, status="checking_fill_plan")
        self.runtime.log(
            job_id,
            "template fill check-plan complete",
            summary=check_report.get("summary", {}),
            check_report=str(paths["check_report"]),
        )

        self.runtime.store.update_job(job_id, status="applying_template")
        requested_output = paths["exports_dir"] / "filled_template.pptx"
        apply_result = pptx_tools.template_fill_apply(
            str(paths["template_pptx"]),
            str(paths["fill_plan"]),
            str(requested_output),
        )
        output_file = pptx_tools.template_fill_find_output(str(requested_output), apply_result.stderr)
        self.runtime.store.update_job(job_id, output_file=output_file)
        self.runtime.log(job_id, "template fill apply complete", output_file=output_file, tool=apply_result.to_dict())

        return {
            "ok": True,
            "stage": "run",
            "slide_library": str(paths["slide_library"]),
            "fill_plan": str(paths["fill_plan"]),
            "check_report": str(paths["check_report"]),
            "output_file": output_file,
        }

    def review(self, job: dict[str, Any]) -> dict[str, Any]:
        job_id = job["job_id"]
        self.runtime.store.update_job(job_id, status="validating_output")
        current_job = self.runtime.store.get_job(job_id) or job
        output_file = current_job.get("output_file")
        if not output_file:
            raise ValueError("template_fill_pptx review requires output_file")

        output_path = Path(output_file)
        if not output_path.exists() or not output_path.is_file():
            raise FileNotFoundError(f"template_fill_pptx output PPTX does not exist: {output_file}")
        size = output_path.stat().st_size
        if size <= 0:
            raise ValueError(f"template_fill_pptx output PPTX is empty: {output_file}")

        paths = self._paths(current_job)
        validation_md = paths["validation_dir"] / f"{output_path.stem}.md"
        validate_result = pptx_tools.template_fill_validate(str(output_path), str(validation_md))
        passed = validate_result.returncode == 0 and validation_md.exists() and validation_md.stat().st_size > 0
        if not passed:
            message = (validate_result.stderr or validate_result.stdout or "ppt_to_md validation failed").strip()
            raise ToolExecutionError(message, validate_result)

        self.runtime.log(
            job_id,
            "template fill validate complete",
            output_file=str(output_path),
            size=size,
            validation_md=str(validation_md),
            tool=validate_result.to_dict(),
        )
        return {
            "ok": True,
            "stage": "review",
            "passed": True,
            "output_file": str(output_path),
            "size": size,
            "validation_md": str(validation_md),
        }

    def export(self, job: dict[str, Any]) -> dict[str, Any]:
        current_job = self.runtime.store.get_job(job["job_id"]) or job
        output_file = current_job.get("output_file")
        if not output_file:
            raise ValueError("template_fill_pptx export requires output_file")
        output_path = Path(output_file)
        if not output_path.exists():
            raise FileNotFoundError(f"template_fill_pptx export missing output file: {output_file}")
        self.runtime.store.update_job(job["job_id"], status="exporting", output_file=str(output_path))
        self.runtime.log(job["job_id"], "template fill export ready", output_file=str(output_path))
        return {"ok": True, "stage": "export", "output_file": str(output_path)}

    def _paths(self, job: dict[str, Any]) -> dict[str, Path]:
        workspace = Path(job["workspace"])
        analysis_dir = workspace / "analysis"
        exports_dir = workspace / "exports"
        validation_dir = workspace / "validation"
        runtime_inputs = json.loads((analysis_dir / "runtime_inputs.json").read_text(encoding="utf-8"))
        return {
            "workspace": workspace,
            "template_pptx": Path(runtime_inputs["template_pptx"]),
            "source_markdown": [Path(path) for path in runtime_inputs["source_markdown"]],
            "analysis_dir": analysis_dir,
            "exports_dir": exports_dir,
            "validation_dir": validation_dir,
            "slide_library": analysis_dir / "slide_library.json",
            "fill_plan_scaffold": analysis_dir / "fill_plan_scaffold.json",
            "fill_plan": analysis_dir / "fill_plan.json",
            "check_report": analysis_dir / "check_report.json",
        }

    def _generate_fill_plan(
        self,
        job: dict[str, Any],
        library: dict,
        scaffold: dict,
        repair_context: dict | None = None,
    ) -> dict:
        request = job.get("request", {})
        paths = self._paths(job)
        source_text = self._read_sources(paths["source_markdown"])
        prompt_path = Path(__file__).resolve().parents[1] / "prompts" / "template_fill_prompt.md"
        system_prompt = prompt_path.read_text(encoding="utf-8")
        prompt = "\n\n".join(
            [
                "Generate a complete fill_plan.json for the template_fill_pptx workflow.",
                "Return only JSON. The JSON must use schema template_fill_pptx_plan.v1.",
                (
                    f"The output slides list MUST contain exactly {int(request['pages'])} entries. "
                    "Repeat suitable source_slide layouts when the template has fewer pages than requested."
                    if request.get("pages")
                    else "Choose the output page count from the source material and request."
                ),
                "The scaffold is only a source-layout starter; it never limits output order or page count.",
                "Split material across repeated content slides instead of overfilling existing text slots.",
                "Use only slot_id/table_id/chart_id values present in slide_library.json.",
                "Preserve the source template layout: select, reorder, or repeat source slides by layout fitness.",
                "Do not invent factual claims beyond the provided source material.",
                f"Request JSON:\n{json.dumps(request, ensure_ascii=False, indent=2)}",
                f"Source material markdown:\n{source_text}",
                f"Slide library JSON:\n{json.dumps(library, ensure_ascii=False, indent=2)}",
                f"Scaffold starter JSON:\n{json.dumps(scaffold, ensure_ascii=False, indent=2)}",
                (
                    f"Previous plan validation feedback; repair every listed issue:\n"
                    f"{json.dumps(repair_context, ensure_ascii=False, indent=2)}"
                    if repair_context
                    else "No previous repair feedback."
                ),
            ]
        )
        callback = getattr(self.runtime, "call_llm_json", call_llm_json)
        return callback(prompt, system_prompt)

    def _generate_checked_fill_plan(
        self,
        job: dict[str, Any],
        library: dict,
        scaffold: dict,
        *,
        slide_library_path: Path,
        fill_plan_path: Path,
        check_report_path: Path,
        max_attempts: int = 3,
    ) -> tuple[dict, dict]:
        repair_context: dict | None = None
        last_issue = "fill plan did not pass validation"
        for _attempt in range(max_attempts):
            plan = self._generate_fill_plan(job, library, scaffold, repair_context=repair_context)
            try:
                self._validate_fill_plan_shape(plan, requested_pages=(job.get("request") or {}).get("pages"))
            except ValueError as exc:
                last_issue = str(exc)
                repair_context = {
                    "code": "FILL_PLAN_SCHEMA_INVALID",
                    "location": self._plan_error_location(last_issue),
                    "reason": last_issue,
                    "correction": {
                        "action": "regenerate_fill_plan_only",
                        "requirements": [
                            "use schema template_fill_pptx_plan.v1",
                            "use only source_slide and slot identifiers from slide_library.json",
                            "return the requested number of slides",
                        ],
                    },
                    "previous_plan": plan,
                }
                continue

            pptx_tools.write_json(fill_plan_path, plan)
            pptx_tools.template_fill_check(
                str(slide_library_path),
                str(fill_plan_path),
                str(check_report_path),
            )
            report = pptx_tools.load_json(check_report_path)
            if not self._check_report_requires_replan(report):
                return plan, report
            last_issue = f"capacity check failed: {report.get('summary', {})}"
            repair_context = {
                "code": "FILL_PLAN_CAPACITY_EXCEEDED",
                "location": {"artifact": str(check_report_path), "summary": report.get("summary", {})},
                "reason": last_issue,
                "correction": {
                    "action": "redistribute_content_across_slides",
                    "requirements": [
                        "shorten overflowing replacements before reducing font size",
                        "repeat a compatible source slide when one layout lacks capacity",
                        "repair every warning and error listed in check_report",
                    ],
                },
                "previous_plan": plan,
                "check_report": report,
            }

        raise WorkflowExecutionError(
            WorkflowError(
                kind=ErrorKind.PROTOCOL,
                code="template_fill_plan_exhausted",
                step="plan",
                message=f"template fill plan failed after {max_attempts} attempts: {last_issue}",
                location=(repair_context or {}).get("location", {"artifact": str(fill_plan_path)}),
                correction={
                    "action": "regenerate_planning_step",
                    "last_feedback": repair_context or {"reason": last_issue},
                    "must_preserve": ["source facts", "template layout", "requested page count"],
                },
                retryable=False,
                suggested_action=SuggestedAction.REGENERATE_STEP,
                attempt=max_attempts,
            )
        )

    @staticmethod
    def _plan_error_location(message: str) -> dict[str, Any]:
        import re

        match = re.search(r"slide\s+(\d+)", message, re.IGNORECASE)
        location: dict[str, Any] = {"artifact": "fill_plan.json"}
        if match:
            location["slide"] = int(match.group(1))
        for field in ("source_slide", "replacements", "table_edits", "chart_edits", "schema", "slides"):
            if field in message:
                location["field"] = field
                break
        return location

    @staticmethod
    def _validate_fill_plan_shape(plan: dict, requested_pages: int | None = None) -> None:
        if plan.get("schema") != "template_fill_pptx_plan.v1":
            raise ValueError("LLM fill_plan must set schema to template_fill_pptx_plan.v1")
        slides = plan.get("slides")
        if not isinstance(slides, list) or not slides:
            raise ValueError("LLM fill_plan must contain a non-empty slides list")
        if requested_pages is not None and len(slides) != int(requested_pages):
            raise ValueError(
                f"LLM fill_plan must contain exactly {int(requested_pages)} slides; got {len(slides)}"
            )
        for index, slide in enumerate(slides, start=1):
            if not isinstance(slide, dict):
                raise ValueError(f"fill_plan slide {index} must be an object")
            if not slide.get("source_slide"):
                raise ValueError(f"fill_plan slide {index} missing source_slide")
            for key in ("replacements", "table_edits", "chart_edits"):
                if key in slide and not isinstance(slide[key], list):
                    raise ValueError(f"fill_plan slide {index} {key} must be a list")

    @staticmethod
    def _check_report_requires_replan(report: dict) -> bool:
        summary = report.get("summary") if isinstance(report, dict) else None
        if not isinstance(summary, dict):
            return True
        return int(summary.get("warn") or 0) > 0 or int(summary.get("error") or 0) > 0

    @staticmethod
    def _read_sources(paths: list[Path], limit: int = 30000) -> str:
        chunks: list[str] = []
        used = 0
        for path in paths:
            text = path.read_text(encoding="utf-8", errors="replace")
            remaining = limit - used
            if remaining <= 0:
                break
            chunks.append(f"# Source: {path.name}\n\n{text[:remaining]}")
            used += min(len(text), remaining)
        return "\n\n".join(chunks)

    @staticmethod
    def _slides_arg(request: dict[str, Any], library: dict) -> str | None:
        requested_pages = request.get("pages")
        slide_count = int(library.get("slide_count") or len(library.get("slides", [])) or 0)
        if not requested_pages or slide_count <= 0:
            return None
        count = max(1, min(int(requested_pages), slide_count))
        return f"1-{count}"

    @staticmethod
    def _brief_markdown(request: dict[str, Any]) -> str:
        lines = ["# Template Fill Brief", ""]
        for key in ("topic", "prompt", "language", "style", "quality_level", "extra_requirements"):
            value = request.get(key)
            if value:
                lines.extend([f"## {key}", str(value), ""])
        if request.get("pages"):
            lines.extend(["## pages", str(request["pages"]), ""])
        return "\n".join(lines)
