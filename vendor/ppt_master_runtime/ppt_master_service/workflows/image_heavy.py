from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET
import zipfile

from .. import llm_runtime, visual_assets
from ..conformance import (
    ErrorKind,
    SuggestedAction,
    WorkflowError,
    WorkflowExecutionError,
    sha256_file,
)
from ..image_understanding import understand_project_images
from ..tools import export_tools, image_tools
from .document_to_ppt import DocumentToPPTWorkflow
from .image_heavy_policy import (
    ImageStrategyValidation,
    required_generated_asset_ids,
    validate_image_heavy_strategy,
)


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}


class ImageHeavyWorkflow(DocumentToPPTWorkflow):
    name = "image_heavy"
    is_stub = False

    def prepare(self, job: dict[str, Any]) -> dict[str, Any]:
        self.runtime.store.update_job(job["job_id"], status="planning_images")
        original_request = dict(job.get("request", {}))
        image_files, document_files = self._split_image_files(original_request.get("source_files") or [])
        job["request"] = {**original_request, "source_files": document_files}
        try:
            result = super().prepare(job)
        finally:
            job["request"] = original_request
        current_job = self.runtime.store.get_job(job["job_id"]) or job
        project_path = Path(current_job["project_path"])

        copied_images = self._copy_images(image_files, project_path)
        image_plan = self._write_image_plan(project_path, original_request, copied_images)
        analysis = None
        if copied_images:
            analysis = image_tools.analyze_images(str(project_path / "images" / "user")).to_dict()
            self.runtime.log(job["job_id"], "image assets analyzed", tool=analysis)
            understanding = understand_project_images(project_path, copied_images, original_request)
            self.runtime.log(
                job["job_id"],
                "image assets understood",
                image_asset_count=len(understanding.get("assets") or []),
                research_count=len(understanding.get("research") or []),
            )
        self.runtime.log(
            job["job_id"],
            "image-heavy plan prepared",
            image_plan=str(image_plan),
            copied_images=copied_images,
        )
        return {
            **result,
            "image_plan": str(image_plan),
            "copied_images": copied_images,
            "image_analysis": analysis,
        }

    def run(self, job: dict[str, Any]) -> dict[str, Any]:
        project_path = self._job_project_path(job)
        request = job.get("request", {})
        pages = int(request.get("pages") or 6)
        strategy_path = project_path / ".service_strategy.json"
        supplied = self._load_supplied_assets(project_path)
        if request.get("resume_image_generation") and strategy_path.is_file():
            strategy = json.loads(strategy_path.read_text(encoding="utf-8"))
        else:
            validator = lambda candidate: validate_image_heavy_strategy(
                candidate,
                pages=pages,
                supplied_assets=supplied,
            ).issues
            call_json = getattr(self.runtime, "call_llm_json", None)
            strategy = llm_runtime.run_strategist(
                project_path,
                request,
                validator=validator,
                max_protocol_attempts=2,
                call_json=call_json,
            )

        policy = validate_image_heavy_strategy(
            strategy,
            pages=pages,
            supplied_assets=supplied,
        )
        if not policy.passed:
            first = policy.issues[0]
            raise self._error(
                ErrorKind.PROTOCOL,
                "image.strategy.invalid",
                "strategy",
                f"image-heavy strategy is invalid: {first['code']}; required {first['required']}",
                {"issue": first},
                suggested_action=SuggestedAction.REGENERATE_STEP,
            )
        required_ids = required_generated_asset_ids(strategy, policy)
        assets = visual_assets.materialize_visual_assets(
            project_path,
            strategy,
            resume=bool(request.get("resume_image_generation")),
            required_asset_ids=required_ids,
            provider=getattr(self.runtime, "image_provider", None),
            log=lambda event, payload: self.runtime.log(job["job_id"], event, **payload),
        )
        if assets["incomplete_asset_ids"]:
            return self._waiting_for_image_generation_result(assets)

        executor = getattr(self.runtime, "run_executor", llm_runtime.run_executor)
        execution = executor(
            project_path,
            request,
            strategy,
            log=lambda event, payload: self.runtime.log(job["job_id"], event, **payload),
        )
        self.runtime.log(job["job_id"], "image-heavy SVG generation complete", result=execution)
        return {
            "ok": True,
            "stage": "run",
            "project_path": str(project_path),
            "execution": execution,
            "visual_assets": assets,
        }

    def review(self, job: dict[str, Any]) -> dict[str, Any]:
        result = super().review(job)
        project_path = self._job_project_path(job)
        image_plan = project_path / "sources" / "image_plan.md"
        if not image_plan.is_file() or image_plan.stat().st_size == 0:
            raise self._error(
                ErrorKind.ARTIFACT,
                "image.plan.missing",
                "quality",
                "image_heavy review failed: image_plan.md was not created",
                {"path": str(image_plan)},
            )
        strategy = json.loads((project_path / ".service_strategy.json").read_text(encoding="utf-8"))
        pages = int((job.get("request") or {}).get("pages") or len(strategy.get("outline") or []))
        policy = validate_image_heavy_strategy(
            strategy,
            pages=pages,
            supplied_assets=self._load_supplied_assets(project_path),
        )
        required_ids = required_generated_asset_ids(strategy, policy)
        manifest_path = project_path / "images" / "image_prompts.json"
        verification = visual_assets.verify_image_manifest(
            project_path,
            manifest_path,
            required_asset_ids=required_ids,
        )
        if not verification.ready:
            raise self._error(
                ErrorKind.ARTIFACT,
                "image.quality.assets",
                "quality",
                f"image-heavy review found incomplete assets: {list(verification.incomplete_asset_ids)}",
                {"issues": list(verification.issues)},
            )
        verified_names = {str(item["filename"]) for item in verification.verified}
        covered_pages: set[int] = set()
        for svg_path in sorted((project_path / "svg_output").glob("*.svg")):
            page_index = self._page_index(svg_path)
            if page_index not in policy.content_pages:
                continue
            try:
                root = ET.parse(svg_path).getroot()
            except ET.ParseError as exc:
                raise self._error(
                    ErrorKind.QUALITY,
                    "image.quality.svg_xml",
                    "quality",
                    f"cannot inspect raster references in {svg_path.name}: {exc}",
                    {"file": str(svg_path)},
                ) from exc
            hrefs = {
                str(value)
                for node in root.iter()
                for key, value in node.attrib.items()
                if key == "href" or key.endswith("}href")
            }
            if any(Path(href).name in verified_names for href in hrefs):
                covered_pages.add(page_index)
        if len(covered_pages) < policy.required_raster_pages:
            raise self._error(
                ErrorKind.QUALITY,
                "image.quality.coverage",
                "quality",
                f"verified raster assets appear on {len(covered_pages)} content pages; required {policy.required_raster_pages}",
                {
                    "content_pages": list(policy.content_pages),
                    "covered_pages": sorted(covered_pages),
                    "required_raster_pages": policy.required_raster_pages,
                },
                suggested_action=SuggestedAction.REPAIR,
            )
        return {
            **result,
            "image_review": {
                "image_plan": str(image_plan),
                "manifest": str(manifest_path),
                "content_pages": list(policy.content_pages),
                "covered_pages": sorted(covered_pages),
                "required_raster_pages": policy.required_raster_pages,
                "verified_images": list(verification.verified),
            },
        }

    def export(self, job: dict[str, Any]) -> dict[str, Any]:
        project_path = self._job_project_path(job)
        self.runtime.store.update_job(job["job_id"], status="exporting")
        exporter = getattr(self.runtime, "export_project", export_tools.export_project_to_pptx)
        output_file = exporter(str(project_path))
        output = Path(output_file)
        if not output.is_file() or not zipfile.is_zipfile(output):
            raise self._error(
                ErrorKind.ARTIFACT,
                "image.export.unreadable",
                "export",
                f"image-heavy export is not a readable PPTX zip: {output}",
                {"output_file": str(output)},
            )
        with zipfile.ZipFile(output) as archive:
            media = [name for name in archive.namelist() if name.startswith("ppt/media/") and not name.endswith("/")]
        manifest = json.loads((project_path / "images" / "image_prompts.json").read_text(encoding="utf-8"))
        required_media = len(
            {
                str(item.get("filename"))
                for item in manifest.get("items") or []
                if item.get("status") == "Generated"
            }
        )
        if len(media) < required_media:
            raise self._error(
                ErrorKind.QUALITY,
                "image.export.media_missing",
                "export",
                f"PPTX contains {len(media)} media files; expected at least {required_media}",
                {"media": media, "required_media": required_media},
            )
        self.runtime.store.update_job(job["job_id"], output_file=str(output))
        return {
            "ok": True,
            "stage": "export",
            "output_file": str(output),
            "image_media_count": len(media),
        }

    @staticmethod
    def _split_image_files(source_files: list[str]) -> tuple[list[str], list[str]]:
        image_files: list[str] = []
        document_files: list[str] = []
        for item in source_files:
            if Path(item).suffix.lower() in IMAGE_EXTENSIONS:
                image_files.append(item)
            else:
                document_files.append(item)
        return image_files, document_files

    @staticmethod
    def _copy_images(image_files: list[str], project_path: Path) -> list[str]:
        target_dir = project_path / "images" / "user"
        target_dir.mkdir(parents=True, exist_ok=True)
        copied: list[str] = []
        for item in image_files:
            source = Path(item).expanduser().resolve()
            if not source.is_file():
                raise FileNotFoundError(f"image source file does not exist: {item}")
            target = target_dir / source.name
            if not target.is_file() or sha256_file(target) != sha256_file(source):
                shutil.copy2(source, target)
            copied.append(str(target))
        return copied

    @staticmethod
    def _write_image_plan(project_path: Path, request: dict[str, Any], copied_images: list[str]) -> Path:
        plan = project_path / "sources" / "image_plan.md"
        plan.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "# Image Plan",
            "",
            "Use verified raster assets on at least half of all content pages, rounded up.",
            "The workflow must wait when required raster assets are unavailable; SVG-only fallback is not successful image-heavy output.",
            "",
            "## Visual Intent",
            str(request.get("style") or request.get("extra_requirements") or request.get("prompt") or "Image-heavy presentation"),
            "",
            "## Supplied Raster Assets",
        ]
        lines.extend(f"- {path}" for path in copied_images)
        if not copied_images:
            lines.append("- No supplied raster assets; generate the required manifest assets before SVG execution.")
        plan.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return plan

    @staticmethod
    def _load_supplied_assets(project_path: Path) -> list[dict[str, Any]]:
        for candidate in (project_path / ".image_assets.json", project_path / "image_understanding.json"):
            if not candidate.is_file():
                continue
            try:
                payload = json.loads(candidate.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            assets = payload.get("assets") if isinstance(payload, dict) else None
            if isinstance(assets, list):
                return [dict(item) for item in assets if isinstance(item, dict)]
        return []

    @staticmethod
    def _waiting_for_image_generation_result(assets: dict[str, Any]) -> dict[str, Any]:
        incomplete = list(assets["incomplete_asset_ids"])
        detail = WorkflowError(
            kind=ErrorKind.DEPENDENCY,
            code="image.generation.incomplete",
            step="acquire_images",
            message=f"required image assets are incomplete: {', '.join(incomplete)}",
            location={"asset_ids": incomplete, "manifest": assets["manifest_path"]},
            retryable=True,
            suggested_action=SuggestedAction.RETRY,
        )
        return {
            "ok": True,
            "stage": "run",
            "status": "waiting_for_image_generation",
            "message": detail.message,
            "error_detail": detail.to_dict(),
            "visual_assets": assets,
        }

    @staticmethod
    def _page_index(path: Path) -> int:
        match = re.search(r"(\d+)", path.stem)
        return int(match.group(1)) if match else 0

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
