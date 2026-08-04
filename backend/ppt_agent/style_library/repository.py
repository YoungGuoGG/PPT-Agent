from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..storage import AgentStore


REQUIRED_FIELDS = {
    "id",
    "name",
    "version",
    "status",
    "origin",
    "tags",
    "suitable_for",
    "avoid_for",
}
REQUIRED_SECTIONS = {
    "positioning",
    "color tokens",
    "typography",
    "page rhythm",
    "layout grammar",
    "charts",
    "image and icon policy",
    "do not",
}
STYLE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")


class DesignValidationError(ValueError):
    def __init__(self, issues: list[str]) -> None:
        self.issues = issues
        super().__init__("; ".join(issues))


@dataclass(frozen=True)
class DesignDocument:
    metadata: dict[str, Any]
    content: str
    path: Path

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()

    def summary(self) -> dict[str, Any]:
        references = sorted(
            str(path.resolve())
            for path in (self.path.parent / "references").glob("*.svg")
            if path.is_file()
        )
        return {
            **self.metadata,
            "design_path": str(self.path.resolve()),
            "source_path": str(self.path.parent.resolve()),
            "reference_paths": references,
            "design_hash": self.digest,
            "description": str(self.metadata.get("description") or ""),
        }


def parse_design(path: Path, *, required_status: str | None = None) -> DesignDocument:
    path = path.resolve()
    content = path.read_text(encoding="utf-8-sig")
    match = re.match(r"\A---\s*\r?\n(.*?)\r?\n---\s*\r?\n(.*)\Z", content, re.DOTALL)
    if not match:
        raise DesignValidationError(["design.md must begin with YAML-style frontmatter"])
    metadata: dict[str, Any] = {}
    issues: list[str] = []
    for number, raw_line in enumerate(match.group(1).splitlines(), start=2):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            issues.append(f"frontmatter line {number} must contain ':'")
            continue
        key, raw_value = line.split(":", 1)
        key = key.strip()
        raw_value = raw_value.strip()
        if raw_value.startswith(("[", "{", '"')):
            try:
                value = json.loads(raw_value)
            except json.JSONDecodeError:
                issues.append(f"frontmatter field {key} must contain valid JSON")
                continue
        else:
            value = raw_value
        metadata[key] = value
    missing = sorted(REQUIRED_FIELDS - metadata.keys())
    if missing:
        issues.append(f"missing frontmatter fields: {', '.join(missing)}")
    style_id = str(metadata.get("id") or "")
    if style_id and not STYLE_ID.fullmatch(style_id):
        issues.append("id must contain lowercase letters, digits, or hyphens")
    for field in ("tags", "suitable_for", "avoid_for"):
        value = metadata.get(field)
        if value is not None and (
            not isinstance(value, list) or any(not isinstance(item, str) for item in value)
        ):
            issues.append(f"{field} must be a JSON string array")
    if required_status and metadata.get("status") != required_status:
        issues.append(f"status must be {required_status}")
    headings = {
        heading.strip().lower()
        for heading in re.findall(r"^##\s+(.+?)\s*$", match.group(2), re.MULTILINE)
    }
    missing_sections = sorted(REQUIRED_SECTIONS - headings)
    if missing_sections:
        issues.append(f"missing sections: {', '.join(missing_sections)}")
    if issues:
        raise DesignValidationError(issues)
    return DesignDocument(metadata, content, path)


