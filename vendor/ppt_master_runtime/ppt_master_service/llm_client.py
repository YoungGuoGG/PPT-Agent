from __future__ import annotations

import json
import mimetypes
import os
import re
import socket
import urllib.error
import urllib.request
import base64
from enum import Enum
from pathlib import Path
from typing import Any


DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_TIMEOUT_SECONDS = 120.0


class LLMError(RuntimeError):
    pass


class FailureKind(str, Enum):
    TRANSPORT = "transport"
    PROTOCOL = "protocol"
    ARTIFACT = "artifact"
    VISUAL = "visual"


def classify_llm_failure(error: BaseException | str) -> FailureKind:
    """Classify a failed model/page operation before selecting a retry strategy."""
    message = str(error).lower()
    if any(
        token in message
        for token in (
            "timed out",
            "timeout",
            "connection reset",
            "remote end closed",
            "winerror 10054",
            "429",
            "rate limit",
            "too many requests",
        )
    ):
        return FailureKind.TRANSPORT
    if any(
        token in message
        for token in (
            "json response is not an object",
            "did not return valid json",
            "missing svg",
            "required field",
            "empty svg",
        )
    ):
        return FailureKind.PROTOCOL
    if any(token in message for token in ("overflow", "overlap", "contrast", "visual")):
        return FailureKind.VISUAL
    return FailureKind.ARTIFACT


def call_llm(prompt: str, system_prompt: str | None = None) -> str:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise LLMError("OPENAI_API_KEY is not set")
    model = os.environ.get("OPENAI_MODEL") or DEFAULT_MODEL
    base_url = (os.environ.get("OPENAI_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    payload: dict[str, Any] = {
        "model": model,
        "messages": _messages(prompt, system_prompt),
        "temperature": 0.4,
    }
    response = _post_json(f"{base_url}/chat/completions", payload, api_key)
    try:
        return str(response["choices"][0]["message"]["content"]).strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"unexpected LLM response shape: {response}") from exc


def call_llm_json(prompt: str, system_prompt: str | None = None) -> dict:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise LLMError("OPENAI_API_KEY is not set")
    model = os.environ.get("OPENAI_MODEL") or DEFAULT_MODEL
    base_url = (os.environ.get("OPENAI_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    payload: dict[str, Any] = {
        "model": model,
        "messages": _messages(prompt, system_prompt),
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }
    try:
        response = _post_json(f"{base_url}/chat/completions", payload, api_key)
        content = str(response["choices"][0]["message"]["content"]).strip()
    except (LLMError, KeyError, IndexError, TypeError) as exc:
        if "OPENAI_API_KEY is not set" in str(exc):
            raise
        # Some OpenAI-compatible providers do not support response_format.
        content = call_llm(prompt + "\n\nReturn only valid JSON.", system_prompt)
    return _parse_json_object(content)


def call_llm_messages_json(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Call an OpenAI-compatible provider without collapsing Agent history."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise LLMError("OPENAI_API_KEY is not set")
    payload: dict[str, Any] = {
        "model": os.environ.get("OPENAI_MODEL") or DEFAULT_MODEL,
        "messages": messages,
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }
    base_url = (os.environ.get("OPENAI_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    try:
        response = _post_json(f"{base_url}/chat/completions", payload, api_key)
    except LLMError as exc:
        message = str(exc).lower()
        unsupported = "response_format" in message and any(
            token in message for token in ("400", "422", "unsupported")
        )
        if not unsupported:
            raise
        fallback = dict(payload)
        fallback.pop("response_format")
        fallback["messages"] = [
            *messages,
            {"role": "user", "content": "Return one valid JSON action object only."},
        ]
        response = _post_json(
            f"{base_url}/chat/completions", fallback, api_key
        )
    try:
        content = str(response["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"unexpected LLM response shape: {response}") from exc
    return _parse_json_object(content)


def call_vision_llm_json(
    prompt: str,
    image_paths: list[str],
    system_prompt: str | None = None,
) -> dict:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise LLMError("OPENAI_API_KEY is not set")
    model = os.environ.get("OPENAI_MODEL") or DEFAULT_MODEL
    base_url = (os.environ.get("OPENAI_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    payload: dict[str, Any] = {
        "model": model,
        "messages": _vision_messages(prompt, image_paths, system_prompt),
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }
    try:
        response = _post_json(f"{base_url}/chat/completions", payload, api_key)
        content = str(response["choices"][0]["message"]["content"]).strip()
    except (LLMError, KeyError, IndexError, TypeError) as exc:
        if "OPENAI_API_KEY is not set" in str(exc):
            raise
        fallback_payload = {
            "model": model,
            "messages": _vision_messages(prompt + "\n\nReturn only valid JSON.", image_paths, system_prompt),
            "temperature": 0.2,
        }
        response = _post_json(f"{base_url}/chat/completions", fallback_payload, api_key)
        content = str(response["choices"][0]["message"]["content"]).strip()
    return _parse_json_object(content)


def _messages(prompt: str, system_prompt: str | None) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})
    return messages


def _vision_messages(prompt: str, image_paths: list[str], system_prompt: str | None) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for path in image_paths:
        content.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": _image_data_url(path),
                    "detail": "auto",
                },
            }
        )
    messages.append({"role": "user", "content": content})
    return messages


def _image_data_url(path: str) -> str:
    image_path = Path(path)
    mime = mimetypes.guess_type(image_path.name)[0] or "image/png"
    data = base64.b64encode(image_path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{data}"


def _post_json(url: str, payload: dict[str, Any], api_key: str) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    timeout = _request_timeout()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise LLMError(f"LLM HTTP error {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise LLMError(f"LLM request failed: {exc}") from exc
    except (TimeoutError, socket.timeout) as exc:
        raise LLMError(f"LLM request timed out after {timeout or 'unbounded'} seconds") from exc


def _request_timeout() -> float | None:
    raw = os.environ.get("PPT_MASTER_LLM_TIMEOUT_SECONDS")
    if raw is None or raw.strip() == "":
        return DEFAULT_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError as exc:
        raise LLMError("PPT_MASTER_LLM_TIMEOUT_SECONDS must be a number") from exc
    if value <= 0:
        return None
    return value


def _parse_json_object(content: str) -> dict:
    cleaned = content.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", cleaned, flags=re.S)
    if fence:
        cleaned = fence.group(1).strip()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            parsed = json.loads(cleaned[start : end + 1])
        else:
            raise LLMError(f"LLM did not return valid JSON: {content[:500]}") from exc
    if not isinstance(parsed, dict):
        raise LLMError("LLM JSON response is not an object")
    return parsed
