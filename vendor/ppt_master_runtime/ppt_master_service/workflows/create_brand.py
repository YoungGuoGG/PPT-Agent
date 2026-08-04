from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

from .scripted import REPO_ROOT, ScriptedWorkflow


HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")


class CreateBrandWorkflow(ScriptedWorkflow):
    name = "create_brand"
    is_stub = False

    def prepare(self, job: dict[str, Any]) -> dict[str, Any]:
        request = job.get("request") or {}
        brand_id = str(request.get("brand_id") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{1,63}", brand_id):
            self.fail(
                step="analyze",
                code="brand_id_invalid",
                message="brand_id must be a 2-64 character filesystem-safe slug",
                location={"field": "brand_id", "value": brand_id},
                correction={"action": "use_ascii_slug", "example": "acme-corporate"},
            )
        target = REPO_ROOT / "skills" / "ppt-master" / "templates" / "brands" / brand_id
        if target.exists() and not request.get("replace"):
            self.fail(
                step="analyze",
                code="brand_exists",
                message=f"brand already exists: {brand_id}",
                location={"path": str(target)},
                correction={"action": "set_replace_true_or_choose_new_id"},
            )
        primary = str(request.get("primary_color") or "#0066CC")
        if not HEX.fullmatch(primary):
            self.fail(
                step="analyze",
                code="brand_color_invalid",
                message="primary_color must use #RRGGBB",
                location={"field": "primary_color", "value": primary},
                correction={"action": "provide_hex_color", "example": "#0066CC"},
            )
        assumptions = {
            "brand_id": brand_id,
            "display_name": str(request.get("display_name") or brand_id.replace("-", " ").title()),
            "primary_color": primary.upper(),
            "secondary_color": str(request.get("secondary_color") or "#1D1D1F").upper(),
            "accent_color": str(request.get("accent_color") or primary).upper(),
            "title_font": str(request.get("title_font") or "Arial"),
            "body_font": str(request.get("body_font") or "Arial"),
            "tone": str(request.get("tone") or "neutral"),
            "provenance": "user" if request.get("primary_color") else "agent-default",
            "target": str(target),
        }
        path = self.write_json(Path(job["workspace"]) / "analysis" / "brand_commit.json", assumptions)
        return {"ok": True, "stage": "prepare", "design_commit": str(path), **assumptions}

    def run(self, job: dict[str, Any]) -> dict[str, Any]:
        commit = __import__("json").loads((Path(job["workspace"]) / "analysis" / "brand_commit.json").read_text(encoding="utf-8"))
        target = Path(commit["target"])
        target.mkdir(parents=True, exist_ok=True)
        spec = target / "design_spec.md"
        spec.write_text(self._spec(commit), encoding="utf-8")
        copied = []
        for source in (job.get("request") or {}).get("brand_assets") or []:
            path = Path(source).expanduser().resolve()
            if not path.exists():
                self.fail(step="materialize", code="brand_asset_missing", message=f"brand asset does not exist: {path}", location={"path": str(path)}, correction={"action": "remove_or_replace_missing_asset"})
            if path.is_file():
                destination = target / ("logo" + path.suffix.lower() if "logo" in path.stem.lower() else path.name)
                shutil.copy2(path, destination)
                copied.append(destination.name)
        return {"ok": True, "stage": "run", "brand_package": str(target), "files": ["design_spec.md", *copied]}

    def review(self, job: dict[str, Any]) -> dict[str, Any]:
        commit = __import__("json").loads((Path(job["workspace"]) / "analysis" / "brand_commit.json").read_text(encoding="utf-8"))
        spec = Path(commit["target"]) / "design_spec.md"
        text = spec.read_text(encoding="utf-8")
        required = ["## II. Color Scheme", "## III. Typography", "## V. Voice & Tone"]
        missing = [section for section in required if section not in text]
        if missing:
            self.fail(step="validate", code="brand_spec_invalid", message="brand specification is incomplete", location={"missing_sections": missing}, correction={"action": "restore_brand_sections", "sections": missing}, retryable=True)
        return {"ok": True, "stage": "review", "passed": True, "brand_validation_report": {"missing_sections": []}}

    def export(self, job: dict[str, Any]) -> dict[str, Any]:
        commit = __import__("json").loads((Path(job["workspace"]) / "analysis" / "brand_commit.json").read_text(encoding="utf-8"))
        self.run_script(job, "register_template.py", commit["brand_id"], "--kind", "brand", step="register")
        return {"ok": True, "stage": "export", "output_file": str(Path(commit["target"]) / "design_spec.md")}

    @staticmethod
    def _spec(c: dict[str, Any]) -> str:
        return f'''---
brand_id: {c["brand_id"]}
kind: brand
summary: "{c["display_name"]} presentation identity"
keywords: [brand, identity, reusable]
primary_color: "{c["primary_color"]}"
---

# {c["display_name"]} Brand Specification

> Identity-only preset. Layout remains free. Defaults were committed internally and can be changed during review.

## I. Brand Overview
| Property | Value |
|---|---|
| Brand Name | {c["display_name"]} |
| Tone | {c["tone"]} |

## II. Color Scheme
| Role | HEX | Provenance |
|---|---|---|
| primary | {c["primary_color"]} | {c["provenance"]} |
| secondary | {c["secondary_color"]} | {c["provenance"]} |
| accent | {c["accent_color"]} | {c["provenance"]} |
| text | #1D1D1F | agent-default |
| bg | #FFFFFF | agent-default |

## III. Typography
| Role | Family | Weight |
|---|---|---|
| title | {c["title_font"]} | 600 |
| body | {c["body_font"]} | 400 |

## IV. Logo
- Usage: cover-only

## V. Voice & Tone
- Formality: {c["tone"]}
- Person: we
- Emoji: forbidden
- Abbreviations: spell-out-first

## VI. Icon Style
- Preference: linear
'''
