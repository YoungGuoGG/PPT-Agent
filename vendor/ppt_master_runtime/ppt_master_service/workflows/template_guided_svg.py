from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from ..tools import template_tools
from ..tools.template_guidance import build_template_context
from ..tools.template_guided_policy import write_template_bindings
from .document_to_ppt import DocumentToPPTWorkflow


class TemplateGuidedSVGWorkflow(DocumentToPPTWorkflow):
    name = "template_guided_svg"
    is_stub = False

    def prepare(self, job: dict[str, Any]) -> dict[str, Any]:
        request = job.get("request") or {}
        template_value = request.get("template_pptx")
        if not template_value:
            raise ValueError("template_guided_svg requires template_pptx")
        template_path = Path(str(template_value)).expanduser().resolve()
        if not template_path.exists() or not template_path.is_file():
            raise FileNotFoundError(f"template_pptx does not exist: {template_value}")
        if template_path.suffix.lower() != ".pptx":
            raise ValueError("template_guided_svg currently requires a .pptx template")

        parent_request = dict(request)
        parent_request["source_files"] = [
            value
            for value in request.get("source_files", [])
            if Path(str(value)).expanduser().resolve() != template_path
        ]
        result = super().prepare({**job, "request": parent_request})
        project_path = Path(result["project_path"])
        templates_dir = project_path / "templates"
        import_dir = templates_dir / "source_import"
        templates_dir.mkdir(parents=True, exist_ok=True)
        local_template = templates_dir / "source_template.pptx"
        if local_template.resolve() != template_path:
            shutil.copy2(template_path, local_template)

        import_result = template_tools.import_pptx_template(str(local_template), str(import_dir))
        if int(getattr(import_result, "returncode", 1)) != 0:
            detail = str(
                getattr(import_result, "stderr", "")
                or getattr(import_result, "stdout", "")
                or "unknown import error"
            ).strip()
            raise RuntimeError(f"template PPTX import failed: {detail}")

        context = build_template_context(import_dir)
        context_path = templates_dir / "template_context.json"
        context_path.write_text(
            json.dumps(context, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        page_count = int(request.get("pages") or 6)
        bindings_path = write_template_bindings(project_path, page_count)
        bindings = json.loads(bindings_path.read_text(encoding="utf-8"))
        self.runtime.log(
            job["job_id"],
            "template guided SVG context prepared",
            template_pptx=str(local_template),
            import_dir=str(import_dir),
            template_context=str(context_path),
            template_bindings=str(bindings_path),
            binding_page_count=page_count,
            binding_roles=[
                item["role"] for item in bindings["bindings"].values()
            ],
        )
        return {
            **result,
            "template_pptx": str(local_template),
            "template_import_dir": str(import_dir),
            "template_context": str(context_path),
            "template_bindings": str(bindings_path),
        }
