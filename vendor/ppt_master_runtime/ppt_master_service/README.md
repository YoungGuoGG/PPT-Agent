# PPT Master Local Agent Service

This package wraps PPT Master as a local FastAPI service. It can create jobs, route workflows, persist local state, run deterministic PPT Master scripts, and now call an OpenAI-compatible LLM runtime for Strategist and Executor phases.

The service still does not replace the full manual quality judgment of a skilled PPT Master operator. It provides an automated MVP path for local experimentation.

## Environment

Required for LLM generation:

```bash
set OPENAI_API_KEY=...
```

Optional:

```bash
set OPENAI_MODEL=gpt-4o-mini
set OPENAI_BASE_URL=https://api.openai.com/v1
```

`OPENAI_BASE_URL` may point to an OpenAI-compatible provider. API keys are read only from environment variables; never hard-code them in this repository.

## Start

From the `ppt-master` repo root:

```bash
uvicorn ppt_master_service.main:app --host 0.0.0.0 --port 8510 --reload
```

## Submit A Topic-Only Job

```bash
curl -X POST http://127.0.0.1:8510/api/ppt/generate ^
  -H "Content-Type: application/json" ^
  -d "{\"mode\":\"auto\",\"topic\":\"AI agent trends\",\"pages\":5,\"language\":\"en\"}"
```

Routing:

```text
topic_research -> document_to_ppt
```

Artifacts:

```text
ppt_master_service/jobs/<job_id>/workspace/request.json
<project>/sources/research.md
<project>/design_spec.md
<project>/spec_lock.md
<project>/svg_output/*.svg
<project>/notes/total.md
<project>/exports/*.pptx
```

## Submit A Source Markdown Job

```bash
curl -X POST http://127.0.0.1:8510/api/ppt/generate ^
  -H "Content-Type: application/json" ^
  -d "{\"mode\":\"document_to_ppt\",\"source_files\":[\"E:/path/source.md\"],\"pages\":6,\"language\":\"en\"}"
```

The workflow creates a project, converts/copies sources into `<project>/sources/`, asks the persistent Strategist Agent for a draft, pauses for the Eight Confirmations, and only then materializes the locked design and starts the persistent Executor Agent.

## Interactive Document Workflow

The `document_to_ppt` API is a pause/resume workflow. The client owns the presentation of the confirmation UI; the Agent core owns the interaction digest, receipt, checkpoint, and idempotent resume behavior.

Create the job:

```powershell
$job = Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:8510/api/ppt/generate `
  -ContentType application/json `
  -Body (@{
    mode = "document_to_ppt"
    source_files = @("E:/path/source.md")
    pages = 6
    language = "zh-CN"
  } | ConvertTo-Json -Depth 10)
```

Poll until the service returns `waiting_user`, then render the eight objects under `interaction.confirmations` in the desktop or web UI:

```powershell
do {
  Start-Sleep -Milliseconds 500
  $status = Invoke-RestMethod "http://127.0.0.1:8510/api/ppt/status/$($job.job_id)"
} until ($status.status -in @("waiting_user", "failed"))

$status.interaction.confirmations | ConvertTo-Json -Depth 20
```

Confirm the displayed values. To request a revision, change `action` to `revise` and provide a non-empty `feedback` string instead:

```powershell
$interaction = $status.interaction
$response = @{
  action = "confirm"
  revision = $interaction.revision
  strategy_digest = $interaction.strategy_digest
  confirmations = $interaction.confirmations
  generation_mode = "continuous"
} | ConvertTo-Json -Depth 20

Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8510/api/ppt/jobs/$($job.job_id)/interactions/$($interaction.interaction_id)/responses" `
  -ContentType application/json `
  -Body $response
```

Continue polling. `status.preview.url` appears after the confirmed design, formula, and visual-asset phases finish; `success` is emitted only after exact-page SVG quality, ordered notes, deterministic post-processing, and PPTX page-count verification pass:

```powershell
do {
  Start-Sleep -Seconds 1
  $status = Invoke-RestMethod "http://127.0.0.1:8510/api/ppt/status/$($job.job_id)"
  $status | Select-Object status,current_step,progress,preview,error
} until ($status.status -in @("success", "failed"))

Invoke-WebRequest `
  -Uri "http://127.0.0.1:8510/api/ppt/download/$($job.job_id)" `
  -OutFile result.pptx
```

