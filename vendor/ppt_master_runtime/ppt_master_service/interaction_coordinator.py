from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Callable

from .conformance import (
    ErrorKind,
    InteractionRequest,
    InteractionSubmission,
    SuggestedAction,
    WorkflowError,
    WorkflowExecutionError,
)
from .job_store import JobStore, utc_now
from .schemas import InteractionResponseRequest


CONFIRMATION_KEYS = {
    "canvas",
    "page_count",
    "target_audience",
    "style_objective",
    "color_scheme",
    "icon_usage",
    "typography",
    "image_usage",
}
HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")
FORMULA_POLICIES = {"mixed", "render-all", "text-only"}
IMAGE_MODES = {"none", "user", "ai", "web", "mixed"}
RevisionCallback = Callable[[dict[str, Any], str], InteractionRequest]


def _invalid(
    interaction_id: str,
    field: str,
    message: str,
    reason: str,
    required: str,
    example: Any,
) -> None:
    raise WorkflowExecutionError(
        WorkflowError(
            kind=ErrorKind.PROTOCOL,
            code="interaction_validation_failed",
            step="confirm_design",
            message=message,
            location={"interaction_id": interaction_id, "field": field},
            correction={
                "reason": reason,
                "required": required,
                "example": example,
            },
            retryable=True,
            suggested_action=SuggestedAction.REQUEST_USER,
        )
    )


def validate_confirmations(
    interaction_id: str,
    values: dict[str, dict[str, Any]],
) -> None:
    missing = sorted(CONFIRMATION_KEYS - set(values))
    extra = sorted(set(values) - CONFIRMATION_KEYS)
    if missing or extra:
        _invalid(
            interaction_id,
            "confirmations",
            "Eight Confirmations fields are invalid",
            f"missing={missing}, extra={extra}",
            "exactly the eight canonical keys",
            sorted(CONFIRMATION_KEYS),
        )

    page = values["page_count"]
    minimum = page.get("minimum")
    maximum = page.get("maximum")
    recommended = page.get("recommended")
    counts = (minimum, maximum, recommended)
    if not all(isinstance(value, int) and value >= 1 for value in counts):
        _invalid(
            interaction_id,
            "confirmations.page_count",
            "Page counts must be positive integers",
            f"received {page}",
            "minimum, maximum, recommended are integers >= 1",
            {"minimum": 3, "maximum": 5, "recommended": 4},
        )
    if not minimum <= recommended <= maximum:
        _invalid(
            interaction_id,
            "confirmations.page_count.recommended",
            "Recommended page count is outside the range",
            f"current value {recommended} is outside {minimum}-{maximum}",
            "minimum <= recommended <= maximum",
            {
                "minimum": minimum,
                "maximum": maximum,
                "recommended": (minimum + maximum) // 2,
            },
        )

    for name in ("background", "primary", "accent", "body_text"):
        value = str(values["color_scheme"].get(name) or "")
        if not HEX.fullmatch(value):
            _invalid(
                interaction_id,
                f"confirmations.color_scheme.{name}",
                "Color must be six-digit HEX",
                f"received {value!r}",
                "#RRGGBB",
                "#174EA6",
            )

    typography = values["typography"]
    if typography.get("formula_policy") not in FORMULA_POLICIES:
        _invalid(
            interaction_id,
            "confirmations.typography.formula_policy",
            "Unknown formula policy",
            f"received {typography.get('formula_policy')!r}",
            "mixed, render-all, or text-only",
            "mixed",
        )
    if (
        not isinstance(typography.get("body_size_px"), int)
        or typography["body_size_px"] < 10
    ):
        _invalid(
            interaction_id,
            "confirmations.typography.body_size_px",
            "Body size is invalid",
            f"received {typography.get('body_size_px')!r}",
            "integer >= 10",
            20,
        )
    if values["image_usage"].get("mode") not in IMAGE_MODES:
        _invalid(
            interaction_id,
            "confirmations.image_usage.mode",
            "Unknown image mode",
            f"received {values['image_usage'].get('mode')!r}",
            "none, user, ai, web, or mixed",
            "mixed",
        )


def _conflict(
    code: str,
    interaction_id: str,
    reason: str,
) -> WorkflowExecutionError:
    return WorkflowExecutionError(
        WorkflowError(
            kind=ErrorKind.PROTOCOL,
            code=code,
            step="confirm_design",
            message=f"{code}: {reason}",
            location={"interaction_id": interaction_id},
            correction={
                "reason": reason,
                "required": "reload the current job interaction",
                "example": {},
            },
            retryable=True,
            suggested_action=SuggestedAction.REQUEST_USER,
        )
    )


