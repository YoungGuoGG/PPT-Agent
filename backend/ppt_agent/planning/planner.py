from __future__ import annotations

import json
from typing import Any

from ..providers import ModelProvider
from ..skills import SkillLoader
from ..style_library import StyleLibrary
from .catalog import ToolCatalog
from .compiler import PlanCompileError, PlanCompiler


class AgentPlanner:
    """LLM planner with a deterministic compiler and safe fallback recipe."""

    def __init__(
        self,
        *,
        skills: SkillLoader,
        catalog: ToolCatalog,
        styles: StyleLibrary,
        provider: ModelProvider | None,
        max_protocol_repairs: int = 2,
    ) -> None:
        self.skills = skills
        self.catalog = catalog
        self.styles = styles
        self.provider = provider
        self.compiler = PlanCompiler(catalog)
        self.max_protocol_repairs = max_protocol_repairs

    def create(
        self,
        session_id: str,
        *,
        prompt: str,
        attachments: list[str],
        options: dict[str, Any],
    ):
        requested_style = str(options.get("style_id") or options.get("style_case_id") or "") or None
        fallback_selection = self.styles.select(prompt, requested_style)
        fallback = self._fallback_draft(prompt, attachments, options, fallback_selection)
        if self.provider is None:
            return self.compiler.compile(session_id, fallback)

        system = self.skills.planner().prompt()
        request = {
            "request": {
                "prompt": prompt,
                "attachments": attachments,
                "options": options,
                "interaction_policy": "review_only",
            },
            "tools": self.catalog.planner_summary(),
            "style_catalog": self.styles.summaries(),
            "style_policy": {
                "explicit_style_id": requested_style,
                "required_output": "style_selection.style_id and reason",
                "precedence": "explicit user style, then semantic selection, then neutral fallback",
            },
        }
        feedback: list[dict[str, Any]] = []
        for _ in range(self.max_protocol_repairs + 1):
            user_prompt = json.dumps({**request, "compiler_feedback": feedback}, ensure_ascii=False)
            try:
                draft = self.provider.complete_json(system=system, prompt=user_prompt)
                proposed = draft.get("style_selection") if isinstance(draft, dict) else None
                proposed_id = str((proposed or {}).get("style_id") or "") or None
                reason = str((proposed or {}).get("reason") or "")
                if requested_style:
                    selection = self.styles.select(prompt, requested_style, reason=reason)
                elif proposed_id and self.styles.get(proposed_id):
                    selection = self.styles.select(prompt, proposed_id, reason=reason)
                    selection["mode"] = "planner"
                else:
                    selection = fallback_selection
                draft["style_selection"] = selection
                return self.compiler.compile(session_id, draft)
            except PlanCompileError as exc:
                feedback = exc.issues
            except Exception:
                break
        return self.compiler.compile(session_id, fallback)

    def _fallback_draft(
        self,
        prompt: str,
        attachments: list[str],
        options: dict[str, Any],
        style_selection: dict[str, Any],
    ) -> dict[str, Any]:
        route = self._route(attachments, options)
        assumptions = []
        if not options.get("language"):
            assumptions.append("使用与用户请求一致的语言")
        if not options.get("canvas_format"):
            assumptions.append("使用16:9画布")
        steps: list[dict[str, Any]] = []
        if route == "topic_research":
            steps.append(self._step("research", "research.prepare", []))
            source_deps = ["research"]
        else:
            source_deps = []
        steps.extend(
            [
                self._step("source", "source.ingest", source_deps),
                self._step("style", "style.retrieve", ["source"]),
                self._step("design", "design.compose", ["style"]),
                self._step("assets", "assets.prepare", ["design"]),
                self._step("svg", "svg.generate", ["design", "assets"]),
                self._step("quality", "quality.check_repair", ["svg"]),
                self._step("export", "pptx.export", ["quality"]),
                self._step("review", "review.prepare", ["export"]),
            ]
        )
        return {
            "schema": "ppt-agent.execution-plan.v1",
            "goal": prompt.strip() or "生成演示文稿",
            "route": route,
            "assumptions": assumptions,
            "style_selection": style_selection,
            "steps": steps,
        }

    def _step(self, step_id: str, tool_id: str, dependencies: list[str]) -> dict[str, Any]:
        tool = self.catalog.require(tool_id)
        return {
            "id": step_id,
            "tool": tool_id,
            "title": tool.title,
            "depends_on": dependencies,
            "input_bindings": {},
            "expected_artifacts": list(tool.produces),
        }

    @staticmethod
    def _route(attachments: list[str], options: dict[str, Any]) -> str:
        explicit = options.get("workflow_id")
        if explicit:
            return str(explicit)
        if options.get("template_pptx"):
            return "template_guided_svg"
        if options.get("image_heavy"):
            return "image_heavy"
        if attachments:
            return "document_to_ppt"
        return "topic_research"
