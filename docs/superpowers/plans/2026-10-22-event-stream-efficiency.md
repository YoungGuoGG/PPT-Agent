# Event Stream Efficiency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce repeated event-query and snapshot work while keeping session progress reliable across reconnects and revisions.

**Architecture:** Add a composite index and optional event-history omission to the backend while preserving existing defaults. Keep the UI event stream active independently of routine session status refreshes, resume from SSE-delivered cursors, and explicitly restart it when a revision begins.

**Tech Stack:** Python 3.11+, SQLite, FastAPI, React, TypeScript, browser `EventSource`.

**Spec:** `docs/superpowers/specs/2026-10-22-event-stream-efficiency-design.md`

## Global Constraints

- `include_events` defaults to `true` so existing callers continue receiving full event history.
- The event stream keeps its existing event names, payloads, and `after` query support.
- No changes to planning, prompts, generated slides, artifact formats, or persistent event contents.
- Add no runtime dependencies.

## Review Focus

- Malformed or negative `Last-Event-ID`: ignore the header and use the explicit `after` cursor.
- `after` greater than the header cursor: start from `after`.
- Omitted `include_events` versus `include_events=false`: preserve full history for old callers and omit only the `events` field for compact snapshots.
- Restored active session with no in-memory cursor: start at sequence zero to avoid skipping an event.
- Revision with the same session ID: explicitly reactivate the stream from the last SSE-delivered sequence; ordinary status refreshes must not recreate it.

## Files and Responsibilities

- `backend/ppt_agent/storage/sqlite_store.py`: initialize the `(session_id, seq)` event index.
- `backend/ppt_agent/core/runtime.py`: optionally omit event history from snapshots while keeping the current default.
- `backend/ppt_agent/gateway/app.py`: expose the optional snapshot query and resolve the SSE resume cursor.
- `apps/ppt-agent-ui/src/types.ts`: represent event history as optional.
- `apps/ppt-agent-ui/src/api.ts`: request compact snapshots and build SSE URLs with an optional cursor.
- `apps/ppt-agent-ui/src/App.tsx`: manage stream activation, reconnect behavior, cursor tracking, and operation completion.

## Global Validation Constraint

Do not add or run automated tests or builds unless the user asks for verification. Review the source diff against every acceptance criterion above and inspect the changed control flow for compatibility and cursor races.

---

### Task 1: Index event queries and make snapshot/SSE cursors compatible

**Files:**
- Modify: `backend/ppt_agent/storage/sqlite_store.py`
- Modify: `backend/ppt_agent/core/runtime.py`
- Modify: `backend/ppt_agent/gateway/app.py`

**Interfaces:**
- `AgentRuntime.snapshot(session_id: str, *, include_events: bool = True) -> dict[str, Any]`
- `GET /v1/sessions/{session_id}?include_events=true|false`, defaulting to `true`
- `GET /v1/sessions/{session_id}/events?after=<seq>` continues to accept `Last-Event-ID`

- [x] Add `CREATE INDEX IF NOT EXISTS idx_events_session_seq ON events(session_id, seq)` to the SQLite schema initialization.
- [x] Extend `AgentRuntime.snapshot` with keyword-only `include_events: bool = True`; omit only the `events` key and avoid calling `list_events` when false.
- [x] Add `include_events: bool = True` to the session route and pass it to `AgentRuntime.snapshot`.
- [x] Read `Last-Event-ID` on the SSE endpoint; accept only an integer in SQLite's non-negative signed 64-bit range and set the starting cursor to `max(0, after, valid_header_value_or_zero)`.
- [x] Confirm by source review that old snapshot callers still get `events`, compact snapshots retain every other field, and malformed or out-of-range resume headers do not fail the request.

### Task 2: Keep UI progress streams compact and resumable

**Files:**
- Modify: `apps/ppt-agent-ui/src/types.ts`
- Modify: `apps/ppt-agent-ui/src/api.ts`
- Modify: `apps/ppt-agent-ui/src/App.tsx`

**Interfaces:**
- `Snapshot.events` becomes optional.
- `api.snapshot(sessionId)` requests `include_events=false`.
- `api.eventUrl(sessionId, after?: number)` adds `after` and preserves the access token query parameter.

- [x] Make the `Snapshot.events` property optional.
- [x] Make routine UI snapshot requests pass `include_events=false` and make `eventUrl` encode both an optional `after` cursor and the current access token.
- [x] Add UI event-stream activation state: enable it for a new generation and a restored non-terminal session, clear it when switching sessions, and reactivate it after a revision request succeeds.
- [x] Key the stream effect to the active stream session and snapshot session ID, not session status; this keeps routine refreshes from resetting EventSource's native reconnect cursor.
- [x] Track the latest numeric `MessageEvent.lastEventId` by session from listened progress and operation-ending events. Reuse that cursor when a new stream is opened; use zero when no cursor is known, including a restored session after app restart.
- [x] Keep the source open after transient errors, refresh once per reconnect attempt, and reset the refresh guard on `open`.
- [x] Listen for and close/deactivate the stream after `review_ready`, `session_failed`, `session_cancelled`, `revision_ready`, `revision_failed`, `version_accepted`, and `version_rolled_back`.
- [x] Confirm by source review that initial submission, restored active sessions, revisions, terminal events, and normal status refreshes follow the lifecycle above without requesting full event history.

## Completion Review

- Compare the diff with the spec's compatibility and failure-handling sections.
- Check for accidental changes to existing event names, payloads, and snapshot fields.
- Do not claim automated tests or builds passed; none are in scope unless the user requests verification.
