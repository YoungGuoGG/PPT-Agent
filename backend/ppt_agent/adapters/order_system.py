from __future__ import annotations

from pathlib import Path
from typing import Any

from ..core import AgentRuntime


class OrderSystemAgentAdapter:
    """Pure adapter: accepts claimed-order data and returns artifacts; never edits order SQLite."""

    def __init__(self, runtime: AgentRuntime) -> None:
        self.runtime = runtime

    def execute(self, *, request_text: str, source_files: list[str], preferences: dict[str, Any] | None = None) -> dict[str, Any]:
        conversation = self.runtime.create_conversation("Order System presentation")
        snapshot = self.runtime.submit(conversation["id"], request_text, source_files, preferences or {})
        result = self.runtime.run(snapshot["session"]["id"])
        if result["session"]["status"] != "review_ready":
            raise RuntimeError(f"PPT Agent order execution failed: {result['session'].get('error')}")
        version = result["versions"][-1]
        return {
            "session_id": result["session"]["id"],
            "project_path": version.get("project_path"),
            "review_pptx": str(Path(version["pptx_path"])),
        }
