from __future__ import annotations

import json
import traceback
import os
from pathlib import Path
from typing import Any

from ..domain.models import AgentSession, DeckVersion, ReviewScope, SessionStatus, new_id
from ..planning import AgentPlanner, ToolCatalog
from ..providers import ModelProvider, OpenAICompatibleProvider
from ..skills import SkillLoader
from ..storage import AgentStore, ArtifactStore
from ..style_library import StyleImportService, StyleLibrary
from ..tools import PptMasterToolExecutor
from ..workflows import WorkflowCatalog, WorkflowExecutor


class AgentRuntime:
    """Public Agent Core API shared by Tauri, web and Order System adapters."""

    def __init__(self, *, root: Path | None = None, store: AgentStore | None = None, executor: WorkflowExecutor | None = None, provider: ModelProvider | None = None) -> None:
        resource_root = root or (Path(os.environ["PPT_AGENT_RESOURCE_ROOT"]) if os.environ.get("PPT_AGENT_RESOURCE_ROOT") else Path(__file__).resolve().parents[3])
        self.root = resource_root.resolve()
        self._load_development_env()
        effective_provider = provider
        if effective_provider is None and executor is None:
            try:
                effective_provider = OpenAICompatibleProvider()
            except RuntimeError:
                effective_provider = None
        self.data_root = Path(os.environ.get("PPT_AGENT_DATA_ROOT") or self.root / ".data").expanduser().resolve()
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.store = store or AgentStore(self.data_root / "agent.db")
        self.artifacts = ArtifactStore(self.data_root / "artifacts")
        skills_root = self.root / "skills"
        if not skills_root.is_dir():
            skills_root = Path(__file__).resolve().parents[3] / "skills"
        self.skills = SkillLoader(skills_root)
        bundled_styles = self.root / "style-library"
        if not bundled_styles.is_dir():
            bundled_styles = Path(__file__).resolve().parents[3] / "style-library"
        self.styles = StyleLibrary(
            bundled_styles,
            self.data_root / "style-library",
            self.store,
        )
        self.styles.refresh()
        self.tool_catalog = ToolCatalog()
        self.planner = AgentPlanner(
            skills=self.skills,
            catalog=self.tool_catalog,
            styles=self.styles,
            provider=effective_provider,
        )
        ppt_master_root = self.root / "vendor" / "ppt_master_runtime"
        if not ppt_master_root.is_dir():
            ppt_master_root = Path(__file__).resolve().parents[3] / "vendor" / "ppt_master_runtime"
        self.executor = executor or PptMasterToolExecutor(
            ppt_master_root,
            self.data_root,
            skills=self.skills,
            provider=effective_provider,
        )
        self.workflow_catalog = WorkflowCatalog(ppt_master_root) if (ppt_master_root / "workflows" / "registry.json").is_file() else None
        self.style_imports = StyleImportService(
            self.data_root / "style-imports",
            ppt_master_root,
            self.styles,
            self.store,
            self.skills,
            effective_provider,
        )

    def create_conversation(self, title: str = "新建演示文稿") -> dict[str, Any]:
        return self.store.create_conversation(new_id("conv"), title.strip() or "新建演示文稿")

    def submit(self, conversation_id: str, prompt: str, attachments: list[str], options: dict[str, Any] | None = None) -> dict[str, Any]:
        options = dict(options or {})
        normalized = [str(Path(path).expanduser().resolve()) for path in attachments]
        for path in normalized:
            if not Path(path).is_file():
                raise FileNotFoundError(f"attachment does not exist: {path}")
        provisional_id = new_id("session")
        plan = self.planner.create(
            provisional_id,
            prompt=prompt,
            attachments=normalized,
            options=options,
        )
        workflow_id = plan.workflow_id
        if self.workflow_catalog is not None:
            self.workflow_catalog.require_executable(workflow_id)
        message = self.store.add_message(new_id("msg"), conversation_id, "user", prompt, normalized)
        session = AgentSession(
            id=provisional_id,
            conversation_id=conversation_id,
            user_message_id=message["id"],
            status=SessionStatus.QUEUED,
            workflow_id=workflow_id,
            prompt=prompt,
            attachments=normalized,
            options=options,
            plan_id=plan.id,
        )
        self.store.save_session(session)
        self.store.save_plan(plan)
        self.store.append_event(
            session.id,
            "session_queued",
            {
                "workflow_id": workflow_id,
                "plan": plan.to_dict(),
                "assumptions": list(plan.assumptions),
            },
        )
        return self.snapshot(session.id)

    def run(self, session_id: str) -> dict[str, Any]:
        session = self.store.get_session(session_id)
        if session is None:
            raise KeyError(f"session not found: {session_id}")
        self.store.update_session(session_id, status=SessionStatus.RUNNING.value, error=None)
        self.store.append_event(session_id, "session_started", {})
        plan = self.store.get_plan_for_session(session_id) or {"steps": []}
        try:
            payload = self._execution_inputs(session)

            def emit(event: str, data: dict[str, Any]) -> None:
                self.store.append_event(session_id, event, data)
                workflow_id = str(data.get("workflow_id") or "")
                matching = next(
                    (item for item in plan["steps"] if item["workflow_id"] == workflow_id),
                    None,
                )
                if not matching:
                    return
                if event == "subworkflow_started":
                    self.store.update_plan_step(session_id, matching["id"], "running")
                    self.store.append_event(
                        session_id,
                        "step_started",
                        {
                            "step_id": matching["id"],
                            "workflow_id": workflow_id,
                            "title": matching["title"],
                        },
                    )
                elif event == "subworkflow_succeeded":
                    self.store.update_plan_step(session_id, matching["id"], "succeeded")
                    self.store.append_event(
                        session_id,
                        "step_succeeded",
                        {"step_id": matching["id"], "workflow_id": workflow_id},
                    )
                elif event == "subworkflow_failed":
                    self.store.update_plan_step(
                        session_id, matching["id"], "failed", error=data.get("error")
                    )

            execute_plan = getattr(self.executor, "execute_plan", None)
            if callable(execute_plan):
                output = execute_plan(
                    session["workflow_id"],
                    payload,
                    [step["workflow_id"] for step in plan["steps"]],
                    emit,
                )
            else:
                for step in plan["steps"]:
                    self.store.update_plan_step(session_id, step["id"], "running")
                    self.store.append_event(
                        session_id,
                        "step_started",
                        {
                            "step_id": step["id"],
                            "workflow_id": step["workflow_id"],
                            "title": step["title"],
                        },
                    )
                output = self.executor.execute(session["workflow_id"], payload, emit)
            if (self.store.get_session(session_id) or {}).get("status") == SessionStatus.CANCELLED.value:
                self.store.append_event(
                    session_id,
                    "execution_result_discarded",
                    {"reason": "session_cancelled", "workflow_id": session["workflow_id"]},
                )
                return self.snapshot(session_id)
            pptx = output.artifacts.get("pptx")
            if not pptx:
                raise RuntimeError("workflow returned no PPTX artifact")
            self._record_style_provenance(
                output.artifacts.get("project"),
                dict((self.store.get_plan_for_session(session_id) or {}).get("style_selection") or {}),
            )
            artifact_record = self.artifacts.register(session_id, "deck", pptx)
            artifact = self.store.add_artifact(new_id("artifact"), session_id, "deck", artifact_record)
            version = DeckVersion(
                id=new_id("version"), session_id=session_id, version=1,
                pptx_path=str(artifact["path"]), project_path=output.artifacts.get("project"),
                parent_id=None, scope=ReviewScope.WHOLE_DECK, pages=[], instruction="Initial end-to-end generation",
            )
            self.store.add_version(version)
            preview_artifacts = self._register_page_previews(
                session_id,
                output.artifacts.get("project"),
                version=1,
            )
            refreshed_plan = self.store.get_plan_for_session(session_id) or plan
            for step in refreshed_plan["steps"]:
                if step["status"] != "succeeded":
                    self.store.update_plan_step(session_id, step["id"], "succeeded")
                    self.store.append_event(
                        session_id,
                        "step_succeeded",
                        {"step_id": step["id"], "workflow_id": step["workflow_id"]},
                    )
            self.store.update_session(session_id, status=SessionStatus.REVIEW_READY.value)
            self.store.add_message(new_id("msg"), session["conversation_id"], "assistant", "演示文稿已生成，可以在评审区按整份或指定页面提出修改。", [])
            self.store.append_event(
                session_id,
                "review_ready",
                {
                    "version": version.to_dict(),
                    "artifact": artifact,
                    "page_previews": preview_artifacts,
                },
            )
        except Exception as exc:
            detail = getattr(exc, "detail", None) or {
                "code": "agent_execution_failed",
                "message": str(exc),
                "location": {"session_id": session_id},
                "correction": {"action": "inspect_session_events_and_retry"},
                "traceback": traceback.format_exc(),
            }
            self.store.update_session(session_id, status=SessionStatus.FAILED.value, error=detail)
            for step in plan["steps"]:
                current = self.store.get_plan_for_session(session_id) or {}
                matching = next((item for item in current.get("steps", []) if item["id"] == step["id"]), None)
                if matching and matching["status"] == "running":
                    self.store.update_plan_step(session_id, step["id"], "failed", error=detail)
            self.store.append_event(session_id, "session_failed", detail)
        return self.snapshot(session_id)

    def revise(self, session_id: str, scope: ReviewScope, instruction: str, pages: list[int]) -> dict[str, Any]:
        if not instruction.strip():
            raise ValueError("revision instruction cannot be empty")
        versions = self.store.list_versions(session_id)
        if not versions:
            raise ValueError("session has no deck version to revise")
        if scope == ReviewScope.SELECTED_PAGES and (not pages or any(page < 1 for page in pages)):
            raise ValueError("selected_pages revision requires positive page numbers")
        parent = versions[-1]
        self.store.update_session(session_id, status=SessionStatus.REVISING.value)
        self.store.append_event(
            session_id,
            "revision_started",
            {"scope": scope.value, "pages": sorted(set(pages))},
        )
        page_text = "整份演示文稿" if scope == ReviewScope.WHOLE_DECK else "第 " + ", ".join(map(str, sorted(set(pages)))) + " 页"
        prompt = f"仅修改{page_text}。修改要求：{instruction}。未指定页面必须保持不变。"
        try:
            output = self.executor.execute(
                "revision",
                {"reference_pptx": parent["pptx_path"], "source_files": [parent["pptx_path"]], "prompt": prompt, "pages": pages, "revision_scope": scope.value},
                lambda event, data: self.store.append_event(session_id, event, data),
            )
            if (self.store.get_session(session_id) or {}).get("status") == SessionStatus.CANCELLED.value:
                self.store.append_event(
                    session_id,
                    "execution_result_discarded",
                    {"reason": "session_cancelled", "workflow_id": "revision"},
                )
                return self.snapshot(session_id)
            next_version = len(versions) + 1
            record = self.artifacts.register(session_id, f"deck_v{next_version}", output.artifacts["pptx"])
            artifact = self.store.add_artifact(new_id("artifact"), session_id, "deck_revision", record)
            version = DeckVersion(new_id("version"), session_id, next_version, artifact["path"], output.artifacts.get("project"), parent["id"], scope, sorted(set(pages)), instruction)
            self.store.add_version(version)
            previews = self._register_page_previews(
                session_id,
                output.artifacts.get("project"),
                version=next_version,
            )
            self.store.update_session(session_id, status=SessionStatus.REVIEW_READY.value, error=None)
            self.store.append_event(session_id, "revision_ready", {"version": version.to_dict(), "parent_version_id": parent["id"], "page_previews": previews})
        except Exception as exc:
            detail = getattr(exc, "detail", None) or {
                "code": "revision_failed",
                "message": str(exc),
                "location": {"session_id": session_id, "scope": scope.value, "pages": pages},
                "correction": {"action": "inspect_revision_events_and_retry"},
                "traceback": traceback.format_exc(),
            }
            self.store.update_session(session_id, status=SessionStatus.REVIEW_READY.value, error=detail)
            self.store.append_event(session_id, "revision_failed", detail)
        return self.snapshot(session_id)

    def accept_version(self, session_id: str, version_id: str) -> dict[str, Any]:
        self._require_version(session_id, version_id)
        self.store.set_version_status(session_id, version_id, "accepted")
        self.store.update_session(session_id, status=SessionStatus.SUCCEEDED.value)
        self.store.append_event(session_id, "version_accepted", {"version_id": version_id})
        return self.snapshot(session_id)

    def rollback_version(self, session_id: str, version_id: str) -> dict[str, Any]:
        self._require_version(session_id, version_id)
        self.store.set_version_status(session_id, version_id, "accepted")
        self.store.update_session(session_id, status=SessionStatus.REVIEW_READY.value)
        self.store.append_event(session_id, "version_rolled_back", {"version_id": version_id})
        return self.snapshot(session_id)

    def cancel(self, session_id: str) -> dict[str, Any]:
        events = self.store.list_events(session_id)
        execution_id = next(
            (
                str(event["payload"].get("job_id"))
                for event in reversed(events)
                if event["type"] == "legacy_job_created" and event["payload"].get("job_id")
            ),
            None,
        )
        if execution_id:
            self.executor.cancel(execution_id)
        self.store.update_session(session_id, status=SessionStatus.CANCELLED.value)
        self.store.append_event(session_id, "session_cancelled", {"execution_id": execution_id})
        return self.snapshot(session_id)

    def snapshot(self, session_id: str) -> dict[str, Any]:
        session = self.store.get_session(session_id)
        if session is None:
            raise KeyError(f"session not found: {session_id}")
        return {"session": session, "plan": self.store.get_plan_for_session(session_id), "versions": self.store.list_versions(session_id), "artifacts": self.store.list_artifacts(session_id), "events": self.store.list_events(session_id)}

    def list_workflows(self) -> list[dict[str, Any]]:
        return self.workflow_catalog.list() if self.workflow_catalog is not None else []

    def create_style_import(self, source: str, name: str = "", use_model: bool = True) -> dict[str, Any]:
        return self.style_imports.create(source=source, name=name, use_model=use_model)

    def run_style_import(self, import_id: str) -> dict[str, Any]:
        return self.style_imports.run(import_id)

    def publish_style_import(self, import_id: str) -> dict[str, Any]:
        return self.style_imports.publish(import_id)

    def discard_style_import(self, import_id: str) -> dict[str, Any]:
        return self.style_imports.discard(import_id)

    def _register_page_previews(
        self,
        session_id: str,
        project_value: str | None,
        *,
        version: int,
    ) -> list[dict[str, Any]]:
        if not project_value:
            return []
        project = Path(project_value).expanduser().resolve()
        candidates: list[Path] = []
        for directory, pattern in (
            (project / ".preview", "*.png"),
            (project / "svg_final", "*.svg"),
            (project / "svg_output", "*.svg"),
        ):
            candidates = sorted(path for path in directory.glob(pattern) if path.is_file())
            if candidates:
                break
        registered: list[dict[str, Any]] = []
        for page, source in enumerate(candidates, start=1):
            role = f"page_preview_v{version}_{page:03d}"
            record = self.artifacts.register(session_id, role, source)
            registered.append(
                self.store.add_artifact(new_id("artifact"), session_id, role, record)
            )
        return registered

    def _execution_inputs(self, session: dict[str, Any]) -> dict[str, Any]:
        options = dict(session.get("options") or {})
        plan = self.store.get_plan_for_session(session["id"]) or {}
        selection = dict(plan.get("style_selection") or {})
        if not selection:
            selection = self.styles.select(
                str(session.get("prompt") or ""),
                str(options.get("style_id") or options.get("style_case_id") or "") or None,
            )
        style = self.styles.resolve(str(selection["style_id"]))
        style_context = self.styles.prompt_context(style["id"])
        requirements = "\n\n".join(item for item in [str(options.get("extra_requirements") or ""), style_context] if item)
        return {
            "source_files": list(session.get("attachments") or []),
            "topic": session.get("prompt"),
            "prompt": session.get("prompt"),
            "pages": options.get("pages"),
            "language": options.get("language") or "zh-CN",
            "style": style["name"],
            "style_id": style["id"],
            "style_design_path": style["design_path"],
            "style_design_version": style["version"],
            "style_design_hash": style["design_hash"],
            "style_reference_paths": style.get("reference_paths") or [],
            "style_contract": style_context,
            "canvas_format": options.get("canvas_format") or "ppt169",
            "template_pptx": options.get("template_pptx"),
            "brand_assets": options.get("brand_assets") or [],
            "image_assets": options.get("image_assets") or [],
            "extra_requirements": requirements or None,
            "quality_level": options.get("quality_level") or "standard",
        }

    @staticmethod
    def _record_style_provenance(project_value: str | None, selection: dict[str, Any]) -> None:
        if not project_value or not selection:
            return
        project = Path(project_value).expanduser().resolve()
        if not project.is_dir():
            return
        provenance = {
            "source_style_id": selection.get("style_id"),
            "source_style_version": selection.get("design_version"),
            "source_style_hash": selection.get("design_hash"),
            "selection_mode": selection.get("mode"),
            "selection_reason": selection.get("reason"),
        }
        design_spec = project / "design_spec.md"
        if design_spec.is_file():
            content = design_spec.read_text(encoding="utf-8-sig")
            marker = "<!-- ppt-agent-style: " + json.dumps(provenance, ensure_ascii=False) + " -->\n"
            if not content.startswith("<!-- ppt-agent-style:"):
                design_spec.write_text(marker + content, encoding="utf-8")
        spec_lock = project / "spec_lock.md"
        if spec_lock.is_file():
            content = spec_lock.read_text(encoding="utf-8-sig")
            try:
                payload = json.loads(content)
            except json.JSONDecodeError:
                if "## source_style" in content:
                    return
                block = (
                    "## source_style\n"
                    f"- id: {provenance['source_style_id']}\n"
                    f"- version: {provenance['source_style_version']}\n"
                    f"- sha256: {provenance['source_style_hash']}\n"
                    f"- selection_mode: {provenance['selection_mode']}\n"
                    f"- selection_reason: {str(provenance['selection_reason'] or '').replace(chr(10), ' ')}\n\n"
                )
                lines = content.splitlines(keepends=True)
                insert_at = 1 if lines and lines[0].lstrip().startswith("# ") else 0
                lines.insert(insert_at, "\n" + block)
                spec_lock.write_text("".join(lines), encoding="utf-8")
                return
            if isinstance(payload, dict):
                payload["source_style"] = provenance
                spec_lock.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                )

    @staticmethod
    def _route(prompt: str, attachments: list[str], options: dict[str, Any]) -> str:
        if options.get("workflow_id"):
            return str(options["workflow_id"])
        if options.get("template_pptx"):
            return "template_guided_svg" if options.get("template_strategy") == "guided_svg" else "template_fill_pptx"
        if attachments:
            return "document_to_ppt"
        return "topic_research"

    def _require_version(self, session_id: str, version_id: str) -> dict[str, Any]:
        version = next((item for item in self.store.list_versions(session_id) if item["id"] == version_id), None)
        if version is None:
            raise KeyError(f"version not found: {version_id}")
        return version

    def _load_development_env(self) -> None:
        if os.environ.get("PPT_AGENT_PACKAGED"):
            return
        for candidate in (self.root / ".env", self.root.parent / ".env"):
            if not candidate.is_file():
                continue
            for raw in candidate.read_text(encoding="utf-8-sig").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key.replace("_", "").isalnum():
                    os.environ.setdefault(key, value)
            break