def _response_fingerprint(response: InteractionResponseRequest) -> str:
    payload = response.model_dump(mode="json")
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _build_receipt(
    current: dict[str, Any],
    response: InteractionResponseRequest,
) -> dict[str, Any]:
    return {
        "interaction_id": current["interaction_id"],
        "revision": current["revision"],
        "strategy_digest": current["strategy_digest"],
        "confirmations": response.confirmations,
        "generation_mode": response.generation_mode,
        "response_fingerprint": _response_fingerprint(response),
        "checkpoint": current["checkpoint"],
        "accepted_at": utc_now(),
    }


class InteractionCoordinator:
    def __init__(self, store: JobStore) -> None:
        self.store = store

    def pause(
        self,
        job_id: str,
        request: InteractionRequest,
        checkpoint: dict[str, Any],
    ) -> InteractionRequest:
        if not request.strategy_digest.startswith("sha256:"):
            raise ValueError("interaction strategy_digest must start with sha256:")
        validate_confirmations(request.interaction_id, request.confirmations)
        payload = {**request.to_dict(), "checkpoint": checkpoint}
        self.store.save_interaction(job_id, payload, pending=True)
        self.store.update_job(
            job_id,
            status="waiting_user",
            current_step="confirm_design",
            checkpoint=checkpoint,
            interaction=payload,
            pending_interaction_id=request.interaction_id,
            error=None,
            error_detail=None,
        )
        return request

    def submit(
        self,
        job_id: str,
        interaction_id: str,
        response: InteractionResponseRequest,
        revise: RevisionCallback | None,
    ) -> InteractionSubmission:
        with self.store.locked_job(job_id):
            existing = self._existing_submission(job_id, interaction_id, response)
            if existing is not None:
                return existing
            current = self._current(job_id, interaction_id, response)

        if response.action == "revise":
            feedback = str(response.feedback or "").strip()
            if not feedback:
                _invalid(
                    interaction_id,
                    "feedback",
                    "Revision feedback is empty",
                    "action revise requires instructions",
                    "non-empty feedback",
                    "Use a darker palette",
                )
            if revise is None:
                raise RuntimeError("revision callback is required")
            replacement = revise(current, feedback)
            revised_checkpoint = dict(current["checkpoint"])
            if not replacement.strategy_digest.startswith("sha256:"):
                raise ValueError("replacement strategy_digest must start with sha256:")
            validate_confirmations(
                replacement.interaction_id,
                replacement.confirmations,
            )
            with self.store.locked_job(job_id):
                existing = self._existing_submission(
                    job_id, interaction_id, response
                )
                if existing is not None:
                    return existing
                current = self._current(job_id, interaction_id, response)
                stale = {
                    **current,
                    "status": "stale",
                    "replaced_by": replacement.interaction_id,
                }
                self.store.save_interaction(job_id, stale, pending=False)
                self.pause(job_id, replacement, revised_checkpoint)
            return InteractionSubmission(
                False,
                "revise",
                False,
                interaction=replacement,
            )

        validate_confirmations(interaction_id, response.confirmations)
        with self.store.locked_job(job_id):
            existing = self._existing_submission(job_id, interaction_id, response)
            if existing is not None:
                return existing
            current = self._current(job_id, interaction_id, response)
            receipt = _build_receipt(current, response)
            self.store.save_receipt(job_id, interaction_id, receipt)
            closed = {**current, "status": "accepted"}
            self.store.save_interaction(job_id, closed, pending=False)
            self.store.update_job(
                job_id,
                status="queued",
                current_step="resume_pending",
                checkpoint=current["checkpoint"],
                interaction=None,
                pending_interaction_id=None,
                error=None,
            )
        return InteractionSubmission(True, "confirm", True, receipt=receipt)

    def _existing_submission(
        self,
        job_id: str,
        interaction_id: str,
        response: InteractionResponseRequest,
    ) -> InteractionSubmission | None:
        existing_receipt = self.store.get_receipt(job_id, interaction_id)
        if existing_receipt is None:
            return None
        if existing_receipt["response_fingerprint"] != _response_fingerprint(response):
            raise _conflict(
                "interaction_closed",
                interaction_id,
                "the interaction already has a different accepted response",
            )
        return InteractionSubmission(
            True,
            response.action,
            False,
            receipt=existing_receipt,
        )

    def _current(
        self,
        job_id: str,
        interaction_id: str,
        response: InteractionResponseRequest,
    ) -> dict[str, Any]:
        current = self.store.get_pending_interaction(job_id)
        if current is None:
            raise _conflict(
                "no_pending_interaction",
                interaction_id,
                "the job has no pending interaction",
            )
        if current["interaction_id"] != interaction_id:
            raise _conflict(
                "interaction_stale",
                interaction_id,
                f"current interaction is {current['interaction_id']}",
            )
        if current["revision"] != response.revision:
            raise _conflict(
                "interaction_stale",
                interaction_id,
                f"current revision is {current['revision']}",
            )
        if current["strategy_digest"] != response.strategy_digest:
            raise _conflict(
                "strategy_changed",
                interaction_id,
                "submitted digest does not match the pending strategy",
            )
        return current