Submitting the same accepted confirmation again is idempotent and does not schedule a second resume. Submitting changed data to an already closed interaction returns HTTP 409. Provider acceptance uses the configured LLM and may consume quota:

```powershell
python -m ppt_master_service.conformance.acceptance_runner --workflow document_to_ppt
$env:PPT_MASTER_LLM_TIMEOUT_SECONDS='180'
python -m ppt_master_service.conformance.acceptance_runner `
  --workflow document_to_ppt `
  --provider `
  --output-root .scratch/workflow-acceptance `
  --evidence-path workflows/acceptance/document_to_ppt.json
```

The timeout override applies only to the current PowerShell process and does not edit `.env`. A provider or account failure leaves the previous canonical evidence unchanged; never repair evidence by editing checksums manually. The workflow is registered as `complete`; its checksum-bound provider evidence is linked from `workflows/acceptance/document_to_ppt.json`.

## Submit A Template-Fill PPTX Job

Use this when the user uploads an existing PowerPoint template/deck and wants to keep the original layout while replacing the content. This workflow does not use SVG generation.

Required:

- `template_pptx`: absolute or repo-relative path to an existing `.pptx`/PowerPoint file.
- At least one content input: `source_files`, `source_urls`, `topic`, `prompt`, or `extra_requirements`.

Example:

```bash
curl -X POST http://127.0.0.1:8510/api/ppt/generate ^
  -H "Content-Type: application/json" ^
  -d "{\"mode\":\"auto\",\"template_pptx\":\"E:/path/company_template.pptx\",\"source_files\":[\"E:/path/content.md\"],\"pages\":5,\"language\":\"zh-CN\",\"extra_requirements\":\"保留原版式，替换为新内容\"}"
```

Routing:

```text
template_pptx present -> template_fill_pptx
"保留原版式" / "填充模板" / "使用公司模板" intent -> template_fill_pptx
```

Workspace layout:

```text
ppt_master_service/jobs/<job_id>/workspace/sources/
ppt_master_service/jobs/<job_id>/workspace/analysis/slide_library.json
ppt_master_service/jobs/<job_id>/workspace/analysis/fill_plan_scaffold.json
ppt_master_service/jobs/<job_id>/workspace/analysis/fill_plan.json
ppt_master_service/jobs/<job_id>/workspace/analysis/check_report.json
ppt_master_service/jobs/<job_id>/workspace/exports/*.pptx
ppt_master_service/jobs/<job_id>/workspace/validation/*.md
```

True command chain:

```bash
python skills/ppt-master/scripts/template_fill_pptx.py analyze "<workspace>/sources/<template.pptx>" -o "<workspace>/analysis/slide_library.json"
python skills/ppt-master/scripts/template_fill_pptx.py scaffold "<workspace>/analysis/slide_library.json" -o "<workspace>/analysis/fill_plan_scaffold.json" --slides "1-5"
# LLM writes <workspace>/analysis/fill_plan.json from source markdown + slide library.
python skills/ppt-master/scripts/template_fill_pptx.py check-plan "<workspace>/analysis/slide_library.json" "<workspace>/analysis/fill_plan.json" -o "<workspace>/analysis/check_report.json"
python skills/ppt-master/scripts/template_fill_pptx.py apply "<workspace>/sources/<template.pptx>" "<workspace>/analysis/fill_plan.json" -o "<workspace>/exports/filled_template.pptx"
python skills/ppt-master/scripts/source_to_md/ppt_to_md.py "<workspace>/exports/<filled>.pptx" -o "<workspace>/validation/<filled>.md"
```

Current limitations:

- `fill_plan.json` authoring uses the configured LLM. Without `OPENAI_API_KEY`, this workflow fails clearly at the planning stage.
- Image replacement is not supported by the upstream v1 script.
- Object-level animation editing is not supported; existing object animations are preserved where the source deck supports them.
- Visual overflow detection is limited to `check-plan` capacity heuristics plus PPTX read-back validation.

