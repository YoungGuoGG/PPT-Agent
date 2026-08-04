from __future__ import annotations

import re
from typing import Any

from ..domain.models import ExecutionPlan, PlanStep, new_id
from .catalog import ToolCatalog


STEP_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class PlanCompileError(ValueError):
    def __init__(self, issues: list[dict[str, Any]]) -> None:
        self.issues = issues
        super().__init__("; ".join(str(issue.get("message")) for issue in issues))


class PlanCompiler:
    def __init__(self, catalog: ToolCatalog) -> None:
        self.catalog = catalog

    def compile(self, session_id: str, draft: dict[str, Any]) -> ExecutionPlan:
        issues: list[dict[str, Any]] = []
        if draft.get("schema") != "ppt-agent.execution-plan.v1":
            issues.append(self._issue("plan.schema", "schema must be ppt-agent.execution-plan.v1"))
        raw_steps = draft.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            issues.append(self._issue("plan.steps", "steps must be a non-empty array"))
            raise PlanCompileError(issues)

        ids: set[str] = set()
        steps: list[PlanStep] = []
        for index, raw in enumerate(raw_steps):
            if not isinstance(raw, dict):
                issues.append(self._issue("plan.step_type", "step must be an object", index=index))
                continue
            step_id = str(raw.get("id") or "")
            tool_id = str(raw.get("tool") or "")
            if not STEP_ID.fullmatch(step_id):
                issues.append(self._issue("plan.step_id", "invalid step id", index=index, value=step_id))
            if step_id in ids:
                issues.append(self._issue("plan.step_duplicate", "duplicate step id", index=index, value=step_id))
            ids.add(step_id)
            tool = self.catalog.get(tool_id)
            if tool is None:
                issues.append(self._issue("plan.tool_unknown", "tool is not registered", index=index, value=tool_id))
            elif not tool.executable:
                issues.append(self._issue("plan.tool_unavailable", "tool is not executable", index=index, value=tool_id))
            elif tool.interactive:
                issues.append(self._issue("plan.interaction_forbidden", "interactive tool is forbidden before review", index=index, value=tool_id))
            depends_on = raw.get("depends_on") or []
            if not isinstance(depends_on, list) or any(not isinstance(item, str) for item in depends_on):
                issues.append(self._issue("plan.dependencies", "depends_on must be an array of step ids", index=index))
                depends_on = []
            input_bindings = raw.get("input_bindings") or {}
            expected = raw.get("expected_artifacts") or (list(tool.produces) if tool else [])
            if not isinstance(input_bindings, dict):
                issues.append(self._issue("plan.bindings", "input_bindings must be an object", index=index))
                input_bindings = {}
            if not isinstance(expected, list) or any(not isinstance(item, str) for item in expected):
                issues.append(self._issue("plan.artifacts", "expected_artifacts must be an array of strings", index=index))
                expected = []
            steps.append(
                PlanStep(
                    step_id,
                    tool_id,
                    str(raw.get("title") or (tool.title if tool else tool_id)),
                    depends_on=list(depends_on),
                    input_bindings=dict(input_bindings),
                    output_artifacts=list(expected),
                )
            )

        for step in steps:
            for dependency in step.depends_on:
                if dependency not in ids:
                    issues.append(self._issue("plan.dependency_missing", "dependency does not exist", step=step.id, value=dependency))
        if not issues:
            cycle = self._find_cycle(steps)
            if cycle:
                issues.append(self._issue("plan.cycle", "plan dependencies contain a cycle", value=cycle))
        if steps and steps[-1].workflow_id != "review.prepare":
            issues.append(self._issue("plan.review_required", "initial generation must end with review.prepare"))
        if any(step.workflow_id in {"live_preview", "svg_editor.server"} for step in steps):
            issues.append(self._issue("plan.preview_forbidden", "preview servers are not executable tools"))
        route = str(draft.get("route") or "document_to_ppt")
        if route in {"document_to_ppt", "topic_research", "template_guided_svg", "image_heavy"}:
            required = {
                "source.ingest",
                "design.compose",
                "style.retrieve",
                "assets.prepare",
                "svg.generate",
                "quality.check_repair",
                "pptx.export",
                "review.prepare",
            }
            actual = {step.workflow_id for step in steps}
            missing = sorted(required - actual)
            if missing:
                issues.append(self._issue("plan.core_tools_missing", "generation plan is incomplete", value=missing))
        if issues:
            raise PlanCompileError(issues)

        goal = str(draft.get("goal") or "生成演示文稿")
        assumptions = [str(item) for item in (draft.get("assumptions") or []) if str(item).strip()]
        style_selection = draft.get("style_selection") or {}
        if not isinstance(style_selection, dict) or not style_selection.get("style_id"):
            raise PlanCompileError(
                [self._issue("plan.style_required", "style_selection must identify a published style")]
            )
        return ExecutionPlan(
            id=new_id("plan"),
            session_id=session_id,
            workflow_id=route,
            steps=steps,
            goal=goal,
            assumptions=assumptions,
            style_selection=dict(style_selection),
        )

    @staticmethod
    def _find_cycle(steps: list[PlanStep]) -> list[str]:
        graph = {step.id: list(step.depends_on) for step in steps}
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node: str, path: list[str]) -> list[str]:
            if node in visiting:
                return path[path.index(node):] + [node]
            if node in visited:
                return []
            visiting.add(node)
            for dependency in graph.get(node, []):
                cycle = visit(dependency, path + [dependency])
                if cycle:
                    return cycle
            visiting.remove(node)
            visited.add(node)
            return []

        for node in graph:
            cycle = visit(node, [node])
            if cycle:
                return cycle
        return []

    @staticmethod
    def _issue(code: str, message: str, **detail: Any) -> dict[str, Any]:
        return {"code": code, "message": message, **detail}
