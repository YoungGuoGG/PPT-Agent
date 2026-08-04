from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import BackgroundTasks, FastAPI, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse

from ..core import AgentRuntime
from ..domain.models import ReviewScope
from .schemas import ConversationCreate, MessageCreate, RevisionCreate, StyleImportCreate


TERMINAL = {"review_ready", "succeeded", "failed", "cancelled"}


def create_app(runtime: AgentRuntime | None = None) -> FastAPI:
    agent = runtime or AgentRuntime()
    app = FastAPI(title="PPT Agent Gateway", version="1.0.0")
    app.state.agent = agent
    auth_token = os.environ.get("PPT_AGENT_AUTH_TOKEN", "").strip()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:1420",
            "http://127.0.0.1:1420",
            "http://tauri.localhost",
            "tauri://localhost",
            "https://tauri.localhost",
        ],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def require_loopback_token(request, call_next):
        """Protect the bundled loopback API without coupling web deployments to Tauri."""
        # CORS preflight never carries the requested authentication header. Let
        # CORSMiddleware validate the origin and requested headers before the
        # browser sends the authenticated request.
        if request.method == "OPTIONS":
            return await call_next(request)
        if auth_token:
            supplied = request.headers.get("X-PPT-Agent-Token") or request.query_params.get("access_token") or ""
            if not secrets.compare_digest(supplied, auth_token):
                from fastapi.responses import JSONResponse

                return JSONResponse(status_code=401, content={"detail": "invalid PPT Agent access token"})
        return await call_next(request)

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "service": "ppt-agent-gateway", "version": "1.0.0"}

    @app.get("/v1/workflows")
    def list_workflows() -> list[dict[str, Any]]:
        return agent.list_workflows()

    @app.post("/v1/conversations")
    def create_conversation(body: ConversationCreate) -> dict[str, Any]:
        return agent.create_conversation(body.title)

    @app.get("/v1/conversations")
    def list_conversations() -> list[dict[str, Any]]:
        return agent.store.list_conversations()

    @app.get("/v1/conversations/{conversation_id}/sessions")
    def list_conversation_sessions(conversation_id: str) -> list[dict[str, Any]]:
        return agent.store.list_sessions(conversation_id)

    @app.get("/v1/conversations/{conversation_id}/messages")
    def list_messages(conversation_id: str) -> list[dict[str, Any]]:
        return agent.store.list_messages(conversation_id)

    @app.post("/v1/conversations/{conversation_id}/messages")
    def submit_message(conversation_id: str, body: MessageCreate, tasks: BackgroundTasks) -> dict[str, Any]:
        try:
            snapshot = agent.submit(conversation_id, body.prompt, body.attachments, body.options)
        except (ValueError, FileNotFoundError, KeyError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        tasks.add_task(agent.run, snapshot["session"]["id"])
        return snapshot

    @app.get("/v1/sessions/{session_id}")
    def get_session(session_id: str) -> dict[str, Any]:
        try:
            return agent.snapshot(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/v1/sessions/{session_id}/events")
    async def stream_events(session_id: str, after: int = 0) -> StreamingResponse:
        if agent.store.get_session(session_id) is None:
            raise HTTPException(status_code=404, detail="session not found")

        async def generate():
            cursor = after
            while True:
                events = agent.store.list_events(session_id, cursor)
                for event in events:
                    cursor = event["seq"]
                    yield f"id: {cursor}\nevent: {event['type']}\ndata: {json.dumps(event['payload'], ensure_ascii=False)}\n\n"
                session = agent.store.get_session(session_id) or {}
                if session.get("status") in TERMINAL and not events:
                    break
                yield ": keep-alive\n\n"
                await asyncio.sleep(1)

        return StreamingResponse(generate(), media_type="text/event-stream")

    @app.post("/v1/sessions/{session_id}/revisions")
    def revise(session_id: str, body: RevisionCreate, tasks: BackgroundTasks) -> dict[str, Any]:
        scope = ReviewScope(body.scope)
        if body.scope == "selected_pages" and not body.pages:
            raise HTTPException(status_code=422, detail="selected_pages requires pages")
        tasks.add_task(agent.revise, session_id, scope, body.instruction, body.pages)
        return {"session_id": session_id, "status": "revision_queued"}

    @app.post("/v1/sessions/{session_id}/versions/{version_id}/accept")
    def accept_version(session_id: str, version_id: str) -> dict[str, Any]:
        try:
            return agent.accept_version(session_id, version_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/v1/sessions/{session_id}/versions/{version_id}/rollback")
    def rollback_version(session_id: str, version_id: str) -> dict[str, Any]:
        try:
            return agent.rollback_version(session_id, version_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/v1/sessions/{session_id}/cancel")
    def cancel_session(session_id: str) -> dict[str, Any]:
        return agent.cancel(session_id)

    @app.get("/v1/sessions/{session_id}/artifacts/{artifact_id}")
    def download_artifact(session_id: str, artifact_id: str) -> FileResponse:
        artifact = next((item for item in agent.store.list_artifacts(session_id) if item["id"] == artifact_id), None)
        if artifact is None:
            raise HTTPException(status_code=404, detail="artifact not found")
        path = agent.artifacts.resolve(artifact["path"])
        if str(artifact.get("role") or "").startswith("page_preview_"):
            media_type = "image/svg+xml" if path.suffix.lower() == ".svg" else None
            return FileResponse(path, media_type=media_type)
        return FileResponse(path, filename=path.name)

    @app.post("/v1/uploads")
    async def upload(file: UploadFile) -> dict[str, Any]:
        safe_name = Path(file.filename or "upload.bin").name
        target = agent.data_root / "uploads" / f"{uuid4().hex}_{safe_name}"
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as stream:
            shutil.copyfileobj(file.file, stream)
        return {"path": str(target), "name": safe_name, "size": target.stat().st_size}

    @app.get("/v1/style-cases")
    def list_style_cases() -> list[dict[str, Any]]:
        return agent.styles.list()

    @app.post("/v1/style-imports")
    def create_style_import(body: StyleImportCreate, tasks: BackgroundTasks) -> dict[str, Any]:
        try:
            source = Path(body.source).expanduser().resolve()
            upload_root = (agent.data_root / "uploads").resolve()
            if not source.is_relative_to(upload_root):
                raise ValueError("style import source must come from the upload endpoint")
            payload = agent.create_style_import(str(source), body.name, body.use_model)
            tasks.add_task(agent.run_style_import, payload["id"])
            return payload
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/v1/style-imports")
    def list_style_imports() -> list[dict[str, Any]]:
        return agent.store.list_style_imports()

    @app.get("/v1/style-imports/{import_id}")
    def get_style_import(import_id: str) -> dict[str, Any]:
        try:
            return agent.style_imports.require(import_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/v1/style-imports/{import_id}/references/{name}")
    def get_style_import_reference(import_id: str, name: str) -> FileResponse:
        try:
            return FileResponse(agent.style_imports.reference_path(import_id, name), media_type="image/svg+xml")
        except (KeyError, FileNotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/v1/style-imports/{import_id}/publish")
    def publish_style_import(import_id: str) -> dict[str, Any]:
        try:
            return agent.publish_style_import(import_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (ValueError, FileExistsError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/v1/style-imports/{import_id}/discard")
    def discard_style_import(import_id: str) -> dict[str, Any]:
        try:
            return agent.discard_style_import(import_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    return app


app = create_app()
