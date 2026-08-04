from __future__ import annotations

import json
import shutil
from collections import Counter
from pathlib import Path
from typing import Any, Callable

from ..llm_client import FailureKind, classify_llm_failure
from .models import QualityFinding, RepairAttempt, RepairOutcome
from .patches import PatchValidationError, apply_patch
from .recipes import REPAIR_RECIPES


RepairModel = Callable[[dict[str, Any]], dict[str, Any]]
PageChecker = Callable[[Path, Path], list[QualityFinding]]


class RepairEngine:
    def __init__(self, *, model: RepairModel, checker: PageChecker) -> None:
        self.model = model
        self.checker = checker

    def repair_page(
        self,
        project_path: Path,
        svg_path: Path,
        findings: list[QualityFinding],
        *,
        page_context: dict[str, Any] | None = None,
        max_attempts: int = 2,
    ) -> RepairOutcome:
        repair_root = project_path / "repair"
        candidates_dir = repair_root / "candidates"
        attempts_dir = repair_root / "attempts"
        outcomes_dir = repair_root / "outcomes"
        for directory in (candidates_dir, attempts_dir, outcomes_dir):
            directory.mkdir(parents=True, exist_ok=True)

        attempts: list[RepairAttempt] = []
        current_findings = list(findings)
        previous_attempt: dict[str, Any] | None = None
        original = svg_path.read_text(encoding="utf-8", errors="replace")
        working = original

        for attempt_number in range(1, max(0, max_attempts) + 1):
            primary = current_findings[0]
            recipe = REPAIR_RECIPES.get(primary.code)
            if recipe is None:
                break
            instruction = self._instruction(
                primary,
                recipe.to_instruction(),
                page_context or {},
                previous_attempt,
            )
            patch: dict[str, Any] | None = None
            rejection = ""
            remaining: list[QualityFinding] = []
            candidate_path = candidates_dir / f"{svg_path.stem}.attempt_{attempt_number:02d}.svg"
            try:
                patch = self.model(instruction)
                candidate = apply_patch(working, patch, recipe)
                candidate_path.write_text(candidate, encoding="utf-8")
                remaining = self.checker(project_path, candidate_path)
                if remaining:
                    if self._is_expected_progress(current_findings, remaining, primary.code):
                        record = RepairAttempt(
                            attempt=attempt_number,
                            patch=patch,
                            status="accepted_partial",
                            remaining_findings=[item.to_dict() for item in remaining],
                        )
                        attempts.append(record)
                        self._write_json(
                            attempts_dir / f"{svg_path.stem}.attempt_{attempt_number:02d}.json",
                            record.to_dict(),
                        )
                        working = candidate
                        current_findings = remaining
                        previous_attempt = None
                        continue
                    rejection = "; ".join(f"{item.code}: {item.message}" for item in remaining)
                    raise PatchValidationError(rejection)
                backup = repair_root / f"{svg_path.stem}.original.svg"
                if not backup.exists():
                    shutil.copy2(svg_path, backup)
                svg_path.write_text(candidate, encoding="utf-8")
                record = RepairAttempt(attempt_number, patch, "accepted")
                attempts.append(record)
                self._write_json(attempts_dir / f"{svg_path.stem}.attempt_{attempt_number:02d}.json", record.to_dict())
                outcome = RepairOutcome("repaired", str(svg_path), [], attempts)
                self._write_json(outcomes_dir / f"{svg_path.stem}.json", outcome.to_dict())
                return outcome
            except Exception as exc:
                rejection = rejection or str(exc)
                failure_kind = classify_llm_failure(exc)
                attempt_status = "transport_retry" if failure_kind == FailureKind.TRANSPORT else "rejected"
                record = RepairAttempt(
                    attempt=attempt_number,
                    patch=patch,
                    status=attempt_status,
                    rejection=rejection,
                    remaining_findings=[item.to_dict() for item in remaining],
                )
                attempts.append(record)
                self._write_json(attempts_dir / f"{svg_path.stem}.attempt_{attempt_number:02d}.json", record.to_dict())
                if failure_kind == FailureKind.TRANSPORT:
                    previous_attempt = None
                else:
                    previous_attempt = {
                        "attempt": attempt_number,
                        "patch": patch,
                        "rejected_because": rejection,
                    }

        outcome = RepairOutcome(
            status="failed",
            repaired_file=None,
            remaining_findings=current_findings,
            attempts=attempts,
            requires_regeneration=True,
        )
        self._write_json(outcomes_dir / f"{svg_path.stem}.json", outcome.to_dict())
        return outcome

    @staticmethod
    def _is_expected_progress(
        before: list[QualityFinding],
        after: list[QualityFinding],
        repaired_code: str,
    ) -> bool:
        allowed_remaining = Counter(item.code for item in before)
        allowed_remaining[repaired_code] -= 1
        if allowed_remaining[repaired_code] <= 0:
            del allowed_remaining[repaired_code]
        actual_remaining = Counter(item.code for item in after)
        return bool(actual_remaining) and not (actual_remaining - allowed_remaining)

    def _instruction(
        self,
        finding: QualityFinding,
        recipe: dict[str, Any],
        page_context: dict[str, Any],
        previous_attempt: dict[str, Any] | None,
    ) -> dict[str, Any]:
        repeated = sorted(set(recipe.get("forbidden") or [])) if previous_attempt else []
        finding_payload = {
            "code": finding.code,
            "severity": finding.severity,
            "file": finding.file,
            "message": finding.message,
        }
        return {
            "finding": finding_payload,
            "location": finding.location.to_dict(),
            "recipe": recipe,
            "page_context": page_context,
            "previous_attempt": previous_attempt,
            "forbidden_repeated_actions": repeated,
            "output_contract": {
                "type": "object",
                "required": ["operations", "summary"],
                "note": "Return constrained patch operations only; never return a complete SVG document.",
            },
        }

    def _write_json(self, path: Path, payload: dict[str, Any]) -> None:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
