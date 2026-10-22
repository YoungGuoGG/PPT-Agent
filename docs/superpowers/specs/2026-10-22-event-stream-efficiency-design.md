# Event Stream Efficiency Design

## Goal

Reduce avoidable database work and repeated payload transfer while keeping session progress visible and recoverable after a transient connection loss.

## Current behavior

- `GET /v1/sessions/{session_id}/events` polls SQLite once per second. `list_events` filters by `session_id` and `seq`, but the schema has no matching index.
- The UI refreshes the session snapshot after selected SSE events. The snapshot serializes the complete event history even though the UI does not read its `events` field.
- The UI closes its `EventSource` on an error. The browser therefore cannot perform its normal reconnect, and the server does not use the SSE `Last-Event-ID` header to resume a stream.

## Proposed changes

1. Add an idempotent SQLite index on `events(session_id, seq)` so initial and incremental event lookups can use the same index.
2. Add an optional `include_events` query parameter to the session snapshot endpoint. It defaults to `true` to preserve the existing response contract. The UI requests `false` for routine refreshes; `AgentRuntime.snapshot` uses the same default for compatibility with existing internal callers.
3. Resume the SSE cursor from the greater of the existing `after` query parameter and a non-negative integer `Last-Event-ID` header. Invalid header values are ignored in favor of `after`.
4. Keep the UI `EventSource` stable across session status refreshes. Activate it when a new generation starts or an active session is restored; activate it again after a revision request is accepted, even though the session ID is unchanged. Track per-session cursors only from SSE events actually delivered to the UI and pass a known cursor as `after` when creating a new source. If an active session is restored without an in-memory cursor, start from sequence zero; do not infer a cursor from separately read snapshot fields. Keep the source alive after a transient error so native reconnect can occur; refresh once per disconnect and again when progress events arrive.
5. Deactivate and close the source after an operation-ending event: `review_ready`, `session_failed`, `session_cancelled`, `revision_ready`, `revision_failed`, `version_accepted`, or `version_rolled_back`.

## Scope and compatibility

- Changes are limited to the event store, session snapshot route/runtime method, and the UI's snapshot URL, event-stream lifecycle, and snapshot types.
- Existing callers that omit `include_events` continue to receive full event history.
- The event stream keeps its existing event names, payloads, and `after` query support.
- No changes to planning, prompts, generated slides, artifact formats, or persistent event contents.

## Failure handling

- A malformed `Last-Event-ID` must not fail the request; use the explicit `after` value instead.
- Reconnection resumes after the last event delivered to that browser, avoiding replay from sequence zero.
- Repeated errors during one reconnect attempt do not trigger repeated snapshot refreshes; a successful reconnect resets that guard.
- If SSE remains unavailable, the current snapshot refresh still runs once for the disconnect; reconnection remains under the browser's EventSource behavior.
- A revision request starts a new event stream for the same session from the last delivered event sequence. Ordinary snapshot status changes do not recreate the source.
- Restoring an active session without a cursor replays its event history from sequence zero rather than risking skipping an event due to a non-transactional snapshot read.

## Acceptance criteria

- Existing default snapshot responses still contain `events`; requests with `include_events=false` omit that field without changing other snapshot fields.
- The frontend `Snapshot.events` type is optional, matching both response shapes.
- Fresh and existing SQLite databases both gain the event lookup index on initialization.
- Event streams resume from a valid `Last-Event-ID`, honor a larger `after` cursor, and ignore malformed or negative resume headers.
- Status refreshes do not recreate the session's `EventSource`; transient errors do not close it, native reconnect resumes progress, and a revision explicitly reopens the stream from its prior cursor.
- A restored active session with no known cursor starts at zero; a known in-memory cursor is reused only for the matching session.
- Each listed operation-ending event closes the stream, including revision failure and cancellation.
- The UI does not request or receive the full event history during routine event-driven refreshes.

## Verification

Review the resulting diff for response compatibility and cursor edge cases. Automated tests or builds are not included in this design's implementation scope unless requested separately.
