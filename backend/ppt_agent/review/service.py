from __future__ import annotations

from typing import Any

from ..domain.models import ReviewScope


class ReviewService:
    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    def revise_whole_deck(self, session_id: str, instruction: str) -> dict[str, Any]:
        return self.runtime.revise(session_id, ReviewScope.WHOLE_DECK, instruction, [])

    def revise_pages(self, session_id: str, pages: list[int], instruction: str) -> dict[str, Any]:
        return self.runtime.revise(session_id, ReviewScope.SELECTED_PAGES, instruction, pages)

    def accept(self, session_id: str, version_id: str) -> dict[str, Any]:
        return self.runtime.accept_version(session_id, version_id)

    def rollback(self, session_id: str, version_id: str) -> dict[str, Any]:
        return self.runtime.rollback_version(session_id, version_id)
