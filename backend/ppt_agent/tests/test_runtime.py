from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from ppt_agent.core import AgentRuntime
from ppt_agent.domain.models import ReviewScope
from ppt_agent.workflows.contracts import EventSink, WorkflowOutput
from ppt_agent.workflows.catalog import WorkflowCatalog


class FakeExecutor:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.cancelled: list[str] = []

    def execute(self, workflow_id: str, inputs: dict[str, Any], emit: EventSink) -> WorkflowOutput:
        self.calls.append((workflow_id, inputs))
        emit("fake_execution", {"workflow_id": workflow_id})
        index = len(self.calls)
        pptx = self.root / f"output_{index}.pptx"
        pptx.write_bytes(b"PK\x03\x04fake-pptx")
        project = self.root / f"project_{index}"
        project.mkdir()
        svg_dir = project / "svg_output"
        svg_dir.mkdir()
        (svg_dir / "slide_01.svg").write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720"><text x="40" y="80">Preview</text></svg>',
            encoding="utf-8",
        )
        return WorkflowOutput(workflow_id, {"pptx": str(pptx), "project": str(project)}, {"fake": True})

    def cancel(self, execution_id: str) -> None:
        self.cancelled.append(execution_id)


class ComposedFakeExecutor(FakeExecutor):
    def execute_plan(
        self,
        workflow_id: str,
        inputs: dict[str, Any],
        steps: list[str],
        emit: EventSink,
    ) -> WorkflowOutput:
        self.calls.append((workflow_id, {**inputs, "dispatched_steps": list(steps)}))
        for step in steps:
            emit("subworkflow_started", {"workflow_id": step})
            emit("subworkflow_succeeded", {"workflow_id": step})
        pptx = self.root / "composed.pptx"
        pptx.write_bytes(b"PK\x03\x04fake-pptx")
        project = self.root / "composed-project"
        project.mkdir()
        return WorkflowOutput(workflow_id, {"pptx": str(pptx), "project": str(project)})


class AgentRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.executor = FakeExecutor(self.root)
        self.runtime = AgentRuntime(root=self.root, executor=self.executor)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_end_to_end_generation_reaches_review_without_user_interaction(self) -> None:
        source = self.root / "paper.pdf"
        source.write_bytes(b"%PDF-1.4\n")
        conversation = self.runtime.create_conversation("Paper deck")
        queued = self.runtime.submit(conversation["id"], "提取论文架构并生成汇报", [str(source)], {"pages": 10})
        result = self.runtime.run(queued["session"]["id"])

        self.assertEqual(result["session"]["status"], "review_ready")
        self.assertEqual(self.executor.calls[0][0], "document_to_ppt")
        self.assertTrue(all(step["status"] == "succeeded" for step in result["plan"]["steps"]))
        self.assertEqual(len(result["versions"]), 1)
        self.assertTrue(Path(result["versions"][0]["pptx_path"]).is_file())
        self.assertEqual(
            len([artifact for artifact in result["artifacts"] if artifact["role"].startswith("page_preview_v1_")]),
            1,
        )
        event_types = {event["type"] for event in result["events"]}
        self.assertIn("review_ready", event_types)
        self.assertNotIn("waiting_user", event_types)

    def test_page_revision_creates_child_version_and_can_be_accepted(self) -> None:
        source = self.root / "notes.md"
        source.write_text("# Notes", encoding="utf-8")
        conversation = self.runtime.create_conversation()
        queued = self.runtime.submit(conversation["id"], "生成演示", [str(source)], {})
        first = self.runtime.run(queued["session"]["id"])
        revised = self.runtime.revise(first["session"]["id"], ReviewScope.SELECTED_PAGES, "减少文字", [3, 5])

        self.assertEqual(len(revised["versions"]), 2)
        self.assertEqual(revised["versions"][-1]["parent_id"], revised["versions"][0]["id"])
        self.assertEqual(revised["versions"][-1]["pages"], [3, 5])
        self.assertIn("第 3, 5 页", self.executor.calls[-1][1]["prompt"])

        accepted = self.runtime.accept_version(revised["session"]["id"], revised["versions"][-1]["id"])
        self.assertEqual(accepted["session"]["status"], "succeeded")
        self.assertEqual(accepted["versions"][-1]["status"], "accepted")

    def test_selected_design_md_is_injected_as_canonical_contract(self) -> None:
        source = self.root / "input.md"
        source.write_text("content", encoding="utf-8")
        conversation = self.runtime.create_conversation()
        queued = self.runtime.submit(conversation["id"], "生成", [str(source)], {"style_id": "editorial-warm"})
        self.runtime.run(queued["session"]["id"])
        inputs = self.executor.calls[0][1]
        self.assertEqual(inputs["style_id"], "editorial-warm")
        self.assertIn("Canonical Style Contract", inputs["extra_requirements"])
        self.assertIn("## Layout Grammar", inputs["extra_requirements"])

    def test_composed_executor_dispatches_each_plan_step_in_order(self) -> None:
        executor = ComposedFakeExecutor(self.root)
        runtime = AgentRuntime(root=self.root, executor=executor)
        source = self.root / "composed.md"
        source.write_text("content", encoding="utf-8")
        conversation = runtime.create_conversation()
        queued = runtime.submit(conversation["id"], "生成完整演示文稿", [str(source)], {})

        result = runtime.run(queued["session"]["id"])

        dispatched = executor.calls[0][1]["dispatched_steps"]
        self.assertEqual(dispatched, [step["workflow_id"] for step in result["plan"]["steps"]])
        event_steps = [
            event["payload"]["workflow_id"]
            for event in result["events"]
            if event["type"] == "subworkflow_started"
        ]
        self.assertEqual(event_steps, dispatched)
        self.assertTrue(all(step["status"] == "succeeded" for step in result["plan"]["steps"]))

    def test_style_provenance_is_recorded_in_markdown_design_lock(self) -> None:
        project = self.root / "provenance-project"
        project.mkdir()
        (project / "design_spec.md").write_text("# Design\n", encoding="utf-8")
        (project / "spec_lock.md").write_text("# PPT Master Execution Lock\n\n## canvas\n", encoding="utf-8")
        selection = {
            "style_id": "academic-defense",
            "design_version": "1.0.0",
            "design_hash": "a" * 64,
            "mode": "planner",
            "reason": "论文汇报",
        }

        self.runtime._record_style_provenance(str(project), selection)
        self.runtime._record_style_provenance(str(project), selection)

        design = (project / "design_spec.md").read_text(encoding="utf-8")
        lock = (project / "spec_lock.md").read_text(encoding="utf-8")
        self.assertIn('"source_style_id": "academic-defense"', design)
        self.assertEqual(lock.count("## source_style"), 1)
        self.assertIn("- sha256: " + "a" * 64, lock)

    def test_cancel_propagates_to_active_legacy_execution(self) -> None:
        source = self.root / "cancel.md"
        source.write_text("content", encoding="utf-8")
        conversation = self.runtime.create_conversation()
        queued = self.runtime.submit(conversation["id"], "生成", [str(source)], {})
        session_id = queued["session"]["id"]
        self.runtime.store.append_event(session_id, "legacy_job_created", {"job_id": "legacy-123"})

        cancelled = self.runtime.cancel(session_id)

        self.assertEqual(cancelled["session"]["status"], "cancelled")
        self.assertEqual(self.executor.cancelled, ["legacy-123"])


class WorkflowCatalogTests(unittest.TestCase):
    def test_v2_catalog_admits_only_accepted_complete_routes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflows = root / "workflows"
            workflows.mkdir()
            registry = workflows / "registry.json"
            registry.write_text(
                json.dumps(
                    {
                        "workflows": [
                            {"id": "document_to_ppt", "maturity": "complete"},
                            {"id": "live_preview", "maturity": "partial"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            catalog = WorkflowCatalog(root)
            items = {item["id"]: item for item in catalog.list()}

            self.assertTrue(items["document_to_ppt"]["executable"])
            self.assertFalse(items["live_preview"]["executable"])


if __name__ == "__main__":
    unittest.main()
