from __future__ import annotations

import hashlib
import json
import re
import sys
import uuid
from pathlib import Path
from typing import Any, Callable, Sequence

from .llm_client import FailureKind, LLMError, call_llm_json, classify_llm_failure
from .conformance import InteractionRequest
from .interaction_coordinator import validate_confirmations
from .tools.template_tools import scan_templates
from .tools.template_guidance import load_reference_svg, resolve_page_reference
from .tools import quality_tools
from . import visual_assets

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "skills" / "ppt-master" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
from svg_finalize.embed_icons import build_prompt_icon_manifest


CANVAS_DIMENSIONS = {
    "ppt169": (1280, 720),
    "ppt43": (1024, 768),
    "xiaohongshu": (1080, 1440),
    "xhs": (1080, 1440),
    "story": (1080, 1920),
    "moments": (1080, 1080),
    "a4": (1240, 1754),
}

DIRECT_PAGE_JSON_COMPATIBILITY = """

## Direct page JSON compatibility mode

This invocation is the legacy one-page executor, not the persistent action loop.
The caller has already supplied the confirmed spec, source context, page order,
and output filename, and it cannot execute action envelopes. For this invocation,
do not return `action` or `arguments` and do not request `read_spec_lock`.

Return exactly one JSON object with these top-level keys:

```json
{"svg": "<svg ...>...</svg>", "notes": "speaker notes", "summary": "page purpose"}
```

All page and SVG rules above still apply. The `svg` value must contain one
complete SVG document; `notes` and `summary` must be strings.
"""


def _build_icon_contract_prompt() -> str:
    manifest = build_prompt_icon_manifest()
    lines = [
        "Icon Contract:",
        "- Use icons only with <use data-icon=\"library/name\" x=\"...\" y=\"...\" width=\"...\" height=\"...\" fill=\"...\"/>.",
        "- Copy data-icon values exactly from the allowlist below.",
        "- Do not invent icon names or translate business concepts into unlisted names.",
        "- If no listed icon fits, omit the icon or use a generic listed fallback such as building, buildings, chart-line, package, truck, users, globe, target, or lightbulb.",
        "- Do not use raw <use href=\"...\"> or <symbol>; only data-icon placeholders are supported.",
        "Allowed data-icon values:",
    ]
    if not manifest:
        lines.append("- (none; omit icons)")
        return "\n".join(lines)

    for lib, names in manifest.items():
        full_names = [f"{lib}/{name}" for name in names]
        lines.append(f"- {lib}: {', '.join(full_names)}")
    return "\n".join(lines)


def run_topic_research(
    project_path: Path,
    request: dict[str, Any],
    *,
    call_json: Callable[[str, str | None], dict[str, Any]] | None = None,
) -> str:
    prompt_path = Path(__file__).resolve().parent / "prompts" / "topic_research_prompt.md"
    system_prompt = prompt_path.read_text(encoding="utf-8")
    prompt = f"""
Create structured research notes for a presentation.

Topic: {request.get('topic') or ''}
Prompt: {request.get('prompt') or ''}
Language: {request.get('language') or 'match user request'}
Target pages: {request.get('pages') or 6}
Extra requirements: {request.get('extra_requirements') or ''}

Return JSON with:
{{
  "title": "...",
  "sections": [
    {{"heading": "...", "bullets": ["...", "..."], "evidence": ["..."]}}
  ],
  "sources_to_verify": ["..."]
}}
"""
    data = (call_json or call_llm_json)(prompt, system_prompt)
    sources_dir = project_path / "sources"
    sources_dir.mkdir(parents=True, exist_ok=True)
    research_path = sources_dir / "research.md"
    research_path.write_text(_render_research_markdown(data), encoding="utf-8")
    return str(research_path)


