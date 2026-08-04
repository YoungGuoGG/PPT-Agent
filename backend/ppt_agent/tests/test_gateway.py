from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi.testclient import TestClient

from ppt_agent.domain.models import AgentSession, SessionStatus
from ppt_agent.gateway.app import create_app
from ppt_agent.storage.sqlite_store import AgentStore


class GatewayAuthenticationTests(unittest.TestCase):
    def test_bundled_gateway_rejects_missing_token(self) -> None:
        with mock.patch.dict(os.environ, {"PPT_AGENT_AUTH_TOKEN": "test-secret"}, clear=False):
            client = TestClient(create_app(runtime=object()))

        self.assertEqual(client.get("/health").status_code, 401)
        self.assertEqual(
            client.get("/health", headers={"X-PPT-Agent-Token": "test-secret"}).status_code,
            200,
        )

    def test_event_source_can_authenticate_with_query_token(self) -> None:
        with mock.patch.dict(os.environ, {"PPT_AGENT_AUTH_TOKEN": "test-secret"}, clear=False):
            client = TestClient(create_app(runtime=object()))

        self.assertEqual(client.get("/health?access_token=test-secret").status_code, 200)

    def test_tauri_windows_cors_preflight_does_not_require_token(self) -> None:
        with mock.patch.dict(os.environ, {"PPT_AGENT_AUTH_TOKEN": "test-secret"}, clear=False):
            client = TestClient(create_app(runtime=object()))

        response = client.options(
            "/health",
            headers={
                "Origin": "http://tauri.localhost",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "x-ppt-agent-token",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers.get("access-control-allow-origin"),
            "http://tauri.localhost",
        )

    def test_web_gateway_can_run_without_desktop_token(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PPT_AGENT_AUTH_TOKEN", None)
            client = TestClient(create_app(runtime=object()))

        self.assertEqual(client.get("/health").status_code, 200)

    def test_workflow_catalog_is_exposed_through_gateway(self) -> None:
        runtime = mock.Mock()
        runtime.list_workflows.return_value = [
            {"id": "document_to_ppt", "maturity": "complete", "executable": True, "issues": []}
        ]
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PPT_AGENT_AUTH_TOKEN", None)
            client = TestClient(create_app(runtime=runtime))

        response = client.get("/v1/workflows")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]["id"], "document_to_ppt")

    def test_conversation_sessions_are_exposed_through_gateway(self) -> None:
        runtime = SimpleNamespace(
            store=SimpleNamespace(
                list_sessions=lambda conversation_id: [
                    {"id": "session-new", "conversation_id": conversation_id, "status": "review_ready"},
                    {"id": "session-old", "conversation_id": conversation_id, "status": "failed"},
                ]
            )
        )
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PPT_AGENT_AUTH_TOKEN", None)
            client = TestClient(create_app(runtime=runtime))

        response = client.get("/v1/conversations/conversation-1/sessions")

        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["id"] for item in response.json()], ["session-new", "session-old"])

    def test_store_lists_conversation_sessions_newest_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = AgentStore(Path(tmp) / "agent.db")
            store.create_conversation("conversation-1", "History")
            older = AgentSession(
                id="session-old",
                conversation_id="conversation-1",
                user_message_id="message-old",
                status=SessionStatus.FAILED,
                workflow_id="document_to_ppt",
                prompt="old",
                attachments=[],
                options={},
                plan_id="plan-old",
                created_at="2026-08-03T08:00:00+00:00",
                updated_at="2026-08-03T08:00:00+00:00",
            )
            newer = AgentSession(
                id="session-new",
                conversation_id="conversation-1",
                user_message_id="message-new",
                status=SessionStatus.REVIEW_READY,
                workflow_id="document_to_ppt",
                prompt="new",
                attachments=[],
                options={},
                plan_id="plan-new",
                created_at="2026-08-03T09:00:00+00:00",
                updated_at="2026-08-03T09:00:00+00:00",
            )
            store.save_session(older)
            store.save_session(newer)

            sessions = store.list_sessions("conversation-1")

        self.assertEqual([item["id"] for item in sessions], ["session-new", "session-old"])

    def test_svg_page_preview_is_served_inline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            svg = Path(tmp) / "slide_01.svg"
            svg.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>', encoding="utf-8")
            runtime = SimpleNamespace(
                store=SimpleNamespace(
                    list_artifacts=lambda session_id: [
                        {
                            "id": "preview-1",
                            "role": "page_preview_v1_001",
                            "path": str(svg),
                        }
                    ]
                ),
                artifacts=SimpleNamespace(resolve=lambda path: Path(path)),
            )
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("PPT_AGENT_AUTH_TOKEN", None)
                client = TestClient(create_app(runtime=runtime))

            response = client.get("/v1/sessions/session-1/artifacts/preview-1")

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["content-type"], "image/svg+xml")
            self.assertNotIn("attachment", response.headers.get("content-disposition", ""))


if __name__ == "__main__":
    unittest.main()
