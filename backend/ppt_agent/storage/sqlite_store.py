from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ..domain.models import AgentSession, DeckVersion, ExecutionPlan, SessionStatus, utc_now


SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
  attachments_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, payload_json TEXT NOT NULL, status TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plans (
  id TEXT PRIMARY KEY, session_id TEXT NOT NULL, payload_json TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, type TEXT NOT NULL,
  payload_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_session_seq ON events(session_id, seq);
CREATE TABLE IF NOT EXISTS artifacts (
  id TEXT PRIMARY KEY, session_id TEXT NOT NULL, role TEXT NOT NULL, path TEXT NOT NULL,
  sha256 TEXT NOT NULL, size INTEGER NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS deck_versions (
  id TEXT PRIMARY KEY, session_id TEXT NOT NULL, version INTEGER NOT NULL, payload_json TEXT NOT NULL,
  status TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(session_id, version)
);
CREATE TABLE IF NOT EXISTS style_cases (
  id TEXT PRIMARY KEY, payload_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS style_imports (
  id TEXT PRIMARY KEY, payload_json TEXT NOT NULL, status TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
"""


class AgentStore:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def create_conversation(self, conversation_id: str, title: str) -> dict[str, str]:
        now = utc_now()
        with self._lock, self._connect() as db:
            db.execute("INSERT INTO conversations VALUES (?, ?, ?, ?)", (conversation_id, title, now, now))
        return {"id": conversation_id, "title": title, "created_at": now, "updated_at": now}

    def list_conversations(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM conversations ORDER BY updated_at DESC").fetchall()
        return [dict(row) for row in rows]

    def add_message(self, message_id: str, conversation_id: str, role: str, content: str, attachments: list[str]) -> dict[str, Any]:
        now = utc_now()
        with self._lock, self._connect() as db:
            db.execute("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?)", (message_id, conversation_id, role, content, json.dumps(attachments, ensure_ascii=False), now))
            db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, conversation_id))
        return {"id": message_id, "conversation_id": conversation_id, "role": role, "content": content, "attachments": attachments, "created_at": now}

    def list_messages(self, conversation_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM messages WHERE conversation_id=? ORDER BY created_at", (conversation_id,)).fetchall()
        return [{**dict(row), "attachments": json.loads(row["attachments_json"])} for row in rows]

    def save_session(self, session: AgentSession) -> None:
        payload = session.to_dict()
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET payload_json=excluded.payload_json,status=excluded.status,updated_at=excluded.updated_at",
                (session.id, session.conversation_id, json.dumps(payload, ensure_ascii=False), session.status.value, session.created_at, session.updated_at),
            )

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT payload_json FROM sessions WHERE id=?", (session_id,)).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def list_sessions(self, conversation_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT payload_json FROM sessions WHERE conversation_id=? ORDER BY updated_at DESC",
                (conversation_id,),
            ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def update_session(self, session_id: str, **changes: Any) -> dict[str, Any]:
        payload = self.get_session(session_id)
        if payload is None:
            raise KeyError(f"session not found: {session_id}")
        payload.update(changes)
        payload["updated_at"] = utc_now()
        status = str(payload["status"])
        with self._lock, self._connect() as db:
            db.execute("UPDATE sessions SET payload_json=?,status=?,updated_at=? WHERE id=?", (json.dumps(payload, ensure_ascii=False), status, payload["updated_at"], session_id))
        return payload

    def save_plan(self, plan: ExecutionPlan) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO plans VALUES (?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET payload_json=excluded.payload_json,updated_at=excluded.updated_at",
                (plan.id, plan.session_id, json.dumps(plan.to_dict(), ensure_ascii=False), utc_now()),
            )

    def get_plan_for_session(self, session_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT payload_json FROM plans WHERE session_id=? ORDER BY updated_at DESC LIMIT 1", (session_id,)).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def update_plan_step(self, session_id: str, step_id: str, status: str, **fields: Any) -> dict[str, Any]:
        plan = self.get_plan_for_session(session_id)
        if plan is None:
            raise KeyError(f"plan not found for session: {session_id}")
        for step in plan["steps"]:
            if step["id"] == step_id:
                step["status"] = status
                step.update(fields)
                break
        with self._lock, self._connect() as db:
            db.execute("UPDATE plans SET payload_json=?,updated_at=? WHERE id=?", (json.dumps(plan, ensure_ascii=False), utc_now(), plan["id"]))
        return plan

    def append_event(self, session_id: str, event_type: str, payload: dict[str, Any]) -> int:
        with self._lock, self._connect() as db:
            cursor = db.execute("INSERT INTO events(session_id,type,payload_json,created_at) VALUES (?, ?, ?, ?)", (session_id, event_type, json.dumps(payload, ensure_ascii=False), utc_now()))
            return int(cursor.lastrowid)

    def list_events(self, session_id: str, after: int = 0) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM events WHERE session_id=? AND seq>? ORDER BY seq", (session_id, after)).fetchall()
        return [{"seq": row["seq"], "type": row["type"], "payload": json.loads(row["payload_json"]), "created_at": row["created_at"]} for row in rows]

    def add_artifact(self, artifact_id: str, session_id: str, role: str, record: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        with self._lock, self._connect() as db:
            db.execute("INSERT INTO artifacts VALUES (?, ?, ?, ?, ?, ?, ?)", (artifact_id, session_id, role, record["path"], record["sha256"], record["size"], now))
        return {"id": artifact_id, "session_id": session_id, "role": role, **record, "created_at": now}

    def list_artifacts(self, session_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM artifacts WHERE session_id=? ORDER BY created_at", (session_id,)).fetchall()]

    def add_version(self, version: DeckVersion) -> None:
        with self._lock, self._connect() as db:
            db.execute("INSERT INTO deck_versions VALUES (?, ?, ?, ?, ?, ?)", (version.id, version.session_id, version.version, json.dumps(version.to_dict(), ensure_ascii=False), version.status, version.created_at))

    def list_versions(self, session_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT payload_json FROM deck_versions WHERE session_id=? ORDER BY version", (session_id,)).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def set_version_status(self, session_id: str, version_id: str, status: str) -> None:
        versions = self.list_versions(session_id)
        with self._lock, self._connect() as db:
            for version in versions:
                next_status = status if version["id"] == version_id else ("superseded" if status == "accepted" and version.get("status") == "accepted" else version.get("status", "candidate"))
                version["status"] = next_status
                db.execute("UPDATE deck_versions SET payload_json=?,status=? WHERE id=?", (json.dumps(version, ensure_ascii=False), next_status, version["id"]))

    def upsert_style_case(self, case_id: str, payload: dict[str, Any]) -> None:
        now = utc_now()
        with self._lock, self._connect() as db:
            db.execute("INSERT INTO style_cases VALUES (?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET payload_json=excluded.payload_json,updated_at=excluded.updated_at", (case_id, json.dumps(payload, ensure_ascii=False), now, now))

    def list_style_cases(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT payload_json FROM style_cases ORDER BY updated_at DESC").fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def save_style_import(self, import_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        value = {**payload, "id": import_id, "created_at": payload.get("created_at") or now, "updated_at": now}
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO style_imports VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET payload_json=excluded.payload_json,status=excluded.status,updated_at=excluded.updated_at",
                (import_id, json.dumps(value, ensure_ascii=False), str(value["status"]), value["created_at"], now),
            )
        return value

    def get_style_import(self, import_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT payload_json FROM style_imports WHERE id=?", (import_id,)).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def update_style_import(self, import_id: str, **changes: Any) -> dict[str, Any]:
        payload = self.get_style_import(import_id)
        if payload is None:
            raise KeyError(f"style import not found: {import_id}")
        payload.update(changes)
        return self.save_style_import(import_id, payload)

    def list_style_imports(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT payload_json FROM style_imports ORDER BY updated_at DESC").fetchall()
        return [json.loads(row["payload_json"]) for row in rows]