class StyleLibrary:
    """Canonical style catalog backed only by validated, published design.md files."""

    def __init__(self, bundled_root: Path, user_root: Path, store: AgentStore) -> None:
        self.bundled_root = bundled_root.resolve()
        self.user_root = user_root.resolve()
        self.user_root.mkdir(parents=True, exist_ok=True)
        self.store = store

    def refresh(self) -> int:
        count = 0
        for style in self._discover():
            self.store.upsert_style_case(style["id"], style)
            count += 1
        return count

    def list(self) -> list[dict[str, Any]]:
        styles = list(self._discover())
        return sorted(styles, key=lambda item: (item["origin"] != "bundled", item["name"].lower()))

    def summaries(self) -> list[dict[str, Any]]:
        return [
            {
                key: style[key]
                for key in (
                    "id",
                    "name",
                    "version",
                    "tags",
                    "suitable_for",
                    "avoid_for",
                    "description",
                )
            }
            for style in self.list()
        ]

    def get(self, style_id: str) -> dict[str, Any] | None:
        return next((style for style in self.list() if style["id"] == style_id), None)

    def resolve(self, style_id: str) -> dict[str, Any]:
        style = self.get(style_id)
        if style is None:
            raise KeyError(f"style not found or not published: {style_id}")
        return style

    def select(self, prompt: str, requested_id: str | None = None, *, reason: str = "") -> dict[str, Any]:
        if requested_id:
            style = self.resolve(requested_id)
            return self._selection(style, "user", reason or "用户明确选择了该风格")
        prompt_lower = prompt.lower()
        candidates = self.list()
        best: dict[str, Any] | None = None
        best_score = 0
        for style in candidates:
            terms = [*style.get("tags", []), *style.get("suitable_for", [])]
            score = sum(2 if str(term).lower() in prompt_lower else 0 for term in terms if term)
            score -= sum(3 if str(term).lower() in prompt_lower else 0 for term in style.get("avoid_for", []) if term)
            if score > best_score:
                best, best_score = style, score
        if best is not None:
            return self._selection(best, "planner", reason or "请求语义与风格适用场景匹配")
        fallback = next((item for item in candidates if item["id"] == "neutral-professional"), None)
        if fallback is None and candidates:
            fallback = candidates[0]
        if fallback is None:
            raise RuntimeError("style catalog contains no published design.md")
        return self._selection(fallback, "fallback", reason or "未检测到强风格信号，使用稳健通用风格")

    def prompt_context(self, style_id: str) -> str:
        style = self.resolve(style_id)
        design = Path(style["design_path"]).read_text(encoding="utf-8-sig")
        references = "\n".join(f"- {path}" for path in style.get("reference_paths", [])) or "- none"
        return (
            "# Canonical Style Contract\n\n"
            f"Source style: {style['id']}@{style['version']}\n"
            f"SHA-256: {style['design_hash']}\n\n"
            "The following design.md is authoritative for visual decisions. Preserve its tokens, "
            "layout grammar, page rhythm, and prohibitions when creating design_spec.md, spec_lock.md, "
            "and every SVG page. Adapt content to the request; never copy text from references.\n\n"
            f"{design}\n\n# Sanitized Reference SVGs\n{references}"
        )

    def publish(self, draft_dir: Path) -> dict[str, Any]:
        draft = parse_design(draft_dir / "design.md", required_status="draft")
        style_id = str(draft.metadata["id"])
        target = self.user_root / style_id
        if target.exists():
            raise FileExistsError(f"style already exists: {style_id}")
        target.mkdir(parents=True)
        published = re.sub(
            r"(?m)^status:\s*draft\s*$",
            "status: published",
            draft.content,
            count=1,
        )
        (target / "design.md").write_text(published, encoding="utf-8")
        references = draft_dir / "references"
        if references.is_dir():
            shutil.copytree(references, target / "references")
        style = parse_design(target / "design.md", required_status="published").summary()
        self.store.upsert_style_case(style_id, style)
        return style

    def _discover(self):
        seen: set[str] = set()
        for root in (self.user_root, self.bundled_root):
            if not root.is_dir():
                continue
            for design_path in sorted(root.glob("*/design.md")):
                try:
                    style = parse_design(design_path, required_status="published").summary()
                except (OSError, DesignValidationError):
                    continue
                if style["id"] in seen:
                    continue
                seen.add(style["id"])
                yield style

    @staticmethod
    def _selection(style: dict[str, Any], mode: str, reason: str) -> dict[str, Any]:
        return {
            "style_id": style["id"],
            "mode": mode,
            "reason": reason,
            "design_version": style["version"],
            "design_hash": style["design_hash"],
        }