def generate_strategy_draft(
    project_path: Path,
    request: dict[str, Any],
    *,
    validator: Callable[[dict[str, Any]], Sequence[dict[str, Any]]] | None = None,
    max_protocol_attempts: int = 1,
    call_json: Callable[[str, str | None], dict[str, Any]] | None = None,
    revision_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a normalized strategy without writing canonical project artifacts."""
    prompt_path = Path(__file__).resolve().parent / "prompts" / "strategist_prompt.md"
    system_prompt = prompt_path.read_text(encoding="utf-8")
    source_markdown = _load_source_markdown(project_path)
    template_metadata = scan_templates(write_metadata=False)[:20]
    pages = int(request.get("pages") or 6)
    canvas_format = request.get("canvas_format") or "ppt169"
    width, height = CANVAS_DIMENSIONS.get(canvas_format, CANVAS_DIMENSIONS["ppt169"])
    project_template_context = _load_project_template_context(project_path)
    template_guidance = (
        json.dumps(project_template_context, ensure_ascii=False, indent=2)
        if project_template_context
        else "(none)"
    )
    prompt = f"""
Create a PPT Master strategy for a deck.

Request JSON:
{json.dumps(request, ensure_ascii=False, indent=2)}

Canvas: {canvas_format}, {width}x{height}
Target pages: {pages}
Language: {request.get('language') or 'match source/request'}
Style: {request.get('style') or 'content-appropriate'}
Extra requirements: {request.get('extra_requirements') or ''}

Available template metadata sample:
{json.dumps(template_metadata, ensure_ascii=False)[:6000]}

Project template guidance:
{template_guidance}

When project template guidance is present:
- Treat its canvas, colors, fonts, and role mappings as authoritative.
- When svg_distribution_policy is locked, keep every template SVG element at its original coordinates, size, spacing, and layer order.
- Do not move, resize, delete, reorder, or redistribute template SVG elements.
- Use cover for the first page, ending for the last page, and content for intermediate pages.
- A content role with layout_policy free-content-area preserves the reference's visual DNA and chrome while allowing a new composition inside content_area.
- Do not plan around the source slide's old text boxes or sample copy.

{visual_assets.build_visual_asset_prompt(project_path)}

{_build_icon_contract_prompt()}

Source markdown:
{source_markdown[:30000]}

Return JSON with exactly these top-level keys:
- project_info
- canvas
- visual_theme
- typography
- layout
- icon
- visualization
- image
- outline: list of {{"index": 1, "title": "...", "purpose": "...", "key_points": ["..."], "speaker_note": "..."}}
- speaker_notes
- tech_constraints
- spec_lock

If generated raster images are useful for this task, include them under image.image_prompts.items.
If icons are useful, define icon.library, icon.usage_rules, and slide-level icon_slots where helpful.
"""
    if revision_context is not None:
        prompt += (
            "\nRevision request:\n"
            + json.dumps(
                revision_context,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
    data: dict[str, Any] | None = None
    repair_instruction = ""
    caller = call_json or call_llm_json
    for attempt in range(max_protocol_attempts + 1):
        raw_data = caller(prompt + repair_instruction, system_prompt)
        outline = raw_data.get("outline") if isinstance(raw_data, dict) else None
        actual_pages = len(outline) if isinstance(outline, list) else 0
        issues: list[dict[str, Any]] = []
        if actual_pages != pages:
            issues.append(
                {
                    "code": "strategy.outline.page_count",
                    "observed": actual_pages,
                    "required": f"exactly {pages} substantive outline entries",
                }
            )
        if isinstance(raw_data, dict):
            candidate = _normalize_strategy(
                raw_data,
                request,
                pages,
                canvas_format,
                width,
                height,
                project_path,
            )
            if validator is not None:
                issues.extend(dict(item) for item in validator(candidate))
        else:
            candidate = {}
            issues.append(
                {
                    "code": "strategy.response.object",
                    "observed": type(raw_data).__name__,
                    "required": "a JSON object",
                }
            )
        if not issues:
            data = candidate
            break
        if attempt < max_protocol_attempts:
            repair_instruction = _build_strategy_repair_instruction(issues, pages)
            continue
        raise LLMError("strategist response remained invalid after correction attempts: " + json.dumps(issues, ensure_ascii=False))
    assert data is not None
    return data


def revise_strategy_draft(
    project_path: Path,
    request: dict[str, Any],
    previous: dict[str, Any],
    feedback: str,
    *,
    call_json: Callable[[str, str | None], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return generate_strategy_draft(
        project_path,
        request,
        call_json=call_json,
        revision_context={
            "previous_strategy": previous,
            "user_feedback": feedback,
        },
    )


def strategy_digest(strategy: dict[str, Any]) -> str:
    raw = json.dumps(
        strategy,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_design_interaction(
    strategy: dict[str, Any],
    revision: int,
) -> InteractionRequest:
    confirmations = strategy.get("confirmations")
    if not isinstance(confirmations, dict):
        raise LLMError("strategy is missing the Eight Confirmations object")
    validate_confirmations("strategy_draft", confirmations)
    return InteractionRequest(
        interaction_id="int_" + uuid.uuid4().hex,
        type="confirm_design",
        revision=revision,
        status="pending",
        strategy_digest=strategy_digest(strategy),
        title="确认 PPT 设计方案",
        message="请确认或修改以下八项设计建议。",
        confirmations=confirmations,
        split_mode_note=dict(
            strategy.get("split_mode_note")
            or {
                "recommendation": "continuous",
                "message": "💡 当前规模适中，默认连续完成生成。",
            }
        ),
        actions=["confirm", "revise"],
    )


def apply_confirmations(
    strategy: dict[str, Any],
    confirmations: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    validate_confirmations("confirmed_strategy", confirmations)
    confirmed = json.loads(json.dumps(strategy, ensure_ascii=False))
    confirmed["confirmations"] = confirmations
    confirmed["canvas"]["format"] = confirmations["canvas"]["format"]
    confirmed["project_info"]["target_audience"] = confirmations[
        "target_audience"
    ]["description"]
    confirmed["visual_theme"]["style_objective"] = confirmations[
        "style_objective"
    ]["description"]
    confirmed["visual_theme"]["colors"] = confirmations["color_scheme"]
    confirmed["icon"] = confirmations["icon_usage"]
    confirmed["typography"].update(confirmations["typography"])
    confirmed["image"].update(confirmations["image_usage"])
    confirmed["spec_lock"]["canvas"] = confirmed["canvas"]
    confirmed["spec_lock"]["colors"] = confirmations["color_scheme"]
    confirmed["spec_lock"]["typography"] = confirmed["typography"]
    confirmed["spec_lock"]["icons"] = confirmations["icon_usage"]
    confirmed["spec_lock"]["images"] = confirmed["image"]
    return confirmed


def normalize_strategy_action(
    raw: dict[str, Any],
    project_path: Path,
    request: dict[str, Any],
) -> dict[str, Any]:
    pages = int(request.get("pages") or 6)
    canvas_format = str(request.get("canvas_format") or "ppt169")
    width, height = CANVAS_DIMENSIONS.get(
        canvas_format,
        CANVAS_DIMENSIONS["ppt169"],
    )
    outline = raw.get("outline")
    if not isinstance(outline, list) or len(outline) != pages:
        observed = len(outline) if isinstance(outline, list) else 0
        raise LLMError(
            f"strategy action has {observed} outline entries; required exactly {pages}"
        )
    confirmations = raw.get("confirmations")
    if confirmations is None:
        raise LLMError("strategy action is missing confirmations")
    if not isinstance(confirmations, dict):
        raise LLMError(
            "strategy action confirmations received "
            f"{type(confirmations).__name__}; required a single JSON object "
            "with exactly the eight named keys canvas, page_count, "
            "target_audience, style_objective, color_scheme, icon_usage, "
            "typography, and image_usage. Do not return an array; convert "
            "the single-key items into those keys on one object"
        )
    normalized = _normalize_strategy(
        json.loads(json.dumps(raw, ensure_ascii=False)),
        request,
        pages,
        canvas_format,
        width,
        height,
        project_path,
    )
    validate_confirmations("strategy_action", normalized["confirmations"])
    return normalized


def run_strategist(
    project_path: Path,
    request: dict[str, Any],
    **kwargs: Any,
) -> dict[str, Any]:
    data = generate_strategy_draft(project_path, request, **kwargs)
    for key in (
        "project_info",
        "visual_theme",
        "typography",
        "layout",
        "icon",
        "visualization",
        "image",
        "speaker_notes",
        "tech_constraints",
    ):
        if not isinstance(data.get(key), dict):
            data[key] = {}
    if not isinstance(data.get("confirmations"), dict):
        data["confirmations"] = _compat_confirmations(data, request)
    return materialize_confirmed_strategy(project_path, data)


def _build_strategy_repair_instruction(issues: Sequence[dict[str, Any]], pages: int) -> str:
    page_count_prefix = ""
    page_issue = next(
        (item for item in issues if item.get("code") == "strategy.outline.page_count"),
        None,
    )
    if page_issue is not None:
        page_count_prefix = (
            f"\n\nREPAIR REQUIRED: the previous response returned {page_issue['observed']} outline entries, "
            f"but this order requires exactly {pages}."
        )
    instructions = "\n".join(_strategy_issue_instruction(issue) for issue in issues)
    return (
        page_count_prefix
        + "\n\nCORRECTION REQUIRED. Return the complete strategy JSON again and fix every item.\n"
        + "Exact repair instructions:\n"
        + instructions
        + "\nValidation findings:\n"
        + json.dumps(issues, ensure_ascii=False, indent=2)
    )


def _strategy_issue_instruction(issue: dict[str, Any]) -> str:
    code = str(issue.get("code") or "")
    item = issue.get("item")
    required = str(issue.get("required") or "the required value")
    location = f"At image prompt item {item}" if item is not None else "In the strategy"
    if code == "image.strategy.asset_id":
        return f'- {location}: add "asset_id" with a stable non-empty unique slug, for example "asset_id": "slide_02_hero".'
    if code == "image.strategy.asset_id_duplicate":
        return f'- {location}: replace "asset_id" with a unique stable slug not used by another item.'
    if code == "image.strategy.filename":
        return f'- {location}: add "filename" with a project-local image filename, for example "slide_02_hero.png".'
    if code == "image.strategy.filename_duplicate":
        return f'- {location}: replace "filename" with a unique project-local filename.'
    if code in {"image.strategy.prompt", "image.strategy.purpose", "image.strategy.aspect_ratio"}:
        field = code.rsplit(".", 1)[-1]
        return f'- {location}: add a non-empty "{field}" value; required: {required}.'
    if code in {"image.strategy.target_page", "image.strategy.target_role"}:
        return f'- {location}: set "slide_index" to {required}.'
    if code == "image.strategy.coverage":
        return f'- {location}: add image prompt items for distinct content pages until coverage is {required}.'
    if code == "strategy.outline.page_count":
        return f"- In outline: return exactly {required}; do not add or omit entries."
    return f"- {location}: change {code or 'this value'} to {required}."


def run_executor(
    project_path: Path,
    request: dict[str, Any],
    strategy: dict[str, Any] | None = None,
    log: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    prompt_path = Path(__file__).resolve().parent / "prompts" / "executor_prompt.md"
    system_prompt = (
        prompt_path.read_text(encoding="utf-8")
        + DIRECT_PAGE_JSON_COMPATIBILITY
    )
    if strategy is None:
        strategy_path = project_path / ".service_strategy.json"
        if not strategy_path.exists():
            raise RuntimeError("missing .service_strategy.json; run strategist first")
        strategy = json.loads(strategy_path.read_text(encoding="utf-8"))

    outline = strategy.get("outline") or []
    if not isinstance(outline, list) or not outline:
        raise RuntimeError("strategy outline is empty")

    source_markdown = _load_source_markdown(project_path)
    design_spec_path = project_path / "design_spec.md"
    design_spec = design_spec_path.read_text(encoding="utf-8") if design_spec_path.exists() else ""
    spec_lock_path = project_path / "spec_lock.md"
    spec_lock_text = spec_lock_path.read_text(encoding="utf-8") if spec_lock_path.exists() else ""
    executor_contract = _build_executor_contract(
        project_path=project_path,
        request=request,
        strategy=strategy,
        spec_lock_text=spec_lock_text,
        design_spec=design_spec,
    )
    (project_path / ".executor_contract.json").write_text(
        json.dumps(executor_contract, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    svg_dir = project_path / "svg_output"
    notes_dir = project_path / "notes"
    svg_dir.mkdir(parents=True, exist_ok=True)
    notes_dir.mkdir(parents=True, exist_ok=True)
    prior_summaries: list[str] = []
    note_sections: list[str] = []
    generated: list[str] = []

    for page in outline:
        index = int(page.get("index") or len(generated) + 1)
        basename = f"slide_{index:02d}"
        if log:
            log("executor_page_start", {"page_index": index, "basename": basename})
        page_context = _build_page_context(
            executor_contract=executor_contract,
            page=page,
            page_count=len(outline),
            source_markdown=source_markdown,
            prior_summaries=prior_summaries[-2:],
        )
        if log:
            log(
                "executor_page_context",
                {
                    "page_index": index,
                    "sections": len(page_context.get("relevant_source_sections") or []),
                    "prompt_chars_estimate": len(json.dumps(page_context, ensure_ascii=False)),
                },
            )
        result = _generate_page_with_retry(
            system_prompt=system_prompt,
            request=request,
            page=page,
            page_count=len(outline),
            basename=basename,
            page_context=page_context,
            log=log,
        )
        svg = _clean_svg(str(result.get("svg") or ""))
        if not svg:
            raise LLMError(f"page {index} did not produce SVG")
        svg_path = svg_dir / f"{basename}.svg"
        svg_path.write_text(svg, encoding="utf-8")
        page_review = quality_tools.check_svg_page(str(project_path), str(svg_path))
        if log:
            log(
                "executor_page_quality",
                {
                    "page_index": index,
                    "passed": page_review.passed,
                    "issues": page_review.issues,
                },
            )
        if not page_review.passed:
            raise LLMError(
                f"page {index} failed structured quality repair: "
                + " | ".join(page_review.issues)
            )
        notes = str(result.get("notes") or page.get("speaker_note") or page.get("purpose") or "")
        note_sections.append(f"# {basename}\n\n{notes.strip()}\n")
        summary = str(result.get("summary") or "").strip()
        if not summary:
            summary = f"{basename}: {page.get('title') or ''} - {page.get('purpose') or ''}"
        prior_summaries.append(summary)
        generated.append(str(svg_path))
        if log:
            log("executor_page_complete", {"page_index": index, "svg_path": str(svg_path)})

    (notes_dir / "total.md").write_text("\n---\n\n".join(note_sections), encoding="utf-8")
    return {"svg_files": generated, "notes_total": str(notes_dir / "total.md")}


def _generate_page_with_retry(**kwargs: Any) -> dict[str, Any]:
    last_error: Exception | None = None
    log = kwargs.get("log")
    retry_feedback: dict[str, str] | None = None
    for attempt in range(2):
        try:
            return _generate_page(attempt=attempt, retry_feedback=retry_feedback, **kwargs)
        except Exception as exc:
            last_error = exc
            failure_kind = classify_llm_failure(exc)
            retry_feedback = None if failure_kind == FailureKind.TRANSPORT else {
                "kind": failure_kind.value,
                "error": str(exc),
            }
            if log:
                page = kwargs.get("page") or {}
                log(
                    "executor_page_error",
                    {
                        "attempt": attempt + 1,
                        "page_index": page.get("index"),
                        "error": str(exc),
                    },
                )
    raise RuntimeError(f"page generation failed after retry: {last_error}") from last_error


def _generate_page(
    *,
    attempt: int,
    system_prompt: str,
    request: dict[str, Any],
    page: dict[str, Any],
    page_count: int,
    basename: str,
    page_context: dict[str, Any],
    log: Callable[[str, dict[str, Any]], None] | None = None,
    retry_feedback: dict[str, str] | None = None,
) -> dict[str, Any]:
    page_context_json = json.dumps(page_context, ensure_ascii=False, indent=2)
    icon_contract = _build_icon_contract_prompt()
    repair_instruction = ""
    if retry_feedback:
        heading = "PROTOCOL REPAIR" if retry_feedback.get("kind") == "protocol" else "ARTIFACT REPAIR"
        repair_instruction = (
            f"\n{heading}:\n"
            f"- Previous attempt failed with: {retry_feedback.get('error', '')}\n"
            "- Correct exactly this failure while preserving the page contract.\n"
            "- Return the complete required JSON object; do not omit svg, notes, or summary.\n"
        )
    prompt = f"""
Generate one PPT Master SVG page. Return JSON with keys: svg, notes, summary.

Attempt: {attempt + 1}
Slide file stem: {basename}
Page count: {page_count}

Request:
{json.dumps(request, ensure_ascii=False, indent=2)}

Structured page context:
{page_context_json}

{icon_contract}

{repair_instruction}

Template reference rules (apply when structured page context includes template_reference):
- Treat template_reference.svg as the visual design source for this page.
- Preserve its canvas, palette, typography, margins, background, brand chrome, header/footer, and decorative DNA.
- Keep every template SVG element at its original coordinates and layer order.
- Do not move, resize, delete, or reorder any template SVG element.
- Audience-facing text content may be replaced in place, but its text element geometry must not change.
- Do not pour new copy into the reference slide's old text boxes or retain its sample text/data.
- For layout_policy free-content-area, preserve fixed visual DNA and freely compose new editable SVG content inside content_area.
- For behavior literal, stay close to the reference composition while replacing all audience-facing sample content.

SVG requirements:
- Return a complete <svg>...</svg> document.
- Use viewBox, width and height from spec_lock/canvas.
- Use only inline SVG attributes, no <style>, no script, no foreignObject, no mask.
- Use top-level <g id="..."> groups for logical content.
- Never set opacity on <g>; apply transparency on visible child elements with fill-opacity/stroke-opacity.
- If this page has image_slots, include the specified project-local images with <image href="...">.
- Image hrefs must be resolvable from svg_output/*.svg. For source images, use paths like "../sources/name_files/image.png"; do not use "name_files/image.png".
- If icons improve this page, use only <use data-icon="library/name" .../> placeholders from the Icon Contract allowlist. The export finalizer embeds them.
- If generated_images are available and relevant to this page, use their svg_href/path values with <image href="...">.
- Keep text readable and inside the canvas.
- Do not include Markdown fences in the svg value.
"""
    if log:
        log(
            "executor_page_prompt_prepared",
            {
                "page_index": page.get("index"),
                "attempt": attempt + 1,
                "prompt_chars": len(prompt),
            },
        )
    result = call_llm_json(prompt, system_prompt)
    if result.get("action") == "write_svg_page" and isinstance(result.get("arguments"), dict):
        result = dict(result["arguments"])
    svg = _clean_svg(str(result.get("svg") or ""))
    if not svg:
        action = result.get("action")
        arguments = result.get("arguments")
        argument_keys = sorted(arguments) if isinstance(arguments, dict) else []
        raise LLMError(
            "missing svg in page JSON: "
            f"received action={action!r}, top_level_keys={sorted(result)}, "
            f"arguments_type={type(arguments).__name__}, argument_keys={argument_keys}; "
            "return top-level svg/notes/summary or a write_svg_page action with object arguments"
        )
    result["svg"] = svg
    result["notes"] = str(result.get("notes") or "")
    result["summary"] = str(result.get("summary") or "")
    return result


def _build_executor_contract(
    *,
    project_path: Path,
    request: dict[str, Any],
    strategy: dict[str, Any],
    spec_lock_text: str,
    design_spec: str,
) -> dict[str, Any]:
    spec_lock = _parse_json_object(spec_lock_text)
    if not spec_lock:
        spec_lock = {}

    canvas = spec_lock.get("canvas") or strategy.get("canvas") or {}
    colors = spec_lock.get("colors") or strategy.get("visual_theme") or {}
    typography = spec_lock.get("typography") or strategy.get("typography") or {}
    outline = spec_lock.get("outline") or strategy.get("outline") or []
    slide_briefs = strategy.get("slide_briefs")
    if not isinstance(slide_briefs, list):
        slide_briefs = outline if isinstance(outline, list) else []

    contract = {
        "request": {
            "mode": request.get("mode"),
            "language": request.get("language"),
            "style": request.get("style"),
            "quality_level": request.get("quality_level"),
            "extra_requirements": request.get("extra_requirements"),
        },
        "canvas": canvas,
        "colors": colors,
        "typography": typography,
        "global_style": {
            "visual_theme": strategy.get("visual_theme"),
            "layout": strategy.get("layout"),
            "icon": strategy.get("icon"),
            "visualization": strategy.get("visualization"),
            "image": strategy.get("image"),
            "tech_constraints": strategy.get("tech_constraints"),
        },
        "visual_assets": strategy.get("visual_assets") or visual_assets.build_visual_asset_context(project_path),
        "slides": _index_slides(slide_briefs),
        "raw_fallback": {},
    }
    template_context = _load_project_template_context(project_path)
    if template_context:
        contract["template_context"] = template_context

    if not contract["slides"] and isinstance(outline, list):
        contract["slides"] = _index_slides(outline)

    if not spec_lock and spec_lock_text.strip():
        contract["raw_fallback"]["spec_lock"] = spec_lock_text
    if not strategy and design_spec.strip():
        contract["raw_fallback"]["design_spec"] = design_spec
    return contract


def _load_project_template_context(project_path: Path) -> dict[str, Any]:
    path = project_path / "templates" / "template_context.json"
    if not path.exists() or not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid project template context: {path}") from exc
    if not isinstance(payload, dict) or payload.get("schema") != "template_guidance.v1":
        raise RuntimeError(f"unsupported project template context: {path}")
    return payload


def _build_page_context(
    *,
    executor_contract: dict[str, Any],
    page: dict[str, Any],
    page_count: int,
    source_markdown: str,
    prior_summaries: list[str],
) -> dict[str, Any]:
    index = int(page.get("index") or 0)
    slides = executor_contract.get("slides") or {}
    page_key = str(index)
    current_slide = slides.get(page_key) or page
    context = {
        "global": {
            "canvas": executor_contract.get("canvas"),
            "colors": executor_contract.get("colors"),
            "typography": executor_contract.get("typography"),
            "style": executor_contract.get("global_style"),
            "visual_assets": executor_contract.get("visual_assets"),
            "raw_fallback": executor_contract.get("raw_fallback"),
        },
        "page": {
            "index": index,
            "page_count": page_count,
            "outline_entry": page,
            "slide_brief": current_slide,
        },
        "prior_slide_summaries": prior_summaries,
        "relevant_source_sections": _select_relevant_source_sections(source_markdown, page),
    }
    template_context = executor_contract.get("template_context")
    if isinstance(template_context, dict):
        reference = resolve_page_reference(template_context, index, page_count)
        reference["svg"] = load_reference_svg(reference)
        context["template_reference"] = reference
    return context


def _parse_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if not stripped:
        return {}
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end <= start:
            return {}
        try:
            parsed = json.loads(stripped[start:end + 1])
        except json.JSONDecodeError:
            return {}
    return parsed if isinstance(parsed, dict) else {}


def _index_slides(slides: list[Any]) -> dict[str, Any]:
    indexed: dict[str, Any] = {}
    for offset, slide in enumerate(slides, start=1):
        if not isinstance(slide, dict):
            continue
        raw_index = slide.get("index") or offset
        try:
            index = int(raw_index)
        except (TypeError, ValueError):
            index = offset
        indexed[str(index)] = slide
    return indexed


def _select_relevant_source_sections(source_markdown: str, page: dict[str, Any], max_sections: int = 8) -> list[dict[str, str]]:
    sections = _split_markdown_sections(source_markdown)
    if not sections:
        return []

    needles = _page_needles(page)
    scored: list[tuple[int, int, dict[str, str]]] = []
    for position, section in enumerate(sections):
        haystack = f"{section.get('heading', '')}\n{section.get('content', '')}".lower()
        score = 0
        for needle in needles:
            if needle and needle.lower() in haystack:
                score += 3 if len(needle) > 2 else 1
        scored.append((score, -position, section))

    selected = [item for score, _pos, item in sorted(scored, reverse=True) if score > 0]
    if not selected:
        selected = sections
    return selected[:max_sections]


def _split_markdown_sections(markdown: str) -> list[dict[str, str]]:
    sections: list[dict[str, str]] = []
    current_heading = "Source"
    current_lines: list[str] = []

    def flush() -> None:
        content = "\n".join(current_lines).strip()
        if content:
            sections.append({"heading": current_heading, "content": content})

    for line in markdown.splitlines():
        if line.startswith("#"):
            flush()
            current_heading = line.lstrip("#").strip() or "Source"
            current_lines = []
        else:
            current_lines.append(line)
    flush()
    return sections


def _page_needles(page: dict[str, Any]) -> list[str]:
    raw_parts: list[str] = []
    for key in ("title", "purpose", "speaker_note"):
        value = page.get(key)
        if value:
            raw_parts.append(str(value))
    points = page.get("key_points")
    if isinstance(points, list):
        raw_parts.extend(str(point) for point in points if point)

    needles: list[str] = []
    for part in raw_parts:
        text = part.strip()
        if text:
            needles.append(text)
        needles.extend(token for token in re.findall(r"[\w\u4e00-\u9fff]{2,}", text) if token)
    return list(dict.fromkeys(needles))


def _load_source_markdown(project_path: Path) -> str:
    chunks: list[str] = []
    for path in sorted((project_path / "sources").glob("*.md")):
        text = path.read_text(encoding="utf-8", errors="replace")
        text = _rewrite_markdown_image_hrefs_for_svg_output(text, path)
        chunks.append(f"\n\n# Source: {path.name}\n\n{text}")
    if not chunks:
        return "(no source markdown found)"
    return "\n".join(chunks)


def _rewrite_markdown_image_hrefs_for_svg_output(text: str, markdown_path: Path) -> str:
    """Rewrite source-local image links to hrefs usable from svg_output/*.svg."""
    project_path = markdown_path.parent.parent
    sources_dir = project_path / "sources"

    def replace(match: re.Match[str]) -> str:
        alt = match.group(1)
        href = match.group(2).strip()
        if _is_external_or_absolute_href(href) or href.startswith("../"):
            return match.group(0)
        source_relative = (markdown_path.parent / href).resolve()
        try:
            project_relative = source_relative.relative_to(project_path.resolve())
        except ValueError:
            try:
                project_relative = source_relative.relative_to(sources_dir.resolve())
                project_relative = Path("sources") / project_relative
            except ValueError:
                return match.group(0)
        svg_href = "../" + project_relative.as_posix()
        return f"![{alt}]({svg_href})"

    return re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", replace, text)


def _is_external_or_absolute_href(href: str) -> bool:
    return (
        href.startswith(("http://", "https://", "data:", "file:"))
        or Path(href).is_absolute()
    )


def _normalize_strategy(
    data: dict[str, Any],
    request: dict[str, Any],
    pages: int,
    canvas_format: str,
    width: int,
    height: int,
    project_path: Path,
) -> dict[str, Any]:
    outline = data.get("outline")
    if not isinstance(outline, list) or not outline:
        title = request.get("topic") or request.get("prompt") or "Presentation"
        outline = [
            {
                "index": i,
                "title": f"{title} {i}",
                "purpose": "Present one part of the source material.",
                "key_points": [],
                "speaker_note": "",
            }
            for i in range(1, pages + 1)
        ]
    normalized_outline: list[dict[str, Any]] = []
    for i, item in enumerate(outline[:pages], start=1):
        if isinstance(item, dict):
            page = dict(item)
        else:
            page = {"title": str(item)}
        page["index"] = i
        page.setdefault("title", f"Slide {i}")
        page.setdefault("purpose", "")
        page.setdefault("key_points", [])
        page.setdefault("speaker_note", "")
        normalized_outline.append(page)
    template_context = _load_project_template_context(project_path)
    if template_context:
        for page in normalized_outline:
            page["template_reference"] = resolve_page_reference(
                template_context,
                int(page["index"]),
                len(normalized_outline),
            )
    data["outline"] = normalized_outline
    data["canvas"] = data.get("canvas") or {}
    data["canvas"].update({"format": canvas_format, "width": width, "height": height})
    if template_context:
        data["canvas"].update(template_context.get("canvas") or {})
    spec_lock = data.get("spec_lock") if isinstance(data.get("spec_lock"), dict) else {}
    spec_lock.setdefault("canvas", data["canvas"])
    spec_lock.setdefault("colors", data.get("visual_theme", {}))
    spec_lock.setdefault("typography", data.get("typography", {}))
    spec_lock.setdefault("outline", normalized_outline)
    if template_context:
        theme = template_context.get("theme") or {}
        spec_lock["canvas"] = data["canvas"]
        spec_lock["colors"] = theme.get("colors") or {}
        spec_lock["typography"] = theme.get("fonts") or {}
        spec_lock["outline"] = normalized_outline
        spec_lock["template_roles"] = template_context.get("roles") or {}
    data["spec_lock"] = spec_lock
    _normalize_image_slot_hrefs(data, project_path)
    return data


def _normalize_image_slot_hrefs(data: dict[str, Any], project_path: Path) -> None:
    for slides in (data.get("outline"), data.get("slide_briefs"), (data.get("spec_lock") or {}).get("outline")):
        if not isinstance(slides, list):
            continue
        for slide in slides:
            if not isinstance(slide, dict):
                continue
            slots = slide.get("image_slots")
            if not isinstance(slots, list):
                continue
            for slot in slots:
                if not isinstance(slot, dict):
                    continue
                for key in ("path", "svg_href"):
                    value = slot.get(key)
                    if isinstance(value, str):
                        slot[key] = _source_asset_href_for_svg_output(value, project_path)


def _source_asset_href_for_svg_output(href: str, project_path: Path) -> str:
    stripped = href.strip()
    if (
        not stripped
        or stripped.startswith("../")
        or _is_external_or_absolute_href(stripped)
    ):
        return href
    candidate = (project_path / "sources" / stripped).resolve()
    if not candidate.exists():
        return href
    project_relative = candidate.relative_to(project_path.resolve())
    return "../" + project_relative.as_posix()


DESIGN_HEADINGS = (
    "I. Project Information",
    "II. Canvas Specification",
    "III. Visual Theme",
    "IV. Typography System",
    "V. Layout Principles",
    "VI. Icon Usage Specification",
    "VII. Visualization Reference List",
    "VIII. Image Resource List",
    "IX. Content Outline",
    "X. Speaker Notes Requirements",
    "XI. Technical Constraints Reminder",
)


def _compat_confirmations(
    data: dict[str, Any],
    request: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    outline = data.get("outline") if isinstance(data.get("outline"), list) else []
    pages = len(outline) or int(request.get("pages") or 6)
    canvas = data.get("canvas") if isinstance(data.get("canvas"), dict) else {}
    project_info = (
        data.get("project_info")
        if isinstance(data.get("project_info"), dict)
        else {}
    )
    visual_theme = (
        data.get("visual_theme")
        if isinstance(data.get("visual_theme"), dict)
        else {}
    )
    supplied_colors = visual_theme.get("colors")
    colors = {
        "background": "#F7F8FA",
        "primary": "#174EA6",
        "accent": "#00A6A6",
        "body_text": "#202124",
    }
    if isinstance(supplied_colors, dict):
        for key in colors:
            value = str(supplied_colors.get(key) or "")
            if re.fullmatch(r"#[0-9A-Fa-f]{6}", value):
                colors[key] = value
    typography = data.get("typography") if isinstance(data.get("typography"), dict) else {}
    image = data.get("image") if isinstance(data.get("image"), dict) else {}
    icon = data.get("icon") if isinstance(data.get("icon"), dict) else {}
    return {
        "canvas": {"format": str(canvas.get("format") or request.get("canvas_format") or "ppt169")},
        "page_count": {"minimum": pages, "maximum": pages, "recommended": pages},
        "target_audience": {
            "description": str(project_info.get("target_audience") or "general audience")
        },
        "style_objective": {
            "description": str(
                visual_theme.get("style_objective")
                or request.get("style")
                or "clear and content-appropriate"
            )
        },
        "color_scheme": colors,
        "icon_usage": {
            "enabled": bool(icon),
            "style": str(icon.get("style") or "outline"),
        },
        "typography": {
            "title_family": str(typography.get("title_family") or "Microsoft YaHei"),
            "body_family": str(typography.get("body_family") or "Microsoft YaHei"),
            "body_size_px": int(typography.get("body_size_px") or 20),
            "formula_policy": str(typography.get("formula_policy") or "mixed"),
        },
        "image_usage": {
            "mode": str(image.get("mode") or "mixed"),
            "rendering": str(image.get("rendering") or "vector-illustration"),
            "palette": str(image.get("palette") or "match-deck"),
        },
    }


def _ensure_spec_lock_sections(data: dict[str, Any]) -> None:
    lock = data.get("spec_lock") if isinstance(data.get("spec_lock"), dict) else {}
    outline = data.get("outline") if isinstance(data.get("outline"), list) else []
    lock.setdefault("canvas", data.get("canvas") or {})
    theme = data.get("visual_theme") if isinstance(data.get("visual_theme"), dict) else {}
    lock.setdefault("colors", theme.get("colors") or theme)
    lock.setdefault("typography", data.get("typography") or {})
    lock.setdefault("icons", data.get("icon") or {})
    lock.setdefault("images", data.get("image") or {})
    lock.setdefault(
        "page_rhythm",
        {
            f"slide_{index:02d}": "anchor" if index == 1 else "dense"
            for index in range(1, len(outline) + 1)
        },
    )
    lock.setdefault("page_layouts", {})
    lock.setdefault("page_charts", {})
    lock.setdefault("outline", outline)
    data["spec_lock"] = lock


def _validate_confirmed_strategy(data: dict[str, Any]) -> None:
    required = {
        "project_info",
        "canvas",
        "visual_theme",
        "typography",
        "layout",
        "icon",
        "visualization",
        "image",
        "outline",
        "speaker_notes",
        "tech_constraints",
        "spec_lock",
        "confirmations",
    }
    missing = sorted(required - set(data))
    if missing:
        raise LLMError(f"confirmed strategy is missing keys: {missing}")
    if not isinstance(data["outline"], list) or not data["outline"]:
        raise LLMError("confirmed strategy outline is empty")
    if not isinstance(data["confirmations"], dict):
        raise LLMError("confirmed strategy is missing Eight Confirmations")
    validate_confirmations("confirmed_strategy", data["confirmations"])
    _ensure_spec_lock_sections(data)


def _render_design_spec(data: dict[str, Any]) -> str:
    values = {
        DESIGN_HEADINGS[0]: data.get("project_info"),
        DESIGN_HEADINGS[1]: data.get("canvas"),
        DESIGN_HEADINGS[2]: data.get("visual_theme"),
        DESIGN_HEADINGS[3]: data.get("typography"),
        DESIGN_HEADINGS[4]: data.get("layout"),
        DESIGN_HEADINGS[5]: data.get("icon"),
        DESIGN_HEADINGS[6]: data.get("visualization"),
        DESIGN_HEADINGS[7]: data.get("image"),
        DESIGN_HEADINGS[9]: data.get("speaker_notes"),
        DESIGN_HEADINGS[10]: data.get("tech_constraints"),
    }
    lines = ["# PPT Master Design Specification", ""]
    for heading in DESIGN_HEADINGS:
        lines.extend([f"## {heading}", ""])
        if heading == DESIGN_HEADINGS[8]:
            for page in data.get("outline") or []:
                lines.append(f"### Slide {page.get('index')}: {page.get('title')}")
                lines.append(f"- Purpose: {page.get('purpose', '')}")
                if page.get("layout") is not None:
                    lines.append(f"- Layout: {_render_value(page.get('layout'))}")
                if page.get("visualization") is not None:
                    lines.append(
                        f"- Visualization: {_render_value(page.get('visualization'))}"
                    )
                for point in page.get("key_points") or []:
                    lines.append(f"- Key point: {point}")
                for slot in page.get("image_slots") or []:
                    lines.append(f"- Image slot: {_render_value(slot)}")
                if page.get("speaker_note"):
                    lines.append(f"- Speaker note: {page.get('speaker_note')}")
                lines.append("")
            continue
        value = values[heading]
        if heading in {DESIGN_HEADINGS[6], DESIGN_HEADINGS[7]} and not value:
            lines.extend(["None required", ""])
        else:
            lines.extend([_render_value(value) or "None required", ""])
    return "\n".join(lines).strip() + "\n"


def _render_spec_lock(data: dict[str, Any]) -> str:
    lock = data["spec_lock"]
    sections = {
        "canvas": lock["canvas"],
        "colors": lock["colors"],
        "typography": lock["typography"],
        "icons": lock.get("icons", {}),
        "images": lock.get("images", {}),
        "page_rhythm": lock.get("page_rhythm", {}),
        "page_layouts": lock.get("page_layouts", {}),
        "page_charts": lock.get("page_charts", {}),
    }
    lines = ["# PPT Master Execution Lock", ""]
    for section, values in sections.items():
        lines.append(f"## {section}")
        if not values:
            lines.append("- none: true")
        else:
            for key, value in values.items():
                rendered = (
                    json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                    if isinstance(value, (dict, list))
                    else str(value)
                )
                lines.append(f"- {key}: {rendered}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def materialize_confirmed_strategy(
    project_path: Path,
    strategy: dict[str, Any],
) -> dict[str, Any]:
    _validate_confirmed_strategy(strategy)
    design_text = _render_design_spec(strategy)
    for heading in DESIGN_HEADINGS:
        marker = f"## {heading}"
        if marker not in design_text:
            raise LLMError(f"design spec renderer omitted canonical heading: {marker}")
    project_path.mkdir(parents=True, exist_ok=True)
    (project_path / "design_spec.md").write_text(design_text, encoding="utf-8")
    (project_path / "spec_lock.md").write_text(
        _render_spec_lock(strategy),
        encoding="utf-8",
    )
    (project_path / ".service_strategy.json").write_text(
        json.dumps(strategy, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return strategy


def _render_research_markdown(data: dict[str, Any]) -> str:
    lines = [f"# {data.get('title') or 'Research Notes'}", ""]
    sections = data.get("sections") if isinstance(data.get("sections"), list) else []
    for section in sections:
        if not isinstance(section, dict):
            continue
        lines.append(f"## {section.get('heading') or 'Section'}")
        for bullet in section.get("bullets") or []:
            lines.append(f"- {bullet}")
        evidence = section.get("evidence") or []
        if evidence:
            lines.append("")
            lines.append("Evidence:")
            for item in evidence:
                lines.append(f"- {item}")
        lines.append("")
    sources = data.get("sources_to_verify") or []
    if sources:
        lines.append("## Sources To Verify")
        for source in sources:
            lines.append(f"- {source}")
    return "\n".join(lines).strip() + "\n"


def _render_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return "```json\n" + json.dumps(value, ensure_ascii=False, indent=2) + "\n```"


def _clean_svg(raw: str) -> str:
    text = raw.strip()
    fence = re.search(r"```(?:svg|xml)?\s*(.*?)```", text, flags=re.S)
    if fence:
        text = fence.group(1).strip()
    start = text.find("<svg")
    end = text.rfind("</svg>")
    if start < 0 or end < 0:
        return ""
    text = text[start : end + len("</svg>")].strip()
    if "viewBox" not in text:
        return ""
    return text
