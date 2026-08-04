from __future__ import annotations

import json
import re
import hashlib
from pathlib import Path

from .project_tools import REPO_ROOT, SCRIPT_DIR, SERVICE_DIR, run_script


TEMPLATE_ROOT = REPO_ROOT / "skills" / "ppt-master" / "templates"
METADATA_DIR = SERVICE_DIR / "template_metadata"


def import_pptx_template(pptx_path: str, output_dir: str | None = None):
    args = [str(SCRIPT_DIR / "pptx_template_import.py"), pptx_path]
    if output_dir:
        args.extend(["-o", output_dir])
    return run_script(args, timeout=600)


def scan_templates(write_metadata: bool = True) -> list[dict[str, object]]:
    templates: list[dict[str, object]] = []
    if write_metadata:
        METADATA_DIR.mkdir(parents=True, exist_ok=True)
        for old_file in METADATA_DIR.glob("auto_*.json"):
            old_file.unlink()
    for kind in ("brands", "layouts", "decks"):
        kind_dir = TEMPLATE_ROOT / kind
        if not kind_dir.exists():
            continue
        for path in sorted(item for item in kind_dir.iterdir() if item.is_dir()):
            metadata = _metadata_for_template(kind[:-1], path)
            templates.append(metadata)
            if write_metadata:
                _write_metadata(metadata)
    return templates


def _metadata_for_template(kind: str, source_path: Path) -> dict[str, object]:
    spec = source_path / "design_spec.md"
    spec_text = spec.read_text(encoding="utf-8", errors="replace") if spec.exists() else ""
    frontmatter = _parse_frontmatter(spec_text)
    template_id = str(frontmatter.get(f"{kind}_id") or frontmatter.get("template_id") or source_path.name)
    name = _extract_h1(spec_text) or source_path.name
    summary = str(frontmatter.get("summary") or f"PPT Master {kind} template")
    keywords = frontmatter.get("keywords")
    tags = keywords if isinstance(keywords, list) else []
    return {
        "template_id": f"{kind}_{template_id}",
        "name": name,
        "description": summary,
        "scenes": tags,
        "styles": tags,
        "best_for": [summary],
        "avoid_for": [],
        "source_path": str(source_path),
        "kind": kind,
        "generated_by": "ppt_master_service.template_tools.scan_templates",
    }


def _write_metadata(metadata: dict[str, object]) -> None:
    METADATA_DIR.mkdir(parents=True, exist_ok=True)
    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(metadata["template_id"]))
    digest = hashlib.sha1(str(metadata["source_path"]).encode("utf-8")).hexdigest()[:10]
    path = METADATA_DIR / f"auto_{safe_id}_{digest}.json"
    path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")


def _extract_h1(text: str) -> str | None:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return None


def _parse_frontmatter(text: str) -> dict[str, object]:
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    data: dict[str, object] = {}
    for raw_line in parts[1].splitlines():
        line = raw_line.strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if value.startswith("[") and value.endswith("]"):
            items = [
                item.strip().strip('"').strip("'")
                for item in value[1:-1].split(",")
                if item.strip()
            ]
            data[key] = items
        else:
            data[key] = value
    return data