The planner must now match `pages` exactly. Source slides may be repeated, and any capacity `WARN` or `ERROR` triggers up to two repair attempts before the workflow fails instead of applying an overfilled plan.

## Submit A Template-Guided SVG Job

Use this when the source PPTX supplies the visual system, but generated content pages must not be constrained by its original text boxes. The workflow imports the PPTX into layered and flat SVG references, locks its canvas/theme/fonts, maps the first and last pages to literal cover/ending references, and freely composes intermediate pages inside the content reference's safe area.

This workflow always sets `svg_distribution_policy=locked`: existing template SVG elements keep their original coordinates, dimensions, relative spacing, and layer order. Audience-facing text may be replaced without changing its element geometry, and new content may only be composed inside the declared content area.

```bash
curl -X POST http://127.0.0.1:8510/api/ppt/generate ^
  -H "Content-Type: application/json" ^
  -d "{\"mode\":\"auto\",\"template_strategy\":\"guided_svg\",\"template_pptx\":\"E:/path/company_template.pptx\",\"source_files\":[\"E:/path/content.md\"],\"pages\":8,\"language\":\"zh-CN\"}"
```

Routing:

```text
template_pptx + template_strategy=guided_svg -> template_guided_svg
template_pptx + "自由设计/自由排版/不受原文本框限制" -> template_guided_svg
template_pptx without guided intent -> template_fill_pptx
```

Project artifacts:

```text
<project>/templates/source_template.pptx
<project>/templates/source_import/manifest.json
<project>/templates/source_import/svg/
<project>/templates/source_import/svg-flat/
<project>/templates/template_context.json
<project>/templates/template_bindings.json
<project>/design_spec.md
<project>/spec_lock.md
<project>/svg_output/*.svg
<project>/notes/total.md
<project>/quality_report.json
<project>/exports/*.pptx
```

The workflow reuses the same pause/resume interaction, persistent Strategist and Executor sessions, and live preview contract as `document_to_ppt`. `PhaseRunnerRouter` selects `TemplateGuidedPhaseRunner` only after a validated project-local template context exists. The confirmed strategy receives deterministic `cover`, `content`, and `ending` page bindings; each distinct imported reference is read before the first page is written.

Template conformance is checked together with generic SVG quality. Literal cover and ending pages preserve every non-text template atom. Adapted content pages preserve the full-canvas background and chrome outside the editable content area. Retained template sample text, a wrong canvas, or missing chrome returns a structured page/role/location/reason/correction finding to the same persistent Agent session.

If the preview process exits while a job is paused, resume the job so `PreviewRuntime` can start a healthy replacement before Executor continues. For manual recovery, run `python skills/ppt-master/scripts/svg_editor/server.py "<project>" --port 5050 --live --timeout 0`, then verify `<preview_url>/api/config` before resuming.

Offline and real-provider acceptance:

```powershell
python -m ppt_master_service.conformance.acceptance_case_runner --workflow template_guided_svg
$env:PPT_MASTER_LLM_TIMEOUT_SECONDS='180'
python -m ppt_master_service.conformance.acceptance_case_runner `
  --workflow template_guided_svg `
  --provider `
  --output-root .scratch/workflow-acceptance `
  --evidence-path workflows/acceptance/template_guided_svg.json
```

The provider command may consume configured quota. The timeout override is process-local, and canonical evidence is replaced only after a successful run. The registry entry is `complete`. Canonical evidence binds the fixture, implementation files, interaction receipt, imported template context and bindings, generated SVG pages, notes, quality report, and three-page PPTX with SHA-256 checksums.

## Query Status

```bash
curl http://127.0.0.1:8510/api/ppt/status/<job_id>
```

Status response fields:

```json
{
  "job_id": "abc",
  "workflow": "document_to_ppt",
  "status": "generating_svg",
  "progress": 30,
  "current_step": "run",
  "created_at": "2026-06-05T00:00:00Z",
  "updated_at": "2026-06-05T00:00:10Z",
  "project_path": "...",
  "output_file": null,
  "error": null,
  "review": null,
  "interaction": null,
  "preview": null,
  "warnings": [],
  "last_event": {"message": "workflow step start: run"},
  "logs": []
}
```

Statuses:

