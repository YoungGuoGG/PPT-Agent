from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from . import llm_runtime
from .conformance import (
    AgentPhaseRequest,
    AgentPhaseResult,
    ErrorKind,
    SuggestedAction,
    WorkflowError,
    WorkflowExecutionError,
)
from .job_store import JobStore
from .llm_client import (
    FailureKind,
    LLMError,
    call_llm_messages_json,
    classify_llm_failure,
)
from .tools import quality_tools


ALLOWED_ACTIONS = {
    "submit_strategy_draft",
    "read_spec_lock",
    "read_project_file",
    "read_reference_svg",
    "write_svg_page",
    "repair_svg_page",
    "run_chart_calculator",
    "repair_chart_page",
    "submit_chart_receipt",
    "report_phase_complete",
}
MessageCaller = Callable[[list[dict[str, Any]]], dict[str, Any]]
FindingChecker = Callable[..., list[dict[str, Any]]]


class AgentPhaseRunner:
    def __init__(
        self,
        store: JobStore,
        call_messages: MessageCaller = call_llm_messages_json,
        page_checker: FindingChecker = quality_tools.check_svg_page_findings,
        project_checker: FindingChecker = quality_tools.check_svg_project_findings,
        max_protocol_repairs: int = 2,
    ) -> None:
        self.store = store
        self.call_messages = call_messages
        self.page_checker = page_checker
        self.project_checker = project_checker
        self.max_protocol_repairs = max_protocol_repairs

    def _new_session(self, request: AgentPhaseRequest) -> dict[str, Any]:
        prompt_name = (
            "strategist_prompt.md"
            if request.phase == "strategist_draft"
            else "executor_prompt.md"
        )
        system_prompt = (
            Path(__file__).resolve().parent / "prompts" / prompt_name
        ).read_text(encoding="utf-8")
        if request.phase == "strategist_draft":
            system_prompt += (
                "\n\n## Persistent Agent protocol override\n\n"
                "For this persistent Agent session, wrap the complete strategy "
                "described above in exactly one action envelope:\n\n"
                "```json\n"
                '{"action": "submit_strategy_draft", "arguments": '
                '{"strategy": {"project_info": {}, "...": "all required strategy keys"}}}\n'
                "```\n\n"
                "The required strategy top-level keys belong inside "
                "`arguments.strategy`. Return no prose and no second action."
            )
        initial = {
            "phase": request.phase,
            "project_path": request.project_path,
            "checkpoint": request.checkpoint,
            "instruction": "Return exactly one allowed JSON action.",
        }
        return {
            "agent_session_id": "agent_" + uuid.uuid4().hex,
            "phase": request.phase,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": json.dumps(initial, ensure_ascii=False),
                },
            ],
            "page_count": int(request.checkpoint.get("page_count") or 0),
            "next_page": 1,
            "completed_pages": [],
            "buffered_notes": {},
            "expected_references": list(
                request.checkpoint.get("expected_references") or []
            ),
            "read_references": [],
            "completed": False,
            "output": {},
            "provider_attempts": 0,
        }

    def run_phase(
        self,
        request: AgentPhaseRequest,
        *,
        stop_after_page: int | None = None,
    ) -> AgentPhaseResult:
        session = self.store.load_agent_session(request.job_id, request.phase)
        if session is None or (
            request.phase == "strategist_draft" and session.get("completed")
        ):
            session = self._new_session(request)
        for _turn in range(120):
            raw = self._call_with_transport_retry(session)
            action = str(raw.get("action") or "")
            arguments = raw.get("arguments")
            try:
                if action not in ALLOWED_ACTIONS or not isinstance(arguments, dict):
                    self._protocol_error(
                        session,
                        action,
                        "return exactly one allowed action and an arguments object",
                    )
                observation = self._dispatch(
                    request, session, action, arguments
                )
            except WorkflowExecutionError as exc:
                repairs = int(session.get("protocol_repairs") or 0)
                if not exc.detail.retryable or repairs >= self.max_protocol_repairs:
                    raise
                session["protocol_repairs"] = repairs + 1
                session["messages"].append(
                    {
                        "role": "assistant",
                        "content": json.dumps(raw, ensure_ascii=False),
                    }
                )
                session["messages"].append(
                    {
                        "role": "user",
                        "content": json.dumps(
                            {"action_error": exc.detail.to_dict()},
                            ensure_ascii=False,
                        ),
                    }
                )
                self.store.save_agent_session(
                    request.job_id, request.phase, session
                )
                self._log_action(
                    request, action, arguments, accepted=False,
                    error_code=exc.detail.code,
                )
                continue
            session["messages"].append(
                {
                    "role": "assistant",
                    "content": json.dumps(raw, ensure_ascii=False),
                }
            )
            session["messages"].append(
                {
                    "role": "user",
                    "content": json.dumps(
                        {"observation": observation}, ensure_ascii=False
                    ),
                }
            )
            self.store.save_agent_session(
                request.job_id, request.phase, session
            )
            self._log_action(
                request,
                action,
                arguments,
                accepted=bool(observation.get("accepted", True)),
                page=observation.get("page"),
            )
            if stop_after_page and session.get("completed_pages") == list(
                range(1, stop_after_page + 1)
            ):
                return AgentPhaseResult(
                    "paused", request.phase, self._checkpoint(session)
                )
            if session.get("completed"):
                return AgentPhaseResult(
                    "completed",
                    request.phase,
                    self._checkpoint(session),
                    session.get("output", {}),
                )
        raise RuntimeError("agent phase exceeded 120 action turns")

    def _call_with_transport_retry(
        self, session: dict[str, Any]
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(2):
            session["provider_attempts"] = int(
                session.get("provider_attempts") or 0
            ) + 1
            try:
                return self.call_messages(session["messages"])
            except Exception as exc:
                last_error = exc
                if (
                    classify_llm_failure(exc) != FailureKind.TRANSPORT
                    or attempt == 1
                ):
                    raise
        raise RuntimeError(f"provider call failed: {last_error}") from last_error

    def _checkpoint(self, session: dict[str, Any]) -> dict[str, Any]:
        return {
            "agent_session_id": session["agent_session_id"],
            "next_page": session["next_page"],
            "completed_pages": list(session["completed_pages"]),
            "read_references": list(session["read_references"]),
            "chart_receipt": (session.get("output") or {}).get(
                "chart_receipt"
            ),
        }

    def _protocol_error(
        self,
        session: dict[str, Any],
        action: str,
        required: str,
        *,
        page: int | None = None,
        field: str = "action",
    ) -> None:
        raise WorkflowExecutionError(
            WorkflowError(
                kind=ErrorKind.PROTOCOL,
                code="agent_action_invalid",
                step=session["phase"],
                message=f"Agent action {action!r} is not valid in the current state",
                location={"action": action, "page": page, "field": field},
                correction={
                    "reason": "the action violates the persisted phase checkpoint",
                    "required": required,
                    "example": self._protocol_example(session, page),
                },
                retryable=True,
                suggested_action=SuggestedAction.REPAIR,
            )
        )

    @staticmethod
    def _protocol_example(
        session: dict[str, Any], page: int | None
    ) -> dict[str, Any]:
        if session["phase"] == "strategist_draft":
            page_count = int(session.get("page_count") or 1)
            return {
                "action": "submit_strategy_draft",
                "arguments": {
                    "strategy": {
                        "confirmations": {
                            "canvas": {"format": "ppt169"},
                            "page_count": {
                                "minimum": page_count,
                                "maximum": page_count,
                                "recommended": page_count,
                            },
                            "target_audience": {
                                "description": "technical leaders"
                            },
                            "style_objective": {
                                "description": "clear technical consulting"
                            },
                            "color_scheme": {
                                "background": "#F7F8FA",
                                "primary": "#174EA6",
                                "accent": "#00A6A6",
                                "body_text": "#202124",
                            },
                            "icon_usage": {
                                "enabled": True,
                                "style": "outline",
                            },
                            "typography": {
                                "title_family": "Microsoft YaHei",
                                "body_family": "Microsoft YaHei",
                                "body_size_px": 20,
                                "formula_policy": "mixed",
                            },
                            "image_usage": {
                                "mode": "mixed",
                                "rendering": "vector-illustration",
                                "palette": "cool-corporate",
                            },
                        },
                        "...": "preserve every other strategy field unchanged",
                    }
                },
            }
        return {
            "action": "read_spec_lock",
            "arguments": {"page": page or 1},
        }

    def _dispatch(
        self,
        request: AgentPhaseRequest,
        session: dict[str, Any],
        action: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        project = Path(request.project_path).resolve()
        if action == "submit_strategy_draft":
            if request.phase != "strategist_draft" or not isinstance(
                arguments.get("strategy"), dict
            ):
                self._protocol_error(
                    session,
                    action,
                    "submit_strategy_draft with arguments.strategy in strategist_draft",
                )
            try:
                strategy = llm_runtime.normalize_strategy_action(
                    arguments["strategy"],
                    project,
                    request.checkpoint["request"],
                )
            except LLMError as exc:
                validation_error = str(exc)
                self._protocol_error(
                    session,
                    action,
                    "resubmit the complete arguments.strategy object and correct "
                    f"this validation error: {validation_error}",
                    field=(
                        "arguments.strategy.confirmations"
                        if validation_error.startswith(
                            "strategy action confirmations received "
                        )
                        else "arguments.strategy"
                    ),
                )
            session["output"]["strategy_draft"] = strategy
            session["completed"] = True
            return {
                "accepted": True,
                "strategy_digest": llm_runtime.strategy_digest(strategy),
            }

        if request.phase != "executor":
            self._protocol_error(
                session,
                action,
                "submit_strategy_draft is the only completion action for strategist_draft",
            )

        if action == "read_spec_lock":
            page = int(arguments.get("page") or 0)
            finding_pages = {
                int(match.group(1))
                for item in session.get("project_findings") or []
                if (
                    match := re.fullmatch(
                        r"slide_(\d+)\.svg", str(item.get("file") or "")
                    )
                )
            }
            chart_pages = {
                int(item["page"])
                for item in request.checkpoint.get("declared_chart_pages")
                or []
            }
            repair_pages = finding_pages | (
                chart_pages
                if len(session["completed_pages"]) == session["page_count"]
                else set()
            )
            if page != session["next_page"] and page not in repair_pages:
                self._protocol_error(
                    session,
                    action,
                    f"read_spec_lock(page={session['next_page']}) or one currently repairable page",
                    page=page,
                )
            lock_path = project / "spec_lock.md"
            lock_text = lock_path.read_text(encoding="utf-8")
            digest = "sha256:" + hashlib.sha256(
                lock_text.encode("utf-8")
            ).hexdigest()
            session["last_spec_page"] = page
            session["last_spec_digest"] = digest
            return {
                "page": page,
                "spec_digest": digest,
                "content": lock_text,
            }

        if action == "read_project_file":
            relative = PurePosixPath(str(arguments.get("path") or ""))
            if (
                not relative.parts
                or relative.is_absolute()
                or ".." in relative.parts
            ):
                self._protocol_error(
                    session,
                    action,
                    "a project-relative path without '..'",
                    field="arguments.path",
                )
            target = project.joinpath(*relative.parts).resolve()
            if project not in target.parents and target != project:
                self._protocol_error(
                    session,
                    action,
                    "a path inside the project",
                    field="arguments.path",
                )
            if not target.is_file():
                readable_paths = sorted(
                    (
                        candidate.relative_to(project).as_posix()
                        for candidate in project.rglob("*")
                        if candidate.is_file()
                        and candidate.suffix.lower()
                        in {".csv", ".json", ".md", ".rst", ".tsv", ".txt"}
                        and not any(
                            part.startswith(".")
                            for part in candidate.relative_to(project).parts
                        )
                    ),
                    key=lambda path: (
                        0 if path == "design_spec.md" else
                        1 if path.startswith("sources/") else
                        2,
                        path,
                    ),
                )
                available = readable_paths[:12]
                example_path = available[0] if available else "sources/source.md"
                available_text = ", ".join(available) or "no readable text files"
                raise WorkflowExecutionError(
                    WorkflowError(
                        kind=ErrorKind.PROTOCOL,
                        code="agent_project_file_not_found",
                        step=session["phase"],
                        message=(
                            "Agent requested a project file that does not exist: "
                            f"{relative.as_posix()}"
                        ),
                        location={
                            "action": action,
                            "page": None,
                            "field": "arguments.path",
                            "path": relative.as_posix(),
                        },
                        correction={
                            "reason": (
                                f"project-relative path {relative.as_posix()!r} "
                                "does not exist"
                            ),
                            "required": (
                                "retry read_project_file with one existing "
                                "project-relative text path; available paths: "
                                + available_text
                            ),
                            "example": {
                                "action": "read_project_file",
                                "arguments": {"path": example_path},
                            },
                        },
                        retryable=True,
                        suggested_action=SuggestedAction.REPAIR,
                    )
                )
            return {
                "path": relative.as_posix(),
                "content": target.read_text(encoding="utf-8"),
            }

        if action == "read_reference_svg":
            reference = str(arguments.get("path") or "")
            expected = set(session["expected_references"])
            if session["completed_pages"] or reference not in expected:
                self._protocol_error(
                    session,
                    action,
                    "an unread expected reference before page 1",
                    field="arguments.path",
                )
            if reference in session["read_references"]:
                self._protocol_error(
                    session,
                    action,
                    "each distinct reference exactly once",
                    field="arguments.path",
                )
            reference_path = Path(reference).expanduser().resolve()
            template_root = (
                Path(__file__).resolve().parents[1]
                / "skills"
                / "ppt-master"
                / "templates"
            ).resolve()
            if not (
                reference_path.is_relative_to(project)
                or reference_path.is_relative_to(template_root)
            ):
                self._protocol_error(
                    session,
                    action,
                    "a reference inside the project or PPT Master templates",
                    field="arguments.path",
                )
            content = reference_path.read_text(encoding="utf-8")
            if "<svg" not in content or "</svg>" not in content:
                self._protocol_error(
                    session,
                    action,
                    "a complete SVG reference",
                    field="arguments.path",
                )
            session["read_references"].append(reference)
            return {"path": reference, "content": content}

        if action == "write_svg_page":
            return self._write_svg_page(
                request, session, arguments, project
            )

        if action == "submit_chart_receipt":
            return self._submit_chart_receipt(request, session, arguments)

        if action == "report_phase_complete":
            expected_pages = list(range(1, session["page_count"] + 1))
            if session["completed_pages"] != expected_pages:
                self._protocol_error(
                    session,
                    action,
                    f"complete pages in order: {expected_pages}",
                )
            if set(session["read_references"]) != set(
                session["expected_references"]
            ):
                self._protocol_error(
                    session,
                    action,
                    "read every expected reference exactly once",
                )
            if "chart_receipt" not in session["output"]:
                self._protocol_error(
                    session, action, "submit_chart_receipt before completion"
                )
            findings = self.project_checker(str(project))
            if findings:
                session["project_findings"] = findings
                return {
                    "accepted": False,
                    "findings": findings,
                    "correction": {
                        "required": "repair only a reported page with repair_svg_page, then report completion again"
                    },
                }
            session.pop("project_findings", None)
            session["project_quality_passed"] = True
            session["completed"] = True
            return {"accepted": True, "page_count": session["page_count"]}

        if action == "repair_svg_page":
            return self._repair_svg_page(
                request, session, arguments, project
            )

        return self._dispatch_chart_action(
            request, session, action, arguments
        )

    def _write_svg_page(
        self,
        request: AgentPhaseRequest,
        session: dict[str, Any],
        arguments: dict[str, Any],
        project: Path,
    ) -> dict[str, Any]:
        action = "write_svg_page"
        page = int(arguments.get("page") or 0)
        required = (
            f"read_spec_lock(page={session['next_page']}) before "
            f"write_svg_page(page={session['next_page']})"
        )
        if page == 1 and set(session["read_references"]) != set(
            session["expected_references"]
        ):
            self._protocol_error(
                session,
                action,
                "read each expected reference exactly once before write_svg_page(page=1)",
                page=page,
            )
        if page != session["next_page"] or session.get(
            "last_spec_page"
        ) != page:
            self._protocol_error(session, action, required, page=page)
        if arguments.get("spec_digest") != session.get("last_spec_digest"):
            self._protocol_error(
                session,
                action,
                required + " with the returned current spec_digest",
                page=page,
                field="arguments.spec_digest",
            )
        svg = str(arguments.get("svg") or "").strip()
        if not svg.startswith("<svg") or svg.count("<svg") != 1:
            self._protocol_error(
                session,
                action,
                "one complete SVG document",
                page=page,
                field="arguments.svg",
            )
        path = project / "svg_output" / f"slide_{page:02d}.svg"
        self._write_text_atomic(path, svg)
        findings = self.page_checker(str(project), str(path))
        if findings:
            path.unlink(missing_ok=True)
            return {
                "accepted": False,
                "page": page,
                "findings": findings,
                "correction": {
                    "required": "repair only this page and submit write_svg_page again"
                },
            }
        session["completed_pages"].append(page)
        session["buffered_notes"][str(page)] = str(
            arguments.get("notes") or ""
        )
        session.setdefault("page_summaries", {})[str(page)] = str(
            arguments.get("summary") or ""
        )
        session["next_page"] = page + 1
        session.pop("last_spec_page", None)
        session.pop("last_spec_digest", None)
        return {
            "accepted": True,
            "page": page,
            "path": str(path),
            "next_page": session["next_page"],
        }

    def _repair_svg_page(
        self,
        request: AgentPhaseRequest,
        session: dict[str, Any],
        arguments: dict[str, Any],
        project: Path,
    ) -> dict[str, Any]:
        action = "repair_svg_page"
        page = int(arguments.get("page") or 0)
        allowed_files = {
            str(item.get("file") or "")
            for item in session.get("project_findings") or []
        }
        chart_pages = {
            int(item["page"])
            for item in request.checkpoint.get("declared_chart_pages") or []
        }
        filename = f"slide_{page:02d}.svg"
        if filename not in allowed_files:
            self._protocol_error(
                session,
                action,
                "repair a page named by the latest project findings",
                page=page,
            )
        if page in chart_pages:
            self._protocol_error(
                session,
                action,
                "use run_chart_calculator then repair_chart_page for a declared chart page",
                page=page,
            )
        if session.get("last_spec_page") != page or arguments.get(
            "spec_digest"
        ) != session.get("last_spec_digest"):
            self._protocol_error(
                session,
                action,
                f"read_spec_lock(page={page}) immediately before repair_svg_page(page={page})",
                page=page,
                field="arguments.spec_digest",
            )
        svg = str(arguments.get("svg") or "").strip()
        if not svg.startswith("<svg") or svg.count("<svg") != 1:
            self._protocol_error(
                session,
                action,
                "one complete repaired SVG",
                page=page,
                field="arguments.svg",
            )
        path = project / "svg_output" / filename
        self._write_text_atomic(path, svg)
        findings = self.page_checker(str(project), str(path))
        if findings:
            return {
                "accepted": False,
                "page": page,
                "findings": findings,
                "correction": {
                    "required": "repair only the reported findings on this page"
                },
            }
        session.pop("last_spec_page", None)
        session.pop("last_spec_digest", None)
        return {
            "accepted": True,
            "page": page,
            "path": str(path),
            "next": "report_phase_complete to rerun full-project quality",
        }

    def _submit_chart_receipt(
        self,
        request: AgentPhaseRequest,
        session: dict[str, Any],
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        action = "submit_chart_receipt"
        receipt = arguments.get("receipt")
        declared = list(
            request.checkpoint.get("declared_chart_pages") or []
        )
        expected_pages = {int(item["page"]) for item in declared}
        if declared and (
            not isinstance(receipt, list) or len(receipt) != len(declared)
        ):
            self._protocol_error(
                session,
                action,
                f"one verified receipt row for each of {len(declared)} declared chart pages",
            )
        if declared:
            if not all(
                isinstance(item, dict)
                and str(item.get("page") or "").isdigit()
                for item in receipt
            ):
                self._protocol_error(
                    session,
                    action,
                    "receipt rows with integer page and verified=true",
                )
            rows = {int(item["page"]): item for item in receipt}
            calculations = session.get("chart_calculations") or {}
            if set(rows) != expected_pages or any(
                rows[page].get("verified") is not True
                for page in expected_pages
            ):
                self._protocol_error(
                    session,
                    action,
                    f"verified rows for exact chart pages {sorted(expected_pages)}",
                )
            if any(
                str(page) not in calculations for page in expected_pages
            ):
                self._protocol_error(
                    session,
                    action,
                    "a successful calculator record for every chart page",
                )
            receipt = [
                {
                    "page": page,
                    "verified": True,
                    "calculator": calculations[str(page)],
                }
                for page in sorted(expected_pages)
            ]
        if not declared and receipt != {
            "skipped": True,
            "reason": "design spec declares no data-driven chart geometry",
        }:
            self._protocol_error(
                session, action, "the exact no-chart skipped receipt"
            )
        session["output"]["chart_receipt"] = receipt
        return {"accepted": True}

    def _dispatch_chart_action(
        self,
        request: AgentPhaseRequest,
        session: dict[str, Any],
        action: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        project = Path(request.project_path).resolve()
        declared = {
            int(item["page"]): item
            for item in request.checkpoint.get("declared_chart_pages") or []
        }
        if action == "run_chart_calculator":
            page = int(arguments.get("page") or 0)
            subcommand = str(arguments.get("subcommand") or "")
            cli_arguments = arguments.get("arguments")
            if page not in declared or subcommand not in {
                "bar",
                "line",
                "pie",
                "radar",
            }:
                self._protocol_error(
                    session,
                    action,
                    "a declared chart page and bar/line/pie/radar subcommand",
                    page=page,
                )
            if not isinstance(cli_arguments, list) or not all(
                isinstance(value, str) for value in cli_arguments
            ):
                self._protocol_error(
                    session,
                    action,
                    "arguments as a list of strings",
                    page=page,
                    field="arguments.arguments",
                )
            calculator = (
                Path(__file__).resolve().parents[1]
                / "skills"
                / "ppt-master"
                / "scripts"
                / "svg_position_calculator.py"
            )
            command = [
                sys.executable,
                str(calculator),
                "calc",
                subcommand,
                *cli_arguments,
            ]
            completed = subprocess.run(
                command,
                shell=False,
                capture_output=True,
                text=True,
                check=False,
            )
            stdout = completed.stdout[-12000:]
            stderr = completed.stderr[-4000:]
            if completed.returncode != 0:
                return {
                    "accepted": False,
                    "page": page,
                    "returncode": completed.returncode,
                    "stderr": stderr,
                    "correction": {
                        "required": "correct the calculator arguments and rerun this chart page"
                    },
                }
            digest_source = json.dumps(
                {"command": command[2:], "stdout": stdout}, sort_keys=True
            )
            record = {
                "subcommand": subcommand,
                "arguments": cli_arguments,
                "result_digest": "sha256:"
                + hashlib.sha256(digest_source.encode("utf-8")).hexdigest(),
            }
            session.setdefault("chart_calculations", {})[str(page)] = record
            session["last_chart_calc_page"] = page
            return {
                "accepted": True,
                "page": page,
                "returncode": 0,
                "stdout": stdout,
                "result_digest": record["result_digest"],
            }
        if action == "repair_chart_page":
            page = int(arguments.get("page") or 0)
            if page not in declared:
                self._protocol_error(
                    session,
                    action,
                    "repair only a design-declared chart page",
                    page=page,
                )
            if session.get("last_chart_calc_page") != page:
                self._protocol_error(
                    session,
                    action,
                    f"run_chart_calculator(page={page}) immediately before repairing this chart",
                    page=page,
                )
            if session.get("last_spec_page") != page or arguments.get(
                "spec_digest"
            ) != session.get("last_spec_digest"):
                self._protocol_error(
                    session,
                    action,
                    f"read_spec_lock(page={page}) immediately before repair_chart_page(page={page})",
                    page=page,
                    field="arguments.spec_digest",
                )
            svg = str(arguments.get("svg") or "").strip()
            if not svg.startswith("<svg") or svg.count("<svg") != 1:
                self._protocol_error(
                    session,
                    action,
                    "one complete repaired SVG",
                    page=page,
                    field="arguments.svg",
                )
            path = project / "svg_output" / f"slide_{page:02d}.svg"
            self._write_text_atomic(path, svg)
            findings = self.page_checker(str(project), str(path))
            if findings:
                return {
                    "accepted": False,
                    "page": page,
                    "findings": findings,
                    "correction": {
                        "required": "repair the reported chart page findings"
                    },
                }
            session["output"].pop("chart_receipt", None)
            session.pop("last_chart_calc_page", None)
            session.pop("last_spec_page", None)
            session.pop("last_spec_digest", None)
            return {"accepted": True, "page": page, "path": str(path)}
        self._protocol_error(
            session,
            action,
            "one allowed action for the current executor state",
        )
        raise AssertionError("unreachable")

    def materialize_notes(self, job_id: str, project_path: Path) -> Path:
        session = self.store.load_agent_session(job_id, "executor") or {}
        if not session.get("completed") or not session.get(
            "project_quality_passed"
        ):
            raise RuntimeError(
                "notes require a completed executor session and passed project quality"
            )
        buffered = session.get("buffered_notes") or {}
        expected = list(range(1, int(session["page_count"]) + 1))
        if sorted(int(key) for key in buffered) != expected:
            raise RuntimeError("buffered notes do not cover every page")
        sections = [
            f"# slide_{page:02d}\n\n{buffered[str(page)].strip()}\n"
            for page in expected
        ]
        target = project_path / "notes" / "total.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        self._write_text_atomic(target, "\n---\n\n".join(sections))
        return target

    def _log_action(
        self,
        request: AgentPhaseRequest,
        action: str,
        arguments: dict[str, Any],
        *,
        accepted: bool,
        page: Any = None,
        error_code: str | None = None,
    ) -> None:
        self.store.append_log(
            request.job_id,
            "agent action",
            phase=request.phase,
            action=action,
            page=page if page is not None else arguments.get("page"),
            accepted=accepted,
            error_code=error_code,
        )

    @staticmethod
    def _write_text_atomic(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
