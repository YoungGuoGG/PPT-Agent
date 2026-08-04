from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
import traceback
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..providers import ModelProvider
from ..skills import SkillLoader
from ..storage import AgentStore
from .repository import DesignValidationError, StyleLibrary, parse_design


class StyleImportService:
    """PPTX-to-style workflow; legacy parsers remain hidden behind this boundary."""

    def __init__(
        self,
        root: Path,
        ppt_master_root: Path,
        library: StyleLibrary,
        store: AgentStore,
        skills: SkillLoader,
        provider: ModelProvider | None,
    ) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.ppt_master_root = ppt_master_root.resolve()
        self.library = library
        self.store = store
        self.skills = skills
        self.provider = provider

    def create(self, *, source: str, name: str = "", use_model: bool = True) -> dict[str, Any]:
        source_path = Path(source).expanduser().resolve()
        if not source_path.is_file() or source_path.suffix.lower() != ".pptx":
            raise ValueError("style import requires an existing .pptx file")
        import_id = f"styleimport_{uuid4().hex}"
        payload = {
            "id": import_id,
            "status": "uploaded",
            "source_path": str(source_path),
            "source_name": source_path.name,
            "requested_name": name.strip(),
            "use_model": use_model,
            "progress": 0,
            "steps": self._steps(),
            "error": None,
        }
        self.store.save_style_import(import_id, payload)
        return payload

    def run(self, import_id: str) -> dict[str, Any]:
        payload = self.require(import_id)
        source = Path(payload["source_path"])
        draft_dir = self.root / import_id
        try:
            self._advance(import_id, "extracting", 10, "pptx.inspect_style")
            with tempfile.TemporaryDirectory(prefix="ppt-style-", dir=self.root) as temporary:
                workspace = Path(temporary)
                manifest, svg_paths = self._extract(source, workspace)
                self._advance(import_id, "analyzing", 42, "style.extract_tokens")
                evidence = self._evidence(manifest)
                analysis, mode, warning = self._analyze(
                    evidence,
                    payload.get("requested_name") or source.stem,
                    use_model=bool(payload.get("use_model", True)),
                )
                self._advance(import_id, "drafting", 62, "style.build_references")
                references = draft_dir / "references"
                references.mkdir(parents=True, exist_ok=True)
                selected = self._sample(svg_paths, limit=6)
                for index, svg in enumerate(selected, start=1):
                    self._sanitize_svg(svg, references / f"reference_{index:02d}.svg")
                self._advance(import_id, "drafting", 78, "style.author_design")
                style_id = self._unique_style_id(analysis.get("id") or analysis.get("name") or source.stem)
                design = self._render_design(style_id, analysis, evidence)
                (draft_dir / "design.md").write_text(design, encoding="utf-8")
                metadata = {
                    "slide_count": len(manifest.get("slides", [])),
                    "reference_count": len(selected),
                    "colors": evidence["colors"],
                    "fonts": evidence["fonts"],
                    "canvas": evidence["canvas"],
                    "analysis_mode": mode,
                }
                (draft_dir / "metadata.json").write_text(
                    json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            self._advance(import_id, "validating", 92, "style.validate")
            document = parse_design(draft_dir / "design.md", required_status="draft")
            result = self.store.update_style_import(
                import_id,
                status="review_ready",
                progress=100,
                current_step="review.prepare",
                draft_path=str(draft_dir),
                design=document.content,
                summary={**document.metadata, **metadata, "warning": warning},
                references=[path.name for path in sorted(references.glob("*.svg"))],
                error=None,
                steps=self._steps(completed=True),
            )
            return result
        except Exception as exc:
            detail = {
                "code": "style_import_failed",
                "message": str(exc),
                "location": {"import_id": import_id, "source": source.name},
                "correction": {
                    "action": "reimport_pptx",
                    "required": "Use a valid, unencrypted PPTX and keep the source file available until analysis completes.",
                },
                "traceback": traceback.format_exc(),
            }
            return self.store.update_style_import(import_id, status="failed", error=detail)

    def publish(self, import_id: str) -> dict[str, Any]:
        payload = self.require(import_id)
        if payload.get("status") != "review_ready":
            raise ValueError("style import is not ready for review")
        style = self.library.publish(Path(payload["draft_path"]))
        self.store.update_style_import(import_id, status="published", published_style_id=style["id"])
        return style

    def discard(self, import_id: str) -> dict[str, Any]:
        payload = self.require(import_id)
        draft_path = payload.get("draft_path")
        if draft_path:
            target = Path(draft_path).resolve()
            if target.is_relative_to(self.root) and target.is_dir():
                shutil.rmtree(target)
        return self.store.update_style_import(import_id, status="discarded")

    def require(self, import_id: str) -> dict[str, Any]:
        payload = self.store.get_style_import(import_id)
        if payload is None:
            raise KeyError(f"style import not found: {import_id}")
        return payload

    def reference_path(self, import_id: str, name: str) -> Path:
        payload = self.require(import_id)
        draft = Path(payload.get("draft_path") or self.root / import_id).resolve()
        path = (draft / "references" / Path(name).name).resolve()
        if not path.is_relative_to(draft / "references") or not path.is_file():
            raise FileNotFoundError(name)
        return path

    def _extract(self, source: Path, workspace: Path) -> tuple[dict[str, Any], list[Path]]:
        scripts = self.ppt_master_root / "skills" / "ppt-master" / "scripts"
        if str(scripts) not in sys.path:
            sys.path.insert(0, str(scripts))
        from template_import.manifest import build_manifest
        from pptx_to_svg import convert_pptx_to_svg
        from pptx_to_svg.converter import ConvertOptions

        manifest = build_manifest(source, workspace)
        result = convert_pptx_to_svg(
            source,
            workspace,
            ConvertOptions(
                media_subdir="assets",
                embed_images=True,
                keep_hidden=False,
                inheritance_mode="flat",
                asset_name_map=manifest.get("assets", {}).get("assetMap", {}),
            ),
        )
        svg_paths = [workspace / "svg" / f"slide_{item.index:02d}.svg" for item in result.slides]
        return manifest, [path for path in svg_paths if path.is_file()]

    def _analyze(self, evidence: dict[str, Any], requested_name: str, *, use_model: bool) -> tuple[dict[str, Any], str, str | None]:
        fallback = self._fallback_analysis(evidence, requested_name)
        if not use_model:
            return fallback, "deterministic", None
        if self.provider is None:
            return fallback, "deterministic", "未配置模型，已使用确定性风格提取结果。"
        request = {
            "requested_name": requested_name,
            "sanitized_style_evidence": evidence,
            "output": {
                "name": "short reusable style name",
                "description": "one sentence",
                "tags": ["searchable terms"],
                "suitable_for": ["use cases"],
                "avoid_for": ["bad use cases"],
                "positioning": "visual intent",
                "page_rhythm": "sequence and density rules",
                "layout_grammar": "geometry and composition rules",
                "charts": "chart rules",
                "image_policy": "image and icon rules",
                "do_not": ["specific prohibitions"],
            },
        }
        try:
            response = self.provider.complete_json(
                system=self.skills.style_extractor().prompt(),
                prompt=json.dumps(request, ensure_ascii=False),
            )
            merged = {**fallback}
            for key in fallback:
                value = response.get(key)
                if isinstance(fallback[key], list):
                    if isinstance(value, list) and value and all(isinstance(item, str) for item in value):
                        merged[key] = value[:12]
                elif isinstance(value, str) and value.strip():
                    merged[key] = value.strip()
            return merged, "llm", None
        except Exception as exc:
            return fallback, "deterministic_fallback", f"模型分析未完成，已回退到确定性提取：{exc}"

    @staticmethod
    def _evidence(manifest: dict[str, Any]) -> dict[str, Any]:
        theme = manifest.get("theme") or {}
        slide_size = manifest.get("slideSize") or {}
        layouts = manifest.get("layouts") or []
        used_layouts = [layout for layout in layouts if layout.get("usedBySlides")]
        geometries: list[dict[str, Any]] = []
        for layout in used_layouts[:8]:
            for placeholder in layout.get("placeholders", []):
                geometry = placeholder.get("geometry")
                if geometry:
                    geometries.append({"role": placeholder.get("type") or "content", **geometry})
        colors = list(dict.fromkeys(str(value).upper() for value in (theme.get("colors") or {}).values()))
        fonts = list(dict.fromkeys(str(value) for value in (theme.get("fonts") or {}).values() if value))
        return {
            "canvas": {
                "width": slide_size.get("width_px") or 1280,
                "height": slide_size.get("height_px") or 720,
            },
            "colors": colors[:12],
            "fonts": fonts[:6],
            "slide_count": len(manifest.get("slides") or []),
            "shape_counts": [int(slide.get("shapeCount") or 0) for slide in (manifest.get("slides") or [])],
            "layout_geometries": geometries[:30],
        }

    @staticmethod
    def _fallback_analysis(evidence: dict[str, Any], requested_name: str) -> dict[str, Any]:
        name = requested_name.strip() or "Imported Presentation Style"
        return {
            "name": name[:60],
            "description": "从用户提供的 PPTX 中提取的可复用视觉系统。",
            "tags": ["imported", "presentation", "reusable"],
            "suitable_for": ["汇报", "说明", "内部演示"],
            "avoid_for": ["需要严格品牌手册但未提供授权素材的场景"],
            "positioning": "保留原稿的层级、留白和配色关系，同时让内容保持清晰可编辑。",
            "page_rhythm": "封面建立主题；内容页交替使用聚焦页与结构页；结尾回收核心结论。",
            "layout_grammar": "沿用提取到的标题区和内容区比例，优先网格对齐与稳定边距。",
            "charts": "图表沿用主色与强调色，减少网格线，直接标注关键数据。",
            "image_policy": "图片按内容角色裁切；图标使用统一线宽；不得复用源文件中的品牌或人物素材。",
            "do_not": ["复制原 PPT 文本", "复制客户 Logo", "使用未经授权的源图片", "偏离提取到的层级比例"],
        }

    @staticmethod
    def _render_design(style_id: str, analysis: dict[str, Any], evidence: dict[str, Any]) -> str:
        colors = evidence.get("colors") or ["#1D1D1F", "#FFFFFF", "#0071E3", "#6E6E73"]
        fonts = evidence.get("fonts") or ["Microsoft YaHei", "Arial"]
        roles = ["background", "text", "accent", "muted", "support-1", "support-2"]
        color_lines = "\n".join(
            f"- `{roles[index] if index < len(roles) else f'support-{index}'}`: `{color}`"
            for index, color in enumerate(colors[:8])
        )
        prohibitions = "\n".join(f"- {item}" for item in analysis["do_not"])
        return f'''---
id: {style_id}
name: {json.dumps(analysis['name'], ensure_ascii=False)}
version: 1.0.0
status: draft
origin: imported
description: {json.dumps(analysis['description'], ensure_ascii=False)}
tags: {json.dumps(analysis['tags'], ensure_ascii=False)}
suitable_for: {json.dumps(analysis['suitable_for'], ensure_ascii=False)}
avoid_for: {json.dumps(analysis['avoid_for'], ensure_ascii=False)}
---

# {analysis['name']}

## Positioning

{analysis['positioning']}

## Color Tokens

{color_lines}

Use background/text pairs with accessible contrast. Accent colors identify hierarchy, not decoration.

## Typography

- Primary: `{fonts[0]}`
- Fallback: `{fonts[-1]}`
- Use a three-level hierarchy: display title, page title, body/annotation.
- Keep body text comfortably readable on a 16:9 canvas; shorten content before shrinking type.

## Page Rhythm

{analysis['page_rhythm']}

## Layout Grammar

{analysis['layout_grammar']}

- Canvas: {evidence['canvas']['width']} × {evidence['canvas']['height']} reference units.
- Preserve consistent outer margins and align repeated elements to shared anchors.

## Charts

{analysis['charts']}

## Image and Icon Policy

{analysis['image_policy']}

## Do Not

{prohibitions}
'''

    def _unique_style_id(self, value: str) -> str:
        base = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:48] or "imported-style"
        if len(base) < 2:
            base = f"style-{base}"
        candidate = base
        while self.library.get(candidate) is not None or (self.root / candidate).exists():
            candidate = f"{base}-{uuid4().hex[:6]}"
        return candidate

    @staticmethod
    def _sample(paths: list[Path], *, limit: int) -> list[Path]:
        if len(paths) <= limit:
            return paths
        indexes = sorted({round(index * (len(paths) - 1) / (limit - 1)) for index in range(limit)})
        return [paths[index] for index in indexes]

    @staticmethod
    def _sanitize_svg(source: Path, target: Path) -> None:
        tree = ET.parse(source)
        root = tree.getroot()
        counter = {"title": 0, "body": 0, "label": 0}
        for parent in root.iter():
            for child in list(parent):
                tag = child.tag.rsplit("}", 1)[-1].lower()
                if tag in {"script", "foreignobject", "metadata"}:
                    parent.remove(child)
                    continue
                if tag == "image":
                    child.tag = child.tag.rsplit("}", 1)[0] + "}rect" if "}" in child.tag else "rect"
                    for attribute in list(child.attrib):
                        if attribute.rsplit("}", 1)[-1] in {"href", "src"}:
                            del child.attrib[attribute]
                    child.set("fill", "#D9DDE3")
                    child.set("data-placeholder", "image")
                for attribute in list(child.attrib):
                    local = attribute.rsplit("}", 1)[-1].lower()
                    value = child.attrib[attribute].strip().lower()
                    if local.startswith("on") or (local in {"href", "src"} and value.startswith(("http:", "https:", "file:"))):
                        del child.attrib[attribute]
        for element in root.iter():
            tag = element.tag.rsplit("}", 1)[-1].lower()
            if tag not in {"text", "tspan"} or not (element.text or "").strip():
                continue
            size_match = re.search(r"(?:font-size\s*:\s*|font-size=.?)([0-9.]+)", ET.tostring(element, encoding="unicode"))
            size = float(size_match.group(1)) if size_match else 18
            role = "title" if size >= 28 else "body" if size >= 16 else "label"
            counter[role] += 1
            element.text = f"[{role.upper()} {counter[role]}]"
        target.parent.mkdir(parents=True, exist_ok=True)
        tree.write(target, encoding="utf-8", xml_declaration=True)

    def _advance(self, import_id: str, status: str, progress: int, current_step: str) -> None:
        steps = self._steps()
        order = [item["id"] for item in steps]
        active = order.index(current_step)
        for index, step in enumerate(steps):
            step["status"] = "succeeded" if index < active else "running" if index == active else "pending"
        self.store.update_style_import(
            import_id, status=status, progress=progress, current_step=current_step, steps=steps
        )

    @staticmethod
    def _steps(*, completed: bool = False) -> list[dict[str, str]]:
        names = [
            ("pptx.inspect_style", "读取 PPTX 结构"),
            ("style.extract_tokens", "提取视觉令牌"),
            ("style.build_references", "生成脱敏参考页"),
            ("style.author_design", "编写 design.md"),
            ("style.validate", "校验风格包"),
            ("review.prepare", "准备评审"),
        ]
        return [{"id": key, "title": title, "status": "succeeded" if completed else "pending"} for key, title in names]
