from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Protocol
import json


class RepairModel(Protocol):
    def __call__(self, instruction: dict[str, Any]) -> dict[str, Any]: ...


class OpenAICompatibleRepairModel:
    """Adapt the existing JSON model caller to the constrained repair contract."""

    def __init__(self, caller: Callable[[str, str | None], dict[str, Any]]) -> None:
        self.caller = caller
        self.system_prompt = _prompt_path().read_text(encoding="utf-8")

    def __call__(self, instruction: dict[str, Any]) -> dict[str, Any]:
        return self.caller(
            json.dumps(instruction, ensure_ascii=False, indent=2),
            self.system_prompt,
        )


def _prompt_path() -> Path:
    return Path(__file__).resolve().parents[1] / "prompts" / "svg_repair_patch_prompt.md"
