from __future__ import annotations

import json
import os
import urllib.request
from typing import Any


class ModelResponseFormatError(ValueError):
    def __init__(self, detail: dict[str, Any]) -> None:
        self.detail = detail
        super().__init__(str(detail["message"]))


class OpenAICompatibleProvider:
    """Minimal OpenAI-compatible provider; credentials stay outside UI code."""

    def __init__(self, *, api_key: str | None = None, base_url: str | None = None, model: str | None = None) -> None:
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY") or os.environ.get("QWEN_API_KEY")
        self.base_url = (
            base_url
            or os.environ.get("OPENAI_BASE_URL")
            or os.environ.get("PPT_MASTER_OPENAI_BASE_URL")
            or os.environ.get("QWEN_BASE_URL")
            or os.environ.get("QWEN_CODEX_BASE_URL")
            or "https://api.openai.com/v1"
        ).rstrip("/")
        self.model = (
            model
            or os.environ.get("OPENAI_MODEL")
            or os.environ.get("PPT_MASTER_OPENAI_MODEL")
            or os.environ.get("QWEN_CODEX_MODEL")
            or os.environ.get("QWEN_MODEL")
        )
        if not self.api_key or not self.model:
            raise RuntimeError("model provider is not configured")
        raw_timeout = os.environ.get("PPT_AGENT_LLM_TIMEOUT_SECONDS")
        if raw_timeout is None:
            raw_timeout = os.environ.get("PPT_MASTER_LLM_TIMEOUT_SECONDS", "300")
        try:
            parsed_timeout = float(raw_timeout)
        except ValueError as exc:
            raise RuntimeError("LLM timeout must be numeric") from exc
        self.timeout: float | None = None if parsed_timeout <= 0 else parsed_timeout
        try:
            configured_attempts = int(os.environ.get("PPT_AGENT_JSON_ATTEMPTS", "4"))
        except ValueError:
            configured_attempts = 4
        self.json_attempts = max(2, min(6, configured_attempts))

    def complete_messages(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        return self._complete_json_messages(messages)

    def complete_json(self, *, system: str, prompt: str, schema: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._complete_json_messages(
            [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
        )

    def review_image(self, *, image_path: str, prompt: str) -> dict[str, Any]:
        raise NotImplementedError("image review is delegated to the PPT Master visual-review adapter")

    def _complete_json_messages(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        base_messages = [dict(message) for message in messages]
        working_messages = list(base_messages)
        last_detail: dict[str, Any] | None = None
        for attempt in range(self.json_attempts):
            response = self._post(
                "/chat/completions",
                {
                    "model": self.model,
                    "messages": working_messages,
                    "response_format": {"type": "json_object"},
                },
            )
            content = response["choices"][0]["message"]["content"]
            try:
                value = json.loads(content)
                if not isinstance(value, dict):
                    raise TypeError("top-level JSON value must be an object")
                return value
            except (json.JSONDecodeError, TypeError) as exc:
                last_detail = self._json_error_detail(exc)
                if attempt == self.json_attempts - 1:
                    break
                if attempt == 0:
                    working_messages = [
                        *base_messages,
                        {"role": "assistant", "content": str(content)},
                        {
                            "role": "user",
                            "content": json.dumps(
                                {"response_error": last_detail},
                                ensure_ascii=False,
                            ),
                        },
                    ]
                else:
                    working_messages = [
                        {
                            "role": "system",
                            "content": (
                                "You are a JSON syntax repairer. Preserve the complete data and intended Agent action, "
                                "repair only syntax/serialization, and return exactly one valid top-level JSON object. "
                                "Return no Markdown and no explanation."
                            ),
                        },
                        {
                            "role": "user",
                            "content": json.dumps(
                                {
                                    "invalid_response": str(content),
                                    "parse_error": last_detail,
                                    "required": "Regenerate the entire valid JSON object; do not return a partial patch.",
                                },
                                ensure_ascii=False,
                            ),
                        },
                    ]
        raise ModelResponseFormatError(last_detail or self._json_error_detail(TypeError("missing JSON response")))

    @staticmethod
    def _json_error_detail(exc: Exception) -> dict[str, Any]:
        location: dict[str, Any] = {"field": "model_response"}
        if isinstance(exc, json.JSONDecodeError):
            location.update(
                {
                    "line": exc.lineno,
                    "column": exc.colno,
                    "character": exc.pos,
                    "parser_message": exc.msg,
                }
            )
        return {
            "code": "model_json_response_invalid",
            "message": f"Model response is not exactly one valid JSON object: {exc}",
            "location": location,
            "correction": {
                "required": "Return exactly one complete JSON object that follows the requested schema or Agent action envelope.",
                "method": (
                    "Regenerate the complete object from scratch. Inside JSON strings, escape double quotes, "
                    "backslashes, control characters, and line breaks according to the JSON standard."
                ),
                "forbidden": [
                    "Markdown code fences",
                    "explanatory prose before or after JSON",
                    "a second JSON object or action",
                    "truncated JSON",
                    "a character-level patch instead of the full object",
                ],
                "validation": "The entire response must parse with one json.loads call and have an object at the top level.",
            },
        }

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))
