from __future__ import annotations

from dataclasses import asdict, dataclass
import os
from pathlib import Path
from typing import MutableMapping
from urllib.parse import urlsplit


ALLOWED_KEYS = {
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_MODEL",
    "QWEN_API_KEY",
    "QWEN_BASE_URL",
    "QWEN_CODEX_BASE_URL",
    "QWEN_CODEX_MODEL",
    "QWEN_MODEL",
    "PPT_MASTER_OPENAI_BASE_URL",
    "PPT_MASTER_OPENAI_MODEL",
    "IMAGE_BACKEND",
    "IMAGE_PROVIDER",
    "PPT_MASTER_LLM_TIMEOUT_SECONDS",
}


@dataclass(frozen=True)
class ProviderSettings:
    env_file: str | None
    llm_provider: str | None
    llm_model: str | None
    llm_base_url_host: str | None
    image_provider: str | None
    image_model: str | None

    def to_redacted_dict(self) -> dict[str, str | None]:
        return asdict(self)

    def to_acceptance_metadata(self) -> dict[str, str]:
        values = {
            "name": self.llm_provider,
            "model": self.llm_model,
            "base_url_host": self.llm_base_url_host,
            "image_provider": self.image_provider,
            "image_model": self.image_model,
        }
        return {key: value for key, value in values.items() if value}


def initialize_provider_environment(
    start: Path | None = None,
    environ: MutableMapping[str, str] | None = None,
) -> ProviderSettings:
    target = environ if environ is not None else os.environ
    env_file = _resolve_env_file(
        (start or Path.cwd()).expanduser(),
        target.get("PPT_MASTER_ENV_FILE"),
    )
    if env_file is not None:
        for key, value in _read_env_file(env_file).items():
            if key in ALLOWED_KEYS and key not in target:
                target[key] = value

    _copy_if_missing(target, "OPENAI_API_KEY", ("QWEN_API_KEY",))
    _copy_if_missing(
        target,
        "OPENAI_BASE_URL",
        ("PPT_MASTER_OPENAI_BASE_URL", "QWEN_CODEX_BASE_URL", "QWEN_BASE_URL"),
    )
    _copy_if_missing(
        target,
        "OPENAI_MODEL",
        ("PPT_MASTER_OPENAI_MODEL", "QWEN_CODEX_MODEL"),
    )

    parsed = urlsplit(target.get("OPENAI_BASE_URL", ""))
    return ProviderSettings(
        env_file=str(env_file) if env_file else None,
        llm_provider="openai-compatible" if target.get("OPENAI_API_KEY") else None,
        llm_model=target.get("OPENAI_MODEL"),
        llm_base_url_host=parsed.hostname,
        image_provider=target.get("IMAGE_PROVIDER") or target.get("IMAGE_BACKEND"),
        image_model=target.get("QWEN_MODEL"),
    )


def _resolve_env_file(start: Path, explicit: str | None) -> Path | None:
    if explicit:
        candidate = Path(explicit).expanduser().resolve()
        if not candidate.is_file():
            raise ValueError(f"PPT_MASTER_ENV_FILE does not exist: {candidate}")
        return candidate

    current = start.resolve()
    if current.is_file():
        current = current.parent
    for directory in (current, *current.parents):
        candidate = directory / ".env"
        if candidate.is_file():
            return candidate.resolve()
    return None


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def _copy_if_missing(
    environ: MutableMapping[str, str],
    target: str,
    sources: tuple[str, ...],
) -> None:
    if environ.get(target):
        return
    for source in sources:
        value = environ.get(source)
        if value:
            environ[target] = value.rstrip("/") if target == "OPENAI_BASE_URL" else value
            return
