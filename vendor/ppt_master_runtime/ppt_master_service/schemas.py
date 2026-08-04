from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


WorkflowMode = Literal[
    "auto",
    "topic_research",
    "document_to_ppt",
    "template_fill_pptx",
    "template_guided_svg",
    "revision",
    "brand_style",
    "image_heavy",
    "mirror_template",
    "resume_execute",
    "verify_charts",
    "generate_audio",
    "customize_animations",
    "live_preview",
    "visual_review",
    "create_template",
    "create_brand",
]

JobStatus = Literal[
    "queued",
    "running",
    "generating_strategy",
    "generating_svg",
    "preparing_brand",
    "planning_images",
    "loading_mirror_template",
    "revising_pptx",
    "analyzing_template",
    "planning_fill",
    "checking_fill_plan",
    "applying_template",
    "validating_output",
    "checking_quality",
    "exporting",
    "success",
    "failed",
    "waiting_user",
    "waiting_for_llm_svg_generation",
    "waiting_for_image_generation",
]

InteractionAction = Literal["confirm", "revise"]
GenerationMode = Literal["continuous", "split"]


class GeneratePPTRequest(BaseModel):
    mode: WorkflowMode = "auto"
    topic: str | None = None
    prompt: str | None = None
    pages: int | None = Field(default=None, ge=1)
    language: str | None = None
    style: str | None = None
    canvas_format: str = "ppt169"
    source_files: list[str] = Field(default_factory=list)
    source_urls: list[str] = Field(default_factory=list)
    template_pptx: str | None = None
    template_strategy: Literal["fill", "guided_svg"] = "fill"
    template_id: str | None = None
    mirror_template_id: str | None = None
    reference_pptx: str | None = None
    brand_assets: list[str] = Field(default_factory=list)
    image_assets: list[str] = Field(default_factory=list)
    intake_plan: dict[str, Any] = Field(default_factory=dict)
    output_type: str = "pptx"
    quality_level: str = "standard"
    extra_requirements: str | None = None
    project_path: str | None = None


class GeneratePPTResponse(BaseModel):
    job_id: str
    workflow: str
    status: JobStatus
    message: str


class JobStatusResponse(BaseModel):
    job_id: str
    workflow: str | None = None
    status: JobStatus
    progress: float = 0
    current_step: str | None = None
    created_at: datetime
    updated_at: datetime
    project_path: str | None = None
    workspace: str | None = None
    output_file: str | None = None
    error: str | None = None
    error_detail: dict[str, Any] | None = None
    review: dict[str, Any] | None = None
    interaction: dict[str, Any] | None = None
    preview: dict[str, Any] | None = None
    warnings: list[str] = Field(default_factory=list)
    last_event: dict[str, Any] | None = None
    logs: list[str] = Field(default_factory=list)


class InteractionResponseRequest(BaseModel):
    action: InteractionAction
    revision: int = Field(ge=1)
    strategy_digest: str = Field(min_length=8)
    confirmations: dict[str, dict[str, Any]] = Field(default_factory=dict)
    generation_mode: GenerationMode = "continuous"
    feedback: str | None = None


class InteractionSubmissionResponse(BaseModel):
    job_id: str
    status: JobStatus
    accepted: bool
    resume_scheduled: bool
    interaction: dict[str, Any] | None = None
    receipt: dict[str, Any] | None = None


class TemplateInfo(BaseModel):
    template_id: str
    name: str
    kind: str | None = None
    description: str | None = None
    scenes: list[str] = Field(default_factory=list)
    styles: list[str] = Field(default_factory=list)
    best_for: list[str] = Field(default_factory=list)
    avoid_for: list[str] = Field(default_factory=list)
    source_path: str | None = None
    tags: list[str] = Field(default_factory=list)
    path: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ReviewResult(BaseModel):
    passed: bool
    score: float | None = None
    issues: list[str] = Field(default_factory=list)
    notes: str | None = None
