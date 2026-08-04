from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from .agent_phase_runner import AgentPhaseRunner
from .conformance import AgentPhaseRequest
from .tools.template_guided_policy import (
    build_page_bindings,
    check_template_page_findings,
    check_template_project_findings,
    load_template_context,
)


class TemplateGuidedPhaseRunner(AgentPhaseRunner):
    """Apply deterministic imported-template constraints to the shared Agent loop."""

    @classmethod
    def from_base(cls, base: AgentPhaseRunner) -> "TemplateGuidedPhaseRunner":
        def page_checker(project_path: str, svg_path: str) -> list[dict[str, Any]]:
            return [
                *base.page_checker(project_path, svg_path),
                *check_template_page_findings(project_path, svg_path),
            ]

        def project_checker(project_path: str) -> list[dict[str, Any]]:
            return [
                *base.project_checker(project_path),
                *check_template_project_findings(project_path),
            ]

        return cls(
            store=base.store,
            call_messages=base.call_messages,
            page_checker=page_checker,
            project_checker=project_checker,
            max_protocol_repairs=base.max_protocol_repairs,
        )

    def _new_session(self, request: AgentPhaseRequest) -> dict[str, Any]:
        session = super()._new_session(request)
        project = Path(request.project_path).expanduser().resolve()
        context = load_template_context(project)
        page_count = int(request.checkpoint.get("page_count") or 0)
        if page_count < 1:
            raise ValueError("template-guided Agent phase requires a positive page_count")
        bindings = build_page_bindings(context, page_count)
        instruction = {
            "template_contract": {
                "bindings": bindings,
                "rules": [
                    "Use the current page binding from spec_lock.page_layouts.",
                    "Read every distinct bound SVG exactly once before page 1.",
                    "Literal pages preserve every non-text template anchor.",
                    "Adapted pages preserve full-canvas and outside-content-area chrome.",
                    "Replace audience-facing sample text inside the editable content area.",
                    "Repair only the reported page in this same Agent session.",
                ],
            }
        }
        session["messages"][0]["content"] += (
            "\n\n## Template-guided execution contract\n\n"
            + json.dumps(instruction, ensure_ascii=False, indent=2)
        )
        return session

    def _dispatch(
        self,
        request: AgentPhaseRequest,
        session: dict[str, Any],
        action: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        if action == "submit_strategy_draft" and request.phase == "strategist_draft":
            strategy = arguments.get("strategy")
            if isinstance(strategy, dict):
                strategy = copy.deepcopy(strategy)
                page_count = int(request.checkpoint.get("page_count") or 0)
                context = load_template_context(Path(request.project_path))
                bindings = build_page_bindings(context, page_count)
                _lock_template_identity(strategy, context)
                lock = strategy.get("spec_lock")
                if not isinstance(lock, dict):
                    lock = {}
                    strategy["spec_lock"] = lock
                lock["page_layouts"] = {
                    page: {
                        "path": item["svg_path"],
                        "role": item["role"],
                        "behavior": item["behavior"],
                        "layout_policy": item["layout_policy"],
                        "content_area": item["content_area"],
                        "reference_digest": item["reference_digest"],
                    }
                    for page, item in bindings.items()
                }
                arguments = {**arguments, "strategy": strategy}
        return super()._dispatch(request, session, action, arguments)


def _lock_template_identity(
    strategy: dict[str, Any], context: dict[str, Any]
) -> None:
    confirmations = strategy.get("confirmations")
    if not isinstance(confirmations, dict):
        confirmations = {}
        strategy["confirmations"] = confirmations
    theme = context["theme"]
    colors = theme["colors"]
    locked_colors = {
        "background": _first_color(colors, "background", "lt1", "bg1", default="#FFFFFF"),
        "primary": _first_color(colors, "primary", "accent1", "dk1", default="#202124"),
        "accent": _first_color(colors, "accent", "accent2", "accent1", default="#005587"),
        "body_text": _first_color(colors, "body_text", "dk1", "tx1", default="#202124"),
    }
    existing_typography = confirmations.get("typography")
    if not isinstance(existing_typography, dict):
        existing_typography = {}
    fonts = theme["fonts"]
    locked_typography = {
        **existing_typography,
        "title_family": str(
            fonts.get("majorLatin")
            or fonts.get("majorEastAsia")
            or existing_typography.get("title_family")
            or "Aptos Display"
        ),
        "body_family": str(
            fonts.get("minorLatin")
            or fonts.get("minorEastAsia")
            or existing_typography.get("body_family")
            or "Aptos"
        ),
        "body_size_px": int(existing_typography.get("body_size_px") or 20),
        "formula_policy": str(existing_typography.get("formula_policy") or "mixed"),
    }
    confirmations["color_scheme"] = locked_colors
    confirmations["typography"] = locked_typography
    visual_theme = strategy.get("visual_theme")
    if not isinstance(visual_theme, dict):
        visual_theme = {}
        strategy["visual_theme"] = visual_theme
    visual_theme["colors"] = locked_colors
    strategy["typography"] = locked_typography


def _first_color(
    colors: dict[str, Any], *keys: str, default: str
) -> str:
    for key in keys:
        value = str(colors.get(key) or "").strip()
        if value:
            return value.upper() if value.startswith("#") else value
    return default
