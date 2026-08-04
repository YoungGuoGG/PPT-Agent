from __future__ import annotations

import json
from pathlib import Path
from typing import Any


EXECUTABLE_ROUTES = {
    "document_to_ppt",
    "template_guided_svg",
    "image_heavy",
    "mirror_template",
}


class WorkflowCatalog:
    """V2 route catalog for the UI and Planner admission guard."""

    def __init__(self, ppt_runtime_root: Path) -> None:
        self.root = ppt_runtime_root.resolve()

    def list(self) -> list[dict[str, Any]]:
        path = self.root / "workflows" / "registry.json"
        payload = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {"workflows": []}
        items: list[dict[str, Any]] = []
        for raw in payload.get("workflows") or []:
            workflow_id = str(raw.get("id") or "")
            executable = workflow_id in EXECUTABLE_ROUTES and raw.get("maturity") == "complete"
            items.append(
                {
                    **raw,
                    "executable": executable,
                    "issues": [] if executable else [
                        {
                            "code": "v2_route_not_accepted",
                            "message": f"V2 route is not accepted yet: {workflow_id}",
                        }
                    ],
                }
            )
        return items

    def require_executable(self, workflow_id: str) -> dict[str, Any]:
        entry = next((item for item in self.list() if item["id"] == workflow_id), None)
        if entry is None:
            raise ValueError(f"unknown workflow: {workflow_id}")
        if not entry["executable"]:
            raise ValueError(f"workflow is not executable in V2: {workflow_id}")
        return entry
