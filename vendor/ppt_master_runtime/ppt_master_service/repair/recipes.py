from __future__ import annotations

from .models import RepairRecipe


_STRUCTURAL_OPERATIONS = {
    "set_attribute",
    "remove_attribute",
    "insert_before",
    "insert_after",
    "remove_element",
    "replace_element",
}


def _recipe(
    code: str,
    *,
    mode: str,
    diagnosis: str,
    steps: list[str],
    operations: set[str] | None = None,
    preserve: set[str] | None = None,
    forbidden: set[str] | None = None,
    verification: list[str] | None = None,
) -> RepairRecipe:
    return RepairRecipe(
        code=code,
        mode=mode,
        diagnosis=diagnosis,
        steps=steps,
        allowed_operations=operations or set(_STRUCTURAL_OPERATIONS),
        must_preserve=preserve or {"viewBox", "visible_text", "asset_references"},
        forbidden=forbidden or {"script", "foreignObject", "mask", "filter", "network_url"},
        verification=verification or ["SVG XML parses", "the reported finding is absent"],
    )


REPAIR_RECIPES = {
    "SVG_XML_INVALID": _recipe(
        "SVG_XML_INVALID",
        mode="deterministic",
        diagnosis="The SVG is not well-formed XML.",
        steps=["Repair only the reported XML token or unclosed element."],
        operations={"replace_element", "replace_text"},
    ),
    "SVG_VIEWBOX_INVALID": _recipe(
        "SVG_VIEWBOX_INVALID",
        mode="deterministic",
        diagnosis="The SVG viewBox is missing or differs from the locked canvas.",
        steps=["Set the root viewBox to the exact canvas value from spec_lock."],
        operations={"set_attribute"},
        preserve={"visible_text", "asset_references"},
    ),
    "SVG_IMAGE_OPACITY_UNSUPPORTED": _recipe(
        "SVG_IMAGE_OPACITY_UNSUPPORTED",
        mode="deterministic_or_llm_patch",
        diagnosis="DrawingML export does not support opacity on an image element.",
        steps=[
            "Remove opacity from the located image without changing its geometry or href.",
            "For a solid background, insert a same-geometry background-color rectangle after the image.",
            "Set rectangle fill-opacity to one minus the former image opacity.",
            "Never create an SVG mask or filter.",
        ],
        operations={"remove_attribute", "insert_after"},
    ),
    "SVG_GROUP_OPACITY_UNSUPPORTED": _recipe(
        "SVG_GROUP_OPACITY_UNSUPPORTED",
        mode="deterministic",
        diagnosis="Opacity on a group is not reliably preserved by DrawingML export.",
        steps=["Remove group opacity and multiply it into visible child fill/stroke opacity."],
        operations={"remove_attribute", "set_attribute"},
    ),
    "SVG_RGBA_COLOR": _recipe(
        "SVG_RGBA_COLOR",
        mode="deterministic",
        diagnosis="RGBA paint must be represented as a solid color plus paint opacity.",
        steps=["Convert rgba to HEX and multiply alpha into fill-opacity or stroke-opacity."],
        operations={"set_attribute"},
    ),
    "SVG_MASK_OR_FILTER_UNSUPPORTED": _recipe(
        "SVG_MASK_OR_FILTER_UNSUPPORTED",
        mode="llm_patch",
        diagnosis="SVG mask and filter effects are unsupported by native export.",
        steps=["Replace the located effect with ordinary shapes or a supported clip path."],
    ),
    "ASSET_PATH_INVALID": _recipe(
        "ASSET_PATH_INVALID",
        mode="deterministic_or_human",
        diagnosis="An asset reference cannot be resolved inside the project.",
        steps=["Select an existing manifest asset and rewrite href to a project-relative path."],
        operations={"set_attribute"},
    ),
    "ICON_NAME_UNKNOWN": _recipe(
        "ICON_NAME_UNKNOWN",
        mode="llm_patch",
        diagnosis="The requested icon is not in the allowed icon manifest.",
        steps=["Replace it with an exact allowlisted icon name or remove the icon."],
        operations={"set_attribute", "remove_element"},
    ),
    "TEXT_OVERFLOW": _recipe(
        "TEXT_OVERFLOW",
        mode="llm_patch_or_regenerate",
        diagnosis="Estimated text bounds exceed the assigned content box.",
        steps=["Shorten copy before resizing text; keep the configured minimum font size."],
        operations={"replace_text", "set_attribute"},
        preserve={"viewBox", "facts", "numbers", "template_locked_elements"},
    ),
    "ELEMENT_OUT_OF_BOUNDS": _recipe(
        "ELEMENT_OUT_OF_BOUNDS",
        mode="llm_patch",
        diagnosis="The located element extends beyond the canvas or safe margin.",
        steps=["Move or resize only the located element into the declared safe area."],
        operations={"set_attribute"},
    ),
    "ELEMENT_OVERLAP": _recipe(
        "ELEMENT_OVERLAP",
        mode="llm_patch_or_regenerate",
        diagnosis="Two visible elements overlap in a way that hides content.",
        steps=["Move the secondary element while preserving alignment and reading order."],
        operations={"set_attribute"},
    ),
    "SVG_SPEC_COLOR_DRIFT": _recipe(
        "SVG_SPEC_COLOR_DRIFT",
        mode="deterministic_or_human",
        diagnosis="A color is outside the locked palette.",
        steps=["Map rare drift to the nearest approved token; escalate repeated deck-wide drift."],
        operations={"set_attribute"},
    ),
}