- `queued`
- `running`
- `generating_strategy`
- `generating_svg`
- `preparing_brand`
- `planning_images`
- `loading_mirror_template`
- `revising_pptx`
- `analyzing_template`
- `planning_fill`
- `checking_fill_plan`
- `applying_template`
- `validating_output`
- `checking_quality`
- `exporting`
- `success`
- `failed`
- `waiting_user`
- `waiting_for_llm_svg_generation`
- `waiting_for_image_generation`

The response includes `workflow`, `progress`, `current_step`, `project_path`, `output_file`, `error`, `review`, the pending `interaction`, the live `preview`, `warnings`, and recent logs.

## Job Artifacts

Each job keeps an isolated record under:

```text
ppt_master_service/jobs/<job_id>/
```

Stable files:

```text
request.json        Original request payload
workflow.json       Routed workflow and workspace/project pointers
status.json         Current status/progress/current_step/error summary
events.jsonl        Per-job append-only event log
logs.jsonl          Compatibility alias for event records
error.log           Failure reason and traceback, when failed
design_spec.md      Mirrored from the project when available
spec_lock.md        Mirrored from the project when available
review.json         Review result, when review completes
output_file         Text file containing the final output path, when available
workspace/          Per-job working directory; never deleted on failure
```

Failures preserve all intermediate files. To inspect the last step, open `events.jsonl` or read `last_event` from `/api/ppt/status/{job_id}`.

## Download

```bash
curl -L -o result.pptx http://127.0.0.1:8510/api/ppt/download/<job_id>
```

If the job has an existing output file, the endpoint returns `FileResponse`. Otherwise it returns JSON with the current job status and file path.

## Templates

```bash
curl http://127.0.0.1:8510/api/templates
```

The endpoint reads `ppt_master_service/template_metadata/*.json`. If no usable metadata exists, it scans existing PPT Master template directories and writes editable `auto_*.json` files.

## Real Scripts Connected

Deterministic tool wrappers call:

- `project_manager.py`
- `source_to_md/pdf_to_md.py`
- `source_to_md/doc_to_md.py`
- `source_to_md/excel_to_md.py`
- `source_to_md/ppt_to_md.py`
- `source_to_md/web_to_md.py`
- `svg_quality_checker.py`
- `total_md_split.py`
- `finalize_svg.py`
- `svg_to_pptx.py`
- `pptx_template_import.py`
- `template_fill_pptx.py` subcommands

The export sequence is always logged as separate steps:

1. `total_md_split.py`
2. `finalize_svg.py`
3. `svg_to_pptx.py`

## Workflow Status

Implemented:

- `topic_research`: LLM creates `sources/research.md`, then reuses document generation.
- `document_to_ppt`: persistent Strategist drafts the Eight Confirmations, the workflow pauses, and a confirmed digest receipt resumes into canonical `design_spec.md`/`spec_lock.md`, live preview, one-action-per-turn Executor generation, structured repair, deterministic review, and exact-page export.
- `template_fill_pptx`: analyzes a native PPTX template, scaffolds a fill plan, asks LLM to author `fill_plan.json`, runs `check-plan`, applies direct OOXML replacements, then validates read-back Markdown.
- `template_guided_svg`: imports a real PPTX, persists deterministic role bindings, pauses for the Eight Confirmations, starts live preview, preloads imported SVG references, and generates arbitrary-page SVG decks under generic plus template-specific quality gates.
- `revision`: uses an existing `reference_pptx` as a native slide library, extracts its Markdown, generates `revision_plan.md` plus a template-fill `fill_plan.json`, then applies direct OOXML text/table/chart edits. Best for polish, translation, shortening, and copy replacement while keeping layouts.
- `brand_style`: collects `brand_assets`, writes `sources/brand_guide.md` and a preliminary `spec_lock.md`, then reuses the `document_to_ppt` Strategist/Executor/review/export pipeline.
- `image_heavy`: writes `sources/image_plan.md`, copies supplied image files from `source_files` into `images/user/`, optionally runs `analyze_images.py`, then reuses the `document_to_ppt` pipeline.

The workflow registry, rather than this summary list, is authoritative for maturity and linked acceptance evidence.

## Workflow Matrix

