from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ppt_agent.planning import AgentPlanner, PlanCompileError, PlanCompiler, ToolCatalog
from ppt_agent.skills import SkillLoader
from ppt_agent.storage import AgentStore
from ppt_agent.style_library import StyleLibrary


class PlanningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[2]
        self.temp = tempfile.TemporaryDirectory()
        self.catalog = ToolCatalog()
        self.compiler = PlanCompiler(self.catalog)
        temp = Path(self.temp.name)
        self.styles = StyleLibrary(self.root / "style-library", temp / "styles", AgentStore(temp / "agent.db"))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_fallback_plan_is_compiled_and_ends_in_review(self) -> None:
        planner = AgentPlanner(
            skills=SkillLoader(self.root / "skills"),
            catalog=self.catalog,
            styles=self.styles,
            provider=None,
        )
        plan = planner.create(
            "session-1",
            prompt="根据论文生成演示文稿",
            attachments=["paper.pdf"],
            options={},
        )
        self.assertEqual(plan.workflow_id, "document_to_ppt")
        self.assertEqual(plan.steps[-1].workflow_id, "review.prepare")
        self.assertNotIn("live_preview", [step.workflow_id for step in plan.steps])
        self.assertEqual(plan.style_selection["style_id"], "academic-defense")
        self.assertEqual(len(plan.style_selection["design_hash"]), 64)

    def test_explicit_style_overrides_semantic_selection(self) -> None:
        planner = AgentPlanner(
            skills=SkillLoader(self.root / "skills"),
            catalog=self.catalog,
            styles=self.styles,
            provider=None,
        )

        plan = planner.create(
            "session-2",
            prompt="根据论文生成学术汇报",
            attachments=["paper.pdf"],
            options={"style_id": "editorial-warm"},
        )

        self.assertEqual(plan.style_selection["style_id"], "editorial-warm")
        self.assertEqual(plan.style_selection["mode"], "user")

    def test_compiler_rejects_preview_server(self) -> None:
        with self.assertRaises(PlanCompileError):
            self.compiler.compile(
                "session-1",
                {
                    "schema": "ppt-agent.execution-plan.v1",
                    "goal": "bad",
                    "route": "document_to_ppt",
                    "steps": [
                        {"id": "preview", "tool": "svg_editor.server"},
                        {"id": "review", "tool": "review.prepare", "depends_on": ["preview"]},
                    ],
                },
            )

    def test_design_skill_is_not_available_for_export(self) -> None:
        loader = SkillLoader(self.root / "skills")
        with self.assertRaises(ValueError):
            loader.design_for("pptx.export")


if __name__ == "__main__":
    unittest.main()
