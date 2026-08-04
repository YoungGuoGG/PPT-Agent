from __future__ import annotations

from typing import Any, Protocol


class ModelProvider(Protocol):
    def complete_messages(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        ...

    def complete_json(self, *, system: str, prompt: str, schema: dict[str, Any] | None = None) -> dict[str, Any]:
        ...

    def review_image(self, *, image_path: str, prompt: str) -> dict[str, Any]:
        ...