| Workflow | Suitable orders | Main input fields | Output | Current limits |
|---|---|---|---|---|
| `topic_research` | Topic-only deck generation | `topic`, `prompt`, `pages`, `language`, `style` | PPTX from SVG pipeline | No external fact-checking beyond LLM-generated research notes |
| `document_to_ppt` | Source docs/URLs to new PPT | `source_files`, `source_urls`, `prompt`, `pages` | PPTX from SVG pipeline | Depends on LLM SVG quality |
| `template_fill_pptx` | Fill existing company/template PPTX while preserving layout | `template_pptx` plus source material | Native filled PPTX | No image replacement; visual overflow check is heuristic |
| `template_guided_svg` | Reuse a PPTX visual system while freely composing new pages | `template_pptx`, `template_strategy=guided_svg`, content, `pages` | PPTX from SVG pipeline | Complex SmartArt/OLE/animations are references rather than guaranteed round-trip objects |
| `revision` | Beautify, polish, translate, shorten, or revise an existing PPTX | `reference_pptx` or PPTX in `source_files`, revision prompt | Native revised PPTX | Supports direct text/table/chart replacement path; deep redesign and image replacement are TODO |
| `brand_style` | New deck under brand/logo/VI constraints | `brand_assets`, `style`, `extra_requirements`, content inputs | PPTX from SVG pipeline | Brand asset visual verification is minimal |
| `image_heavy` | Illustration/storybook/poster/product/architecture style decks | image files in `source_files`, `prompt`, `style` | PPTX with verified raster media | Required content-page coverage depends on a configured image provider or supplied readable images |
| `mirror_template` | Reuse existing SVG/page layout template | `template_id` or `mirror_template_id` | Mirrored PPTX | Only approved editable text slots may change; locked chrome and structural fingerprints must remain stable |

## Workflow Conformance

The portable workflow catalog is `workflows/registry.json`; the approved contract
and acceptance design is
`docs/superpowers/specs/2026-07-13-workflow-conformance-design.md`; reusable Python
models, loading, persistence, and audits live in `ppt_master_service/conformance/`.

The current service is one caller of this contract, not the permanent Agent Core.
Its order/job status remains an order-service compatibility state, while registry
maturity describes workflow implementation and acceptance evidence. The two must
not be treated as equivalent. `FileRunStore` likewise provides portable workflow
checkpoint files without replacing the Order System as order-state authority.

The RunStore layout is:

```text
<run-root>/<run-id>/run.json
<run-root>/<run-id>/events.jsonl
<run-root>/<run-id>/artifacts.json
```

Auto routing rules:

- `template_pptx` plus `template_strategy=guided_svg` or free-design intent -> `template_guided_svg`.
- Other `template_pptx` requests -> `template_fill_pptx`.
- `reference_pptx` plus modify/beautify/translate/compress/polish intent -> `revision`.
- `brand_assets` or brand/logo/VI/company style wording -> `brand_style`.
- illustration/storybook/poster/lots of images/visual impact/product image wording -> `image_heavy`.
- `mirror_template_id` or mirror-template wording -> `mirror_template`.
- `source_files`/`source_urls` -> `document_to_ppt`.
- only `topic`/`prompt` -> `topic_research`.

## Quality Limits

- The LLM-generated SVG path is an MVP. It may fail `svg_quality_checker.py` or produce weak visual design.
- Executor validates each generated page before advancing. Transport failures retry the same request; protocol failures receive the exact missing/invalid output requirement; SVG failures enter the structured repair pipeline.
- Strategist protocol feedback reports the exact field and observed JSON type. In particular, an array-shaped `confirmations` value is rejected with the required eight-key object shape and a valid `submit_strategy_draft` correction example.
- No external fact-checking is performed by the service. Topic research includes `sources_to_verify` notes.
- No full visual review loop is implemented yet.
- No payment, frontend, users, or database are included.

## Structured SVG Repair

SVG repair is a constrained pipeline rather than a request to rewrite the complete page:

```text
page candidate
  -> deterministic compatibility repair
  -> svg_quality_checker.py --json-output
  -> structured QualityFinding with element location
  -> Repair Recipe Registry
  -> constrained patch operations
  -> isolated candidate check
  -> promote or reject with the rejection supplied to the next attempt
```

