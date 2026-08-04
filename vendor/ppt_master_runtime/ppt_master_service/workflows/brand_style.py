from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from .document_to_ppt import DocumentToPPTWorkflow


class BrandStyleWorkflow(DocumentToPPTWorkflow):
    name = "brand_style"
    is_stub = False

    def prepare(self, job: dict[str, Any]) -> dict[str, Any]:
        self.runtime.store.update_job(job["job_id"], status="preparing_brand")
        result = super().prepare(job)
        current_job = self.runtime.store.get_job(job["job_id"]) or job
        project_path = Path(current_job["project_path"])
        request = current_job.get("request", {})

        copied_assets = self._copy_brand_assets(request.get("brand_assets") or [], project_path)
        guide = self._write_brand_guide(project_path, request, copied_assets)
        lock = self._write_brand_spec_lock(project_path, request, copied_assets)
        self.runtime.log(
            job["job_id"],
            "brand style constraints prepared",
            brand_guide=str(guide),
            spec_lock=str(lock),
            copied_assets=copied_assets,
        )
        return {**result, "brand_guide": str(guide), "brand_assets": copied_assets, "spec_lock": str(lock)}

    def review(self, job: dict[str, Any]) -> dict[str, Any]:
        result = super().review(job)
        current_job = self.runtime.store.get_job(job["job_id"]) or job
        project_path = Path(current_job["project_path"])
        guide = project_path / "sources" / "brand_guide.md"
        if not guide.exists() or guide.stat().st_size <= 0:
            raise RuntimeError("brand_style review failed: brand_guide.md was not created")
        assets = list((project_path / "images" / "brand_assets").glob("*"))
        request_assets = (current_job.get("request") or {}).get("brand_assets") or []
        if request_assets and not assets:
            raise RuntimeError("brand_style review failed: brand_assets were provided but not copied into project/images/brand_assets")
        self.runtime.log(job["job_id"], "brand style review complete", brand_asset_count=len(assets))
        return {**result, "brand_review": {"brand_guide": str(guide), "brand_asset_count": len(assets)}}

    @staticmethod
    def _copy_brand_assets(assets: list[str], project_path: Path) -> list[str]:
        target_dir = project_path / "images" / "brand_assets"
        target_dir.mkdir(parents=True, exist_ok=True)
        copied: list[str] = []
        for item in assets:
            path = Path(item).expanduser().resolve()
            if not path.exists():
                raise FileNotFoundError(f"brand asset does not exist: {item}")
            if path.is_dir():
                for child in path.iterdir():
                    if child.is_file():
                        target = target_dir / child.name
                        shutil.copy2(child, target)
                        copied.append(str(target))
            elif path.is_file():
                target = target_dir / path.name
                shutil.copy2(path, target)
                copied.append(str(target))
        return copied

    @staticmethod
    def _write_brand_guide(project_path: Path, request: dict[str, Any], copied_assets: list[str]) -> Path:
        guide = project_path / "sources" / "brand_guide.md"
        guide.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "# Brand Guide",
            "",
            "Use these brand constraints when creating the presentation.",
            "",
            "## Request Style",
            str(request.get("style") or "Not specified"),
            "",
            "## Extra Requirements",
            str(request.get("extra_requirements") or "Not specified"),
            "",
            "## Brand Assets",
        ]
        lines.extend(f"- {asset}" for asset in copied_assets)
        if not copied_assets:
            lines.append("- No binary brand assets were provided. Infer brand direction only from text requirements.")
        guide.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return guide

    @staticmethod
    def _write_brand_spec_lock(project_path: Path, request: dict[str, Any], copied_assets: list[str]) -> Path:
        lock = project_path / "spec_lock.md"
        lines = [
            "# Spec Lock - Brand Constraints",
            "",
            "This preliminary lock is written by brand_style before Strategist. Strategist may expand it, but should preserve the brand intent.",
            "",
            "## Brand Intent",
            str(request.get("style") or request.get("extra_requirements") or "Use provided brand assets and company style."),
            "",
            "## Brand Assets",
        ]
        lines.extend(f"- {asset}" for asset in copied_assets)
        lock.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return lock
