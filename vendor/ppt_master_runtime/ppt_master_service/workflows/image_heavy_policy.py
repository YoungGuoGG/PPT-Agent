from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Sequence


NON_CONTENT_ROLES = {"cover", "ending", "chapter", "chapter_divider", "divider", "toc"}


@dataclass(frozen=True)
class ImageStrategyValidation:
    passed: bool
    content_pages: tuple[int, ...]
    required_raster_pages: int
    covered_pages: tuple[int, ...]
    issues: tuple[dict[str, Any], ...]


def validate_image_heavy_strategy(
    strategy: dict[str, Any],
    *,
    pages: int,
    supplied_assets: Sequence[dict[str, Any]],
) -> ImageStrategyValidation:
    content_pages = _content_pages(strategy, pages)
    required = max(1, math.ceil(len(content_pages) / 2))
    prompts = image_prompt_items(strategy)
    supplied_pages = {
        _to_int(item.get("slide_index") or item.get("page_index"))
        for item in supplied_assets
        if isinstance(item, dict)
    }
    generated_pages = {
        _to_int(item.get("slide_index") or item.get("page_index"))
        for item in prompts
    }
    content_set = set(content_pages)
    covered = tuple(sorted((supplied_pages | generated_pages) & content_set))
    issues: list[dict[str, Any]] = []

    if len(covered) < required:
        issues.append(
            _issue(
                "image.strategy.coverage",
                None,
                len(covered),
                f"at least {required} distinct content pages with raster assets; content pages are {list(content_pages)}",
            )
        )

    seen_ids: set[str] = set()
    seen_filenames: set[str] = set()
    for offset, item in enumerate(prompts, start=1):
        asset_id = str(item.get("asset_id") or "").strip()
        filename = str(item.get("filename") or "").strip()
        slide_index = _to_int(item.get("slide_index") or item.get("page_index"))
        if not asset_id:
            issues.append(_issue("image.strategy.asset_id", offset, asset_id, "a stable non-empty asset_id"))
        elif asset_id in seen_ids:
            issues.append(_issue("image.strategy.asset_id_duplicate", offset, asset_id, "a unique asset_id"))
        seen_ids.add(asset_id)
        if not filename:
            issues.append(_issue("image.strategy.filename", offset, filename, "a non-empty project-local filename"))
        elif filename in seen_filenames:
            issues.append(_issue("image.strategy.filename_duplicate", offset, filename, "a unique filename"))
        seen_filenames.add(filename)
        for field in ("prompt", "purpose", "aspect_ratio"):
            value = str(item.get(field) or "").strip()
            if not value:
                issues.append(_issue(f"image.strategy.{field}", offset, value, f"a non-empty {field}"))
        if slide_index < 1 or slide_index > pages:
            issues.append(_issue("image.strategy.target_page", offset, slide_index, f"an integer page from 1 to {pages}"))
        elif slide_index not in content_set:
            issues.append(_issue("image.strategy.target_role", offset, slide_index, f"one of the content pages {list(content_pages)}"))

    return ImageStrategyValidation(
        passed=not issues,
        content_pages=tuple(content_pages),
        required_raster_pages=required,
        covered_pages=covered,
        issues=tuple(issues),
    )


def image_prompt_items(strategy: dict[str, Any]) -> list[dict[str, Any]]:
    image = strategy.get("image")
    if not isinstance(image, dict):
        return []
    prompts = image.get("image_prompts")
    if isinstance(prompts, dict):
        prompts = prompts.get("items")
    if not isinstance(prompts, list):
        return []
    return [dict(item) for item in prompts if isinstance(item, dict)]


def required_generated_asset_ids(
    strategy: dict[str, Any],
    validation: ImageStrategyValidation,
) -> set[str]:
    content = set(validation.content_pages)
    return {
        str(item.get("asset_id"))
        for item in image_prompt_items(strategy)
        if str(item.get("asset_id") or "").strip()
        and _to_int(item.get("slide_index") or item.get("page_index")) in content
    }


def _content_pages(strategy: dict[str, Any], pages: int) -> list[int]:
    outline = strategy.get("outline")
    if not isinstance(outline, list):
        outline = []
    indexed: list[tuple[int, str]] = []
    has_explicit_role = False
    for offset in range(1, pages + 1):
        slide = outline[offset - 1] if offset <= len(outline) and isinstance(outline[offset - 1], dict) else {}
        index = _to_int(slide.get("index")) or offset
        role = str(slide.get("role") or slide.get("page_role") or "").strip().lower()
        has_explicit_role = has_explicit_role or bool(role)
        indexed.append((index, role))
    if has_explicit_role:
        return [index for index, role in indexed if role not in NON_CONTENT_ROLES]
    if pages >= 3:
        return list(range(2, pages))
    return list(range(1, pages + 1))


def _to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _issue(code: str, item: int | None, observed: Any, required: str) -> dict[str, Any]:
    return {
        "code": code,
        "item": item,
        "observed": observed,
        "required": required,
    }