The first registry covers malformed XML, invalid viewBox, image/group opacity, RGBA colors, masks/filters, invalid asset paths, unknown icons, text overflow, out-of-bounds elements, overlaps, and `spec_lock` color drift.

Known syntax-preserving conversions are performed by `skills/ppt-master/scripts/svg_repair.py`. The LLM is called only when the selected recipe requires semantic judgment. It receives the exact file, element ID/index, attributes, approved operations, forbidden alternatives, and the previous candidate rejection. It returns patch operations, not a complete SVG.

Repair records are stored with the project:

```text
<project>/repair/findings/quality_latest.json
<project>/repair/candidates/<slide>.attempt_<n>.svg
<project>/repair/attempts/<slide>.attempt_<n>.json
<project>/repair/outcomes/<slide>.json
<project>/repair/<slide>.original.svg
```

## Debug A Job

Inspect:

```text
ppt_master_service/jobs/<job_id>/job.json
ppt_master_service/jobs/<job_id>/logs.jsonl
ppt_master_service/logs/<job_id>.jsonl
ppt_master_service/jobs/<job_id>/workspace/request.json
```

Then inspect the generated PPT Master project path shown in `job.json`:

```text
<project>/request.json
<project>/sources/
<project>/design_spec.md
<project>/spec_lock.md
<project>/.service_strategy.json
<project>/svg_output/
<project>/notes/total.md
<project>/exports/
```

Common failure causes:

- `OPENAI_API_KEY is not set`
- LLM returned invalid JSON
- LLM template fill plan referenced a missing `slot_id`, `table_id`, `chart_id`, or `source_slide`
- `template_fill_pptx.py check-plan` reported capacity or target errors
- `template_fill_pptx.py apply` could not clone or edit the template deck
- `revision` requested unsupported deep layout/image/animation changes
- `mirror_template` reached the explicit TODO boundary
- LLM returned SVG without `viewBox`
- `svg_quality_checker.py` failed
- `total_md_split.py` could not match notes headings to SVG stems
- `svg_to_pptx.py` rejected unsupported SVG content

## Test Client

With the service running:

```bash
python ppt_master_service/test_client.py
```

Focused order tests:

```bash
python ppt_master_service/test_router.py
python ppt_master_service/test_topic_order.py
python ppt_master_service/test_document_order.py
python ppt_master_service/test_template_fill_order.py
```

To test deterministic export using an existing project:

```bash
set PPT_MASTER_TEST_PROJECT=E:/OfficeAgent/ppt-master/examples/ppt169_building_effective_agents
python ppt_master_service/test_client.py
```

## Agent Builder HTTP Tool

External systems only need to call `POST /api/ppt/generate` to create an order, then poll `GET /api/ppt/status/{job_id}` and download from `GET /api/ppt/download/{job_id}` when `status=success`.

Recommended HTTP tool:

```text
tool name: generate_ppt
method: POST
url: http://<service-host>:8510/api/ppt/generate
description: Create a long-running PPT generation job. Returns a job_id that the caller can poll for status and download when complete.
```

Input schema:

```json
{
  "type": "object",
  "properties": {
    "mode": {
      "type": "string",
      "enum": ["auto", "topic_research", "document_to_ppt", "template_fill_pptx", "template_guided_svg", "revision", "brand_style", "image_heavy", "mirror_template"],
      "default": "auto"
    },
    "topic": {"type": "string"},
    "prompt": {"type": "string"},
    "pages": {"type": "integer", "minimum": 1},
    "language": {"type": "string"},
    "style": {"type": "string"},
    "canvas_format": {"type": "string", "default": "ppt169"},
    "source_files": {"type": "array", "items": {"type": "string"}},
    "source_urls": {"type": "array", "items": {"type": "string"}},
    "template_pptx": {"type": "string"},
    "template_strategy": {"type": "string", "enum": ["fill", "guided_svg"], "default": "fill"},
    "template_id": {"type": "string"},
    "mirror_template_id": {"type": "string"},
    "reference_pptx": {"type": "string"},
    "brand_assets": {"type": "array", "items": {"type": "string"}},
    "output_type": {"type": "string", "default": "pptx"},
    "quality_level": {"type": "string", "default": "standard"},
    "extra_requirements": {"type": "string"}
  }
}
```

