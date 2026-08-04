from __future__ import annotations

from typing import Any


class FakeModelProvider:
    def __init__(self, response: dict[str, Any] | None = None) -> None:
        self.response = response or {"ok": True}

    def complete_messages(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        return dict(self.response)

    def complete_json(self, *, system: str, prompt: str, schema: dict[str, Any] | None = None) -> dict[str, Any]:
        return dict(self.response)

    def review_image(self, *, image_path: str, prompt: str) -> dict[str, Any]:
        return {"page": image_path, "findings": [], "passed": True}
