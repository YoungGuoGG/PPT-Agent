from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse

from .conformance import WorkflowExecutionError
from .job_store import JobStore, PACKAGE_DIR
from .runtime import Runtime
from .security import safe_response_path
from .schemas import (
    GeneratePPTRequest,
    GeneratePPTResponse,
    InteractionResponseRequest,
    InteractionSubmissionResponse,
    JobStatusResponse,
    TemplateInfo,
)
from .tools.template_tools import scan_templates


app = FastAPI(title="PPT Master Local Agent Service", version="0.1.0")
store = JobStore()
runtime = Runtime(store)


@app.post("/api/ppt/generate", response_model=GeneratePPTResponse)
def generate_ppt(
    request: GeneratePPTRequest,
    background_tasks: BackgroundTasks,
) -> GeneratePPTResponse:
    try:
        job = runtime.create_job(request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    background_tasks.add_task(runtime.run_job, job["job_id"])
    return GeneratePPTResponse(
        job_id=job["job_id"],
        workflow=job["workflow"],
        status=job["status"],
        message="job queued",
    )


@app.get("/api/ppt/status/{job_id}", response_model=JobStatusResponse)
def get_status(job_id: str) -> JobStatusResponse:
    job = store.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return JobStatusResponse(
        job_id=job["job_id"],
        workflow=job.get("workflow"),
        status=job["status"],
        progress=float(job.get("progress") or 0),
        current_step=job.get("current_step"),
        created_at=job["created_at"],
        updated_at=job["updated_at"],
        project_path=safe_response_path(job.get("project_path")),
        workspace=safe_response_path(job.get("workspace")),
        output_file=safe_response_path(job.get("output_file")),
        error=job.get("error"),
        error_detail=job.get("error_detail"),
        review=job.get("review"),
        interaction=job.get("interaction"),
        preview=job.get("preview"),
        warnings=list(job.get("warnings") or []),
        last_event=store.get_last_event(job_id),
        logs=store.get_logs(job_id),
    )


@app.post(
    "/api/ppt/jobs/{job_id}/interactions/{interaction_id}/responses",
    response_model=InteractionSubmissionResponse,
)
def respond_to_interaction(
    job_id: str,
    interaction_id: str,
    request: InteractionResponseRequest,
    background_tasks: BackgroundTasks,
) -> InteractionSubmissionResponse:
    try:
        submission = runtime.submit_interaction(job_id, interaction_id, request)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except WorkflowExecutionError as exc:
        status_code = (
            422
            if exc.detail.code == "interaction_validation_failed"
            else 409
        )
        raise HTTPException(
            status_code=status_code,
            detail=exc.detail.to_dict(),
        ) from exc

    if submission.resume_required:
        background_tasks.add_task(runtime.run_job, job_id)
    job = store.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return InteractionSubmissionResponse(
        job_id=job_id,
        status=job["status"],
        accepted=submission.accepted,
        resume_scheduled=submission.resume_required,
        interaction=(
            submission.interaction.to_dict()
            if submission.interaction is not None
            else None
        ),
        receipt=submission.receipt,
    )


@app.get("/api/ppt/download/{job_id}")
def download(job_id: str) -> Any:
    job = store.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    output_file = job.get("output_file")
    if not output_file:
        return {"job_id": job_id, "status": job["status"], "file_path": None}
    path = Path(output_file)
    if path.exists() and path.is_file():
        return FileResponse(path, filename=path.name)
    return {"job_id": job_id, "status": job["status"], "file_path": safe_response_path(output_file)}


@app.get("/api/templates", response_model=list[TemplateInfo])
def list_templates() -> list[TemplateInfo]:
    metadata_dir = PACKAGE_DIR / "template_metadata"
    metadata_dir.mkdir(parents=True, exist_ok=True)
    templates: list[TemplateInfo] = []
    for path in sorted(metadata_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        items = payload if isinstance(payload, list) else [payload]
        for item in items:
            if isinstance(item, dict):
                templates.append(_template_from_dict(item, path))
    if not templates:
        templates = [_template_from_dict(item, metadata_dir / "generated.json") for item in scan_templates()]
    return templates


def _template_from_dict(item: dict[str, Any], path: Path) -> TemplateInfo:
    template_id = str(item.get("template_id") or item.get("id") or path.stem)
    return TemplateInfo(
        template_id=template_id,
        name=str(item.get("name") or template_id),
        kind=item.get("kind"),
        description=item.get("description"),
        scenes=list(item.get("scenes") or []),
        styles=list(item.get("styles") or []),
        best_for=list(item.get("best_for") or []),
        avoid_for=list(item.get("avoid_for") or []),
        source_path=item.get("source_path"),
        tags=list(item.get("tags") or []),
        path=item.get("path") or item.get("source_path"),
        metadata={key: value for key, value in item.items() if key not in {"template_id", "id", "name", "kind", "description", "scenes", "styles", "best_for", "avoid_for", "source_path", "tags", "path"}},
    )