Output schema:

```json
{
  "type": "object",
  "properties": {
    "job_id": {"type": "string"},
    "workflow": {"type": "string"},
    "status": {"type": "string"},
    "message": {"type": "string"}
  },
  "required": ["job_id", "workflow", "status", "message"]
}
```

Example request:

```json
{
  "mode": "auto",
  "topic": "Local Agent PPT service architecture",
  "pages": 5,
  "language": "en",
  "style": "technical executive briefing"
}
```

Example response:

```json
{
  "job_id": "6d7c2b2f2f3e41c1a6f7b5c9e1a0d123",
  "workflow": "topic_research",
  "status": "queued",
  "message": "job queued"
}
```

For Agent Builder or Agents SDK, configure a second read-only HTTP tool for status polling:

```text
tool name: get_ppt_job_status
method: GET
url: http://<service-host>:8510/api/ppt/status/{job_id}
description: Check progress, current_step, output_file, review, warnings, and error for a PPT generation job.
```

## Security

- API keys are read only from environment variables and are never returned by API responses.
- `source_files`, `template_pptx`, `reference_pptx`, `brand_assets`, and `project_path` must be under allowed input roots.
- Default allowed roots are the repo root, `ppt_master_service/jobs/`, `ppt_master_service/outputs/`, and `.scratch/`.
- Add production upload roots with `PPT_MASTER_ALLOWED_INPUT_DIRS`, separated by the OS path separator.
- The service writes generated files only inside job workspaces or allowed ppt-master project directories.
- Set `PPT_MASTER_LOCAL_DEV_PATHS=0` in production to avoid returning sensitive local filesystem paths in status responses.
- Production deployments should upload generated PPTX files to object storage and return signed URLs instead of local paths.

## Performance Notes

- Workflow routing rules, prompt files, and deterministic script wrappers are already fixed in `ppt_master_service/`; callers do not need Codex to reinterpret the repository for every order.
- `strategist_prompt.md`, `executor_prompt.md`, `topic_research_prompt.md`, and `template_fill_prompt.md` are stable prompt assets and can be cached by a long-running worker.
- Template metadata is cached in `ppt_master_service/template_metadata/*.json`; call `/api/templates` once at startup or refresh only after template changes.
- Avoid scanning the whole repo per request. Work inside `ppt_master_service/jobs/<job_id>/workspace/` and the selected ppt-master project.
- Existing job workspaces are preserved for retry/debug. A future worker can resume from `status.json`, `events.jsonl`, and intermediate files instead of restarting discovery.
- For repeated template-fill jobs, reuse cached `slide_library.json` by template checksum in a future cache layer; the current MVP keeps it per job for isolation.

## Shared Provider Configuration

The service and production wrapper use the same provider resolver. Configuration
precedence is process environment, `PPT_MASTER_ENV_FILE`, then the nearest `.env`
found by walking upward from the repository. Qwen variables fill missing
OpenAI-compatible variables; process values are never overwritten. Logs and
acceptance evidence contain only provider name, model, and base URL host, never
API keys.

## Completed Batch 2 Workflows

`mirror_template` and `image_heavy` are registered as `complete`. Their canonical
fixtures and provider evidence are linked from `workflows/registry.json`.

`mirror_template` accepts a deck `template_id`, creates a stable editable roster,
locks repeated/footer/legal text, asks the LLM for strict `lines` replacements,
patches text only, verifies SVG structural fingerprints, and exports PPTX.

`image_heavy` requires a stable `asset_id` for every generated image, enforces
raster coverage on content pages, verifies the generated files with Pillow,
supports resume from incomplete manifests, and uses
`waiting_for_image_generation` when image generation must be retried.

Run provider acceptance from the repository root:

```powershell
python -m ppt_master_service.conformance.acceptance_runner --workflow mirror_template --provider --output-root .scratch/workflow-acceptance --evidence-path workflows/acceptance/mirror_template.json
python -m ppt_master_service.conformance.acceptance_runner --workflow image_heavy --provider --output-root .scratch/workflow-acceptance --evidence-path workflows/acceptance/image_heavy.json
```
