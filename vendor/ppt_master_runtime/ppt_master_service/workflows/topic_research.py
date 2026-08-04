from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .document_to_ppt import DocumentToPPTWorkflow
from .. import llm_runtime
from ..tools import project_tools


class TopicResearchWorkflow(DocumentToPPTWorkflow):
    name = "topic_research"
    is_stub = False

    def prepare(self, job: dict[str, Any]) -> dict[str, Any]:
        request = job.get("request", {})
        job_id = job["job_id"]
        project_path = request.get("project_path")
        if project_path:
            project = Path(project_path)
            if not project.exists() or not project.is_dir():
                raise RuntimeError(f"project_path does not exist: {project_path}")
        else:
            project = Path(project_tools.create_project(job_id, request.get("canvas_format") or "ppt169"))

        self.runtime.store.update_job(job_id, project_path=str(project))
        self.runtime.log(job_id, "topic research project ready", project_path=str(project))
        (project / "request.json").write_text(
            json.dumps(request, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.runtime.store.update_job(job_id, status="generating_strategy")
        research_path = llm_runtime.run_topic_research(
            project,
            request,
            call_json=getattr(self.runtime, "call_llm_json", None),
        )
        self.runtime.log(job_id, "topic research markdown generated", research_path=research_path)
        validation = project_tools.validate_project(str(project))
        self.runtime.log(job_id, "validated project", result=validation)
        return {"ok": True, "stage": "prepare", "project_path": str(project), "research_path": research_path}
