from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE

from .. import visual_assets
from ..llm_client import call_llm_json
from ..tools import export_tools, project_tools, quality_tools, source_tools
from .base import BaseWorkflow


CANVAS_DIMENSIONS = {
    "ppt169": (1280, 720),
    "ppt43": (1024, 768),
}
PPTX_SUFFIXES = {".pptx", ".pptm", ".ppsx", ".ppsm", ".potx", ".potm"}


class RevisionWorkflow(BaseWorkflow):
    """Visual beautification workflow for an existing PPTX deck.

    Scripts extract the original deck into content and layout artifacts. The
    LLM reads the complete layout once to decide the global visual direction,
    then regenerates each slide as SVG using the current slide's full layout.
    """

    name = "revision"
    is_stub = False

    def prepare(self, job: dict[str, Any]) -> dict[str, Any]:
        request = job.get("request", {})
        job_id = job["job_id"]
        reference = request.get("reference_pptx") or self._first_pptx_source(request.get("source_files") or [])
        if not reference:
            raise ValueError("revision requires reference_pptx or a PPTX in source_files")

        reference_path = Path(reference).expanduser().resolve()
        if not reference_path.exists() or not reference_path.is_file():
            raise FileNotFoundError(f"reference_pptx does not exist: {reference}")
        if reference_path.suffix.lower() not in PPTX_SUFFIXES:
            raise ValueError(f"reference_pptx must be a PowerPoint file, got: {reference_path.suffix}")

        workspace = Path(job["workspace"])
        sources_dir = workspace / "sources"
        analysis_dir = workspace / "analysis"
        validation_dir = workspace / "validation"
        for directory in (sources_dir, analysis_dir, validation_dir):
            directory.mkdir(parents=True, exist_ok=True)

        project_path = Path(project_tools.create_project(job_id, request.get("canvas_format") or "ppt169"))
        self.runtime.store.update_job(job_id, project_path=str(project_path))

        local_reference = sources_dir / reference_path.name
        if local_reference.resolve() != reference_path:
            shutil.copy2(reference_path, local_reference)

        original_svg_dir = project_path / "revision_original"
        original_svg_dir.mkdir(parents=True, exist_ok=True)
        scripts_root = Path(__file__).resolve().parents[2] / "skills" / "ppt-master" / "scripts"
        if str(scripts_root) not in sys.path:
            sys.path.insert(0, str(scripts_root))
        from pptx_to_svg import convert_pptx_to_svg
        from pptx_to_svg.converter import ConvertOptions

        original_pages = convert_pptx_to_svg(
            local_reference,
            options=ConvertOptions(embed_images=True, inheritance_mode="flat"),
        )
        for page in original_pages.slides:
            (original_svg_dir / f"slide_{page.index:02d}.svg").write_text(
                page.svg,
                encoding="utf-8",
            )

        extracted_md = sources_dir / "reference_content.md"
        source_tools.ppt_to_md(str(local_reference), str(extracted_md))
        project_sources = project_path / "sources"
        project_sources.mkdir(parents=True, exist_ok=True)
        shutil.copy2(extracted_md, project_sources / "reference_content.md")

        extra_files = [
            path
            for path in request.get("source_files", [])
            if Path(path).expanduser().resolve() != reference_path
        ]
        converted_sources = source_tools.convert_sources_to_markdown(
            extra_files,
            request.get("source_urls", []),
            str(project_path),
        )
        source_markdown = [str(project_sources / "reference_content.md"), *converted_sources]

        layout_full = self._extract_layout_full(local_reference, project_path, request)
        layout_path = analysis_dir / "layout_full.json"
        layout_path.write_text(json.dumps(layout_full, ensure_ascii=False, indent=2), encoding="utf-8")
        shutil.copy2(layout_path, project_sources / "layout_full.json")

        revision_brief = analysis_dir / "revision_brief.md"
        revision_brief.write_text(self._revision_brief(request), encoding="utf-8")
        (project_path / "request.json").write_text(
            json.dumps(request, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        payload = {
            "reference_pptx": str(local_reference),
            "project_path": str(project_path),
            "source_markdown": source_markdown,
            "analysis_dir": str(analysis_dir),
            "validation_dir": str(validation_dir),
            "layout_full": str(layout_path),
            "revision_brief": str(revision_brief),
            "original_svg_dir": str(original_svg_dir),
            "method": "visual revision: full layout analysis -> LLM global plan -> per-slide SVG regeneration",
        }
        (analysis_dir / "runtime_inputs.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.runtime.log(job_id, "revision prepared", **payload)
        return {"ok": True, "stage": "prepare", **payload}

    def run(self, job: dict[str, Any]) -> dict[str, Any]:
        job_id = job["job_id"]
        paths = self._paths(job)
        request = job.get("request", {})
        project_path = paths["project_path"]

        self.runtime.store.update_job(job_id, status="planning_visual_revision")
        layout = json.loads(paths["layout_full"].read_text(encoding="utf-8"))
        source_text = self._read_sources(paths["source_markdown"])
        strategy = self._generate_global_beautify_plan(job, layout, source_text, project_path)
        visual_asset_result = visual_assets.materialize_visual_assets(
            project_path,
            strategy,
            log=lambda event, payload: self.runtime.log(job_id, event, **payload),
        )
        self._write_strategy_artifacts(project_path, strategy)
        self.runtime.log(
            job_id,
            "revision visual strategy generated",
            summary=str(project_path / "sources" / "original_ppt_summary.md"),
            strategy=str(project_path / ".service_strategy.json"),
            visual_assets=visual_asset_result,
        )

        self.runtime.store.update_job(job_id, status="generating_revised_svg")
        generated = self._generate_revised_svgs(job, layout, strategy)
        self.runtime.log(job_id, "revision SVG generation complete", result=generated)
        return {"ok": True, "stage": "run", "project_path": str(project_path), **generated}

    def review(self, job: dict[str, Any]) -> dict[str, Any]:
        job_id = job["job_id"]
        self.runtime.store.update_job(job_id, status="checking_quality")
        paths = self._paths(self.runtime.store.get_job(job_id) or job)
        project_path = paths["project_path"]
        layout = json.loads(paths["layout_full"].read_text(encoding="utf-8"))
        expected_count = int(layout.get("deck", {}).get("slide_count") or 0)

        review = quality_tools.check_svg_quality(str(project_path))
        self.runtime.log(job_id, "revision SVG quality check complete", result=review.model_dump())
        if not review.passed:
            raise RuntimeError("SVG quality check failed: " + " | ".join(review.issues))

        svg_count = len(list((project_path / "svg_output").glob("*.svg")))
        if expected_count and svg_count != expected_count:
            raise RuntimeError(f"revision SVG count mismatch: expected {expected_count}, got {svg_count}")
        return {"ok": True, "stage": "review", "svg_count": svg_count, "review": review.model_dump()}

    def export(self, job: dict[str, Any]) -> dict[str, Any]:
        job_id = job["job_id"]
        self.runtime.store.update_job(job_id, status="exporting")
        paths = self._paths(self.runtime.store.get_job(job_id) or job)

        def log_step(step_name, result):
            self.runtime.log(job_id, f"revision export tool step complete: {step_name}", tool=result.to_dict())

        output_file = export_tools.export_project_to_pptx(str(paths["project_path"]), log=log_step)
        self.runtime.store.update_job(job_id, output_file=output_file)
        return {"ok": True, "stage": "export", "output_file": output_file}

    def _generate_global_beautify_plan(
        self,
        job: dict[str, Any],
        layout: dict[str, Any],
        source_text: str,
        project_path: Path,
    ) -> dict[str, Any]:
        request = job.get("request", {})
        system_prompt = (
            "You are the global visual planner for a PPT beautification workflow. "
            "Read the complete original deck layout and content. Choose the most suitable "
            "visual direction for this specific deck. Return JSON only."
        )
        prompt = "\n\n".join(
            [
                "Create a visual beautification plan for this existing PPT.",
                "Do not use a fixed style. Infer the best style from the original deck, content, audience, and request.",
                "Preserve the original deck's core content and required images, but improve layout, hierarchy, typography, color, and visual rhythm.",
                "Return JSON with keys: original_ppt_summary_md, beautify_strategy, design_spec_md, spec_lock, slide_briefs, image.",
                "Each slide_briefs item must include index, title, purpose, key_points, visual_direction, required_content, required_images, speaker_note. Include icon_slots or image_slots only where useful.",
                visual_assets.build_visual_asset_prompt(project_path),
                f"Request JSON:\n{json.dumps(request, ensure_ascii=False, indent=2)}",
                f"Original PPT markdown:\n{source_text}",
                f"Full original PPT layout JSON:\n{json.dumps(layout, ensure_ascii=False, indent=2)}",
            ]
        )
        callback = getattr(self.runtime, "call_llm_json", call_llm_json)
        payload = callback(prompt, system_prompt)
        return self._normalize_strategy(payload, layout, request)

    def _generate_revised_svgs(self, job: dict[str, Any], layout: dict[str, Any], strategy: dict[str, Any]) -> dict[str, Any]:
        request = job.get("request", {})
        paths = self._paths(job)
        project_path = paths["project_path"]
        svg_dir = project_path / "svg_output"
        notes_dir = project_path / "notes"
        svg_dir.mkdir(parents=True, exist_ok=True)
        notes_dir.mkdir(parents=True, exist_ok=True)

        system_prompt = (Path(__file__).resolve().parents[1] / "prompts" / "executor_prompt.md").read_text(encoding="utf-8")
        summary = str(strategy.get("original_ppt_summary_md") or "")
        brief_by_index = {
            int(item.get("index") or offset): item
            for offset, item in enumerate(strategy.get("slide_briefs") or [], start=1)
            if isinstance(item, dict)
        }
        generated: list[str] = []
        note_sections: list[str] = []
        prior_summaries: list[str] = []
        slides = layout.get("slides") or []
        page_count = len(slides)
        revised_pages = self._selected_pages(request, page_count)
        preserved_pages: list[int] = []

        for offset, slide_layout in enumerate(slides, start=1):
            index = int(slide_layout.get("index") or offset)
            basename = f"slide_{index:02d}"
            brief = brief_by_index.get(index) or self._fallback_slide_brief(slide_layout)
            if index not in revised_pages:
                original_svg = paths["original_svg_dir"] / f"{basename}.svg"
                if not original_svg.is_file():
                    raise FileNotFoundError(
                        f"original SVG is missing for preserved revision page {index}: {original_svg}"
                    )
                svg_path = svg_dir / f"{basename}.svg"
                shutil.copy2(original_svg, svg_path)
                note_sections.append(f"# {basename}\n\n\n")
                prior_summaries.append(f"{basename}: preserved from original deck")
                generated.append(str(svg_path))
                preserved_pages.append(index)
                continue
            result = self._generate_revision_page_with_retry(
                job_id=job["job_id"],
                system_prompt=system_prompt,
                request=request,
                summary=summary,
                strategy=strategy,
                slide_layout=slide_layout,
                slide_brief=brief,
                page_count=page_count,
                basename=basename,
                prior_summaries=prior_summaries[-2:],
            )
            svg = self._clean_svg(str(result.get("svg") or ""))
            if not svg:
                raise RuntimeError(f"revision page {index} did not produce SVG")
            svg_path = svg_dir / f"{basename}.svg"
            svg_path.write_text(svg, encoding="utf-8")
            notes = str(result.get("notes") or brief.get("speaker_note") or "").strip()
            note_sections.append(f"# {basename}\n\n{notes}\n")
            summary_line = str(result.get("summary") or brief.get("purpose") or brief.get("title") or basename).strip()
            prior_summaries.append(f"{basename}: {summary_line}")
            generated.append(str(svg_path))

        (notes_dir / "total.md").write_text("\n---\n\n".join(note_sections), encoding="utf-8")
        return {
            "svg_files": generated,
            "notes_total": str(notes_dir / "total.md"),
            "revised_pages": sorted(revised_pages),
            "preserved_pages": preserved_pages,
        }

    @staticmethod
    def _selected_pages(request: dict[str, Any], page_count: int) -> set[int]:
        if request.get("revision_scope") != "selected_pages":
            return set(range(1, page_count + 1))
        try:
            selected = {int(page) for page in request.get("pages") or []}
        except (TypeError, ValueError) as exc:
            raise ValueError("selected revision pages must be integers") from exc
        invalid = sorted(page for page in selected if page < 1 or page > page_count)
        if not selected or invalid:
            raise ValueError(
                f"selected revision requires pages within 1-{page_count}; invalid={invalid}"
            )
        return selected

    def _generate_revision_page_with_retry(self, **kwargs: Any) -> dict[str, Any]:
        last_error: Exception | None = None
        job_id = kwargs.get("job_id", "unknown")
        page_kwargs = {key: value for key, value in kwargs.items() if key != "job_id"}
        for attempt in range(2):
            try:
                return self._generate_revision_page(attempt=attempt, **page_kwargs)
            except Exception as exc:
                last_error = exc
                self.runtime.log(
                    job_id,
                    "revision page generation retry",
                    attempt=attempt + 1,
                    error=str(exc),
                )
        raise RuntimeError(f"revision page generation failed after retry: {last_error}") from last_error

    def _generate_revision_page(
        self,
        *,
        attempt: int,
        system_prompt: str,
        request: dict[str, Any],
        summary: str,
        strategy: dict[str, Any],
        slide_layout: dict[str, Any],
        slide_brief: dict[str, Any],
        page_count: int,
        basename: str,
        prior_summaries: list[str],
    ) -> dict[str, Any]:
        prompt = "\n\n".join(
            [
                "Generate one beautified replacement SVG page for an existing PPT slide.",
                "Return JSON with keys: svg, notes, summary.",
                "Use the global strategy, but base this page on the current slide layout JSON.",
                "Preserve required content and required images from the original slide. You may redesign layout, hierarchy, colors, and typography.",
                "When placing original images, use slide_layout.images[].svg_href exactly as the <image href=\"...\"> value.",
                "Do not use slide_layout.images[].asset directly in SVG href; asset is project-root relative, while svg_href is relative to svg_output/*.svg.",
                "Use visual_assets.generated_images or slide_brief.image_slots only when they fit this slide; use their svg_href/path values with <image href=\"...\">.",
                "Use <use data-icon=\"library/name\" .../> placeholders from visual_assets.icon_library when icons improve comprehension or scanability.",
                f"Attempt: {attempt + 1}",
                f"Slide file stem: {basename}",
                f"Page count: {page_count}",
                f"Request JSON:\n{json.dumps(request, ensure_ascii=False, indent=2)}",
                f"Original PPT summary:\n{summary}",
                f"Global beautify strategy JSON:\n{json.dumps(strategy.get('beautify_strategy') or {}, ensure_ascii=False, indent=2)}",
                f"Visual assets JSON:\n{json.dumps(strategy.get('visual_assets') or {}, ensure_ascii=False, indent=2)}",
                f"Design spec:\n{strategy.get('design_spec_md') or ''}",
                f"Spec lock JSON:\n{json.dumps(strategy.get('spec_lock') or {}, ensure_ascii=False, indent=2)}",
                f"Prior revised slide summaries:\n{json.dumps(prior_summaries, ensure_ascii=False, indent=2)}",
                f"Current slide brief:\n{json.dumps(slide_brief, ensure_ascii=False, indent=2)}",
                f"Current slide full layout JSON:\n{json.dumps(slide_layout, ensure_ascii=False, indent=2)}",
                "SVG requirements: complete <svg>, viewBox/width/height from spec_lock canvas, inline attributes only, no style blocks, no foreignObject, no mask.",
            ]
        )
        callback = getattr(self.runtime, "call_llm_json", call_llm_json)
        result = callback(prompt, system_prompt)
        result["svg"] = RevisionWorkflow._clean_svg(str(result.get("svg") or ""))
        result["notes"] = str(result.get("notes") or "")
        result["summary"] = str(result.get("summary") or "")
        return result

    def _write_strategy_artifacts(self, project_path: Path, strategy: dict[str, Any]) -> None:
        sources_dir = project_path / "sources"
        sources_dir.mkdir(parents=True, exist_ok=True)
        (sources_dir / "original_ppt_summary.md").write_text(
            str(strategy.get("original_ppt_summary_md") or ""),
            encoding="utf-8",
        )
        (project_path / "design_spec.md").write_text(
            str(strategy.get("design_spec_md") or ""),
            encoding="utf-8",
        )
        (project_path / "spec_lock.md").write_text(
            json.dumps(strategy.get("spec_lock") or {}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (project_path / ".service_strategy.json").write_text(
            json.dumps(strategy, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def _normalize_strategy(payload: dict[str, Any], layout: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            payload = {}
        slide_briefs = payload.get("slide_briefs")
        if not isinstance(slide_briefs, list) or not slide_briefs:
            slide_briefs = [RevisionWorkflow._fallback_slide_brief(slide) for slide in layout.get("slides") or []]
        canvas_format = request.get("canvas_format") or layout.get("deck", {}).get("canvas_format") or "ppt169"
        width, height = CANVAS_DIMENSIONS.get(canvas_format, CANVAS_DIMENSIONS["ppt169"])
        spec_lock = payload.get("spec_lock") if isinstance(payload.get("spec_lock"), dict) else {}
        spec_lock.setdefault("canvas", {"format": canvas_format, "width": width, "height": height, "viewbox": f"0 0 {width} {height}"})
        spec_lock.setdefault("outline", slide_briefs)
        return {
            "original_ppt_summary_md": str(payload.get("original_ppt_summary_md") or "# Original PPT Summary\n\nGenerated from layout analysis.\n"),
            "beautify_strategy": payload.get("beautify_strategy") if isinstance(payload.get("beautify_strategy"), dict) else {},
            "design_spec_md": str(payload.get("design_spec_md") or "# Revision Design Spec\n\nBeautify the original deck while preserving content.\n"),
            "spec_lock": spec_lock,
            "slide_briefs": slide_briefs,
            "image": payload.get("image") if isinstance(payload.get("image"), dict) else {},
            "icon": payload.get("icon") if isinstance(payload.get("icon"), dict) else {},
        }

    @staticmethod
    def _extract_layout_full(pptx_path: Path, project_path: Path, request: dict[str, Any]) -> dict[str, Any]:
        presentation = Presentation(str(pptx_path))
        canvas_format = request.get("canvas_format") or "ppt169"
        width, height = CANVAS_DIMENSIONS.get(canvas_format, CANVAS_DIMENSIONS["ppt169"])
        image_dir = project_path / "images" / "reference"
        image_dir.mkdir(parents=True, exist_ok=True)
        slides: list[dict[str, Any]] = []
        for slide_index, slide in enumerate(presentation.slides, start=1):
            slide_payload: dict[str, Any] = {
                "index": slide_index,
                "texts": [],
                "images": [],
                "shapes": [],
                "tables": [],
                "charts": [],
            }
            colors: set[str] = set()
            for z_order, shape in enumerate(slide.shapes, start=1):
                common = {
                    "id": f"s{slide_index:02d}_sh{z_order:02d}",
                    "name": getattr(shape, "name", ""),
                    "shape_type": str(getattr(shape, "shape_type", "")),
                    "z_order": z_order,
                    "bbox": RevisionWorkflow._bbox(shape, presentation, width, height),
                }
                fill_color = RevisionWorkflow._fill_color(shape)
                line_color = RevisionWorkflow._line_color(shape)
                if fill_color:
                    colors.add(fill_color)
                if line_color:
                    colors.add(line_color)

                if getattr(shape, "has_text_frame", False) and shape.text_frame and shape.text_frame.text.strip():
                    text_info = {
                        **common,
                        "text": shape.text_frame.text,
                        "paragraphs": RevisionWorkflow._paragraphs(shape),
                        "role_guess": "title" if len(slide_payload["texts"]) == 0 else "body",
                        "fill": fill_color,
                        "line": line_color,
                    }
                    for paragraph in text_info["paragraphs"]:
                        for run in paragraph.get("runs", []):
                            if run.get("color"):
                                colors.add(run["color"])
                    slide_payload["texts"].append(text_info)
                elif getattr(shape, "shape_type", None) == MSO_SHAPE_TYPE.PICTURE:
                    asset = RevisionWorkflow._write_picture_asset(shape, image_dir, project_path, slide_index, len(slide_payload["images"]) + 1)
                    slide_payload["images"].append({**common, "asset": asset, "svg_href": f"../{asset}"})
                elif getattr(shape, "has_table", False):
                    slide_payload["tables"].append({**common, "rows": len(shape.table.rows), "columns": len(shape.table.columns)})
                elif getattr(shape, "has_chart", False):
                    slide_payload["charts"].append({**common, "chart_type": str(shape.chart.chart_type)})
                else:
                    slide_payload["shapes"].append({**common, "fill": fill_color, "line": line_color})
            slide_payload["visual_summary"] = {
                "text_box_count": len(slide_payload["texts"]),
                "image_count": len(slide_payload["images"]),
                "shape_count": len(slide_payload["shapes"]),
                "table_count": len(slide_payload["tables"]),
                "chart_count": len(slide_payload["charts"]),
                "colors": sorted(colors),
            }
            slides.append(slide_payload)
        return {
            "deck": {
                "source": str(pptx_path),
                "slide_count": len(slides),
                "canvas_format": canvas_format,
                "width": width,
                "height": height,
                "slide_width_emu": int(presentation.slide_width),
                "slide_height_emu": int(presentation.slide_height),
            },
            "slides": slides,
        }

    @staticmethod
    def _bbox(shape: Any, presentation: Presentation, width: int, height: int) -> dict[str, float]:
        return {
            "x": round(int(shape.left) / int(presentation.slide_width) * width, 2),
            "y": round(int(shape.top) / int(presentation.slide_height) * height, 2),
            "w": round(int(shape.width) / int(presentation.slide_width) * width, 2),
            "h": round(int(shape.height) / int(presentation.slide_height) * height, 2),
        }

    @staticmethod
    def _paragraphs(shape: Any) -> list[dict[str, Any]]:
        paragraphs: list[dict[str, Any]] = []
        for paragraph in shape.text_frame.paragraphs:
            runs = []
            for run in paragraph.runs:
                runs.append(
                    {
                        "text": run.text,
                        "font": run.font.name,
                        "size": round(run.font.size.pt, 2) if run.font.size is not None else None,
                        "bold": bool(run.font.bold) if run.font.bold is not None else None,
                        "italic": bool(run.font.italic) if run.font.italic is not None else None,
                        "color": RevisionWorkflow._font_color(run),
                    }
                )
            paragraphs.append({"level": paragraph.level, "alignment": str(paragraph.alignment), "runs": runs})
        return paragraphs

    @staticmethod
    def _write_picture_asset(shape: Any, image_dir: Path, project_path: Path, slide_index: int, image_index: int) -> str:
        ext = shape.image.ext or "png"
        target = image_dir / f"slide_{slide_index:02d}_image_{image_index:02d}.{ext}"
        target.write_bytes(shape.image.blob)
        return target.relative_to(project_path).as_posix()

    @staticmethod
    def _fill_color(shape: Any) -> str | None:
        try:
            fill = shape.fill
            if fill and fill.fore_color and fill.fore_color.rgb:
                return f"#{fill.fore_color.rgb}"
        except Exception:
            return None
        return None

    @staticmethod
    def _line_color(shape: Any) -> str | None:
        try:
            line = shape.line
            if line and line.color and line.color.rgb:
                return f"#{line.color.rgb}"
        except Exception:
            return None
        return None

    @staticmethod
    def _font_color(run: Any) -> str | None:
        try:
            if run.font.color and run.font.color.rgb:
                return f"#{run.font.color.rgb}"
        except Exception:
            return None
        return None

    @staticmethod
    def _fallback_slide_brief(slide: dict[str, Any]) -> dict[str, Any]:
        texts = [item.get("text", "") for item in slide.get("texts") or [] if item.get("text")]
        title = texts[0].splitlines()[0] if texts else f"Slide {slide.get('index')}"
        return {
            "index": slide.get("index"),
            "title": title,
            "purpose": "Beautified revision of the original slide.",
            "key_points": texts[:4],
            "visual_direction": "Improve hierarchy, spacing, readability, and visual balance based on the original layout.",
            "required_content": texts,
            "required_images": [item.get("svg_href") or item.get("asset") for item in slide.get("images") or [] if item.get("asset")],
            "speaker_note": "",
        }

    @staticmethod
    def _clean_svg(svg: str) -> str:
        cleaned = svg.strip()
        cleaned = re.sub(r"^```(?:svg|xml)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
        start = cleaned.find("<svg")
        end = cleaned.rfind("</svg>")
        if start >= 0 and end >= start:
            cleaned = cleaned[start:end + len("</svg>")]
        return cleaned.strip()

    @staticmethod
    def _read_sources(paths: list[Path]) -> str:
        chunks: list[str] = []
        for path in paths:
            text = path.read_text(encoding="utf-8", errors="replace")
            chunks.append(f"# Source: {path.name}\n\n{text}")
        return "\n\n".join(chunks)

    @staticmethod
    def _first_pptx_source(source_files: list[str]) -> str | None:
        for source in source_files:
            if Path(source).suffix.lower() in PPTX_SUFFIXES:
                return source
        return None

    @staticmethod
    def _revision_brief(request: dict[str, Any]) -> str:
        lines = ["# Revision Brief", ""]
        for key in ("prompt", "topic", "language", "style", "pages", "quality_level", "extra_requirements"):
            value = request.get(key)
            if value:
                lines.extend([f"## {key}", str(value), ""])
        return "\n".join(lines)

    def _paths(self, job: dict[str, Any]) -> dict[str, Any]:
        workspace = Path(job["workspace"])
        analysis_dir = workspace / "analysis"
        runtime_inputs = json.loads((analysis_dir / "runtime_inputs.json").read_text(encoding="utf-8"))
        return {
            "workspace": workspace,
            "reference_pptx": Path(runtime_inputs["reference_pptx"]),
            "project_path": Path(runtime_inputs["project_path"]),
            "source_markdown": [Path(path) for path in runtime_inputs["source_markdown"]],
            "analysis_dir": analysis_dir,
            "validation_dir": Path(runtime_inputs["validation_dir"]),
            "layout_full": Path(runtime_inputs["layout_full"]),
            "revision_brief": Path(runtime_inputs["revision_brief"]),
            "original_svg_dir": Path(runtime_inputs["original_svg_dir"]),
        }
