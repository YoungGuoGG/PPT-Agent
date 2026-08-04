from __future__ import annotations

from copy import deepcopy
from typing import Any
import xml.etree.ElementTree as ET

from .models import RepairRecipe


HARD_FORBIDDEN_ELEMENTS = {"script", "foreignObject", "mask", "filter", "iframe", "object", "embed"}


class PatchValidationError(ValueError):
    """Raised when a model patch exceeds the selected repair recipe."""


def apply_patch(source_svg: str, patch: dict[str, Any], recipe: RepairRecipe) -> str:
    try:
        root = ET.fromstring(source_svg)
    except ET.ParseError as exc:
        raise PatchValidationError(f"source SVG is invalid XML: {exc}") from exc

    original_view_box = root.get("viewBox")
    original_text = _visible_text(root)
    original_hrefs = _asset_hrefs(root)

    operations = patch.get("operations")
    if not isinstance(operations, list) or not operations:
        raise PatchValidationError("patch must contain a non-empty operations list")

    for operation in operations:
        if not isinstance(operation, dict):
            raise PatchValidationError("each patch operation must be an object")
        op = str(operation.get("op") or "")
        if op not in recipe.allowed_operations:
            raise PatchValidationError(f"operation {op!r} is not allowed for {recipe.code}")
        target = _find_target(root, operation.get("target"))
        _apply_operation(root, target, op, operation)

    _reject_forbidden_elements(root, recipe)
    if "viewBox" in recipe.must_preserve and root.get("viewBox") != original_view_box:
        raise PatchValidationError("patch changed the slide viewBox")
    if "visible_text" in recipe.must_preserve and _visible_text(root) != original_text:
        raise PatchValidationError("patch changed visible slide text")
    if "asset_references" in recipe.must_preserve and _asset_hrefs(root) != original_hrefs:
        raise PatchValidationError("patch changed asset references")
    return ET.tostring(root, encoding="unicode")


def _apply_operation(
    root: ET.Element,
    target: ET.Element,
    op: str,
    operation: dict[str, Any],
) -> None:
    if op == "remove_attribute":
        attribute = str(operation.get("attribute") or "")
        if not attribute:
            raise PatchValidationError("remove_attribute requires attribute")
        target.attrib.pop(attribute, None)
        return
    if op == "set_attribute":
        attribute = str(operation.get("attribute") or "")
        if not attribute:
            raise PatchValidationError("set_attribute requires attribute")
        target.set(attribute, str(operation.get("value") or ""))
        return
    if op == "replace_text":
        target.text = str(operation.get("text") or "")
        return
    if op == "remove_element":
        parent = _parent_of(root, target)
        if parent is None:
            raise PatchValidationError("cannot remove the SVG root")
        parent.remove(target)
        return

    element = _build_element(root, operation.get("element"))
    parent = _parent_of(root, target)
    if parent is None:
        raise PatchValidationError(f"{op} cannot target the SVG root")
    position = list(parent).index(target)
    if op == "insert_before":
        parent.insert(position, element)
    elif op == "insert_after":
        parent.insert(position + 1, element)
    elif op == "replace_element":
        parent.remove(target)
        parent.insert(position, element)
    else:
        raise PatchValidationError(f"unsupported patch operation: {op}")


def _find_target(root: ET.Element, target: object) -> ET.Element:
    if not isinstance(target, dict):
        raise PatchValidationError("operation target must be an object")
    element_id = str(target.get("element_id") or "")
    if element_id:
        matches = [element for element in root.iter() if element.get("id") == element_id]
    else:
        tag = str(target.get("tag") or "")
        try:
            index = int(target.get("index") or 0)
        except (TypeError, ValueError) as exc:
            raise PatchValidationError("target index must be an integer") from exc
        matches = [element for element in root.iter() if _local_name(element.tag) == tag]
        matches = [matches[index - 1]] if 1 <= index <= len(matches) else []
    if len(matches) != 1:
        raise PatchValidationError(f"patch target must resolve exactly once; resolved {len(matches)}")
    return matches[0]


def _build_element(root: ET.Element, payload: object) -> ET.Element:
    if not isinstance(payload, dict):
        raise PatchValidationError("insert/replace operation requires an element object")
    tag = str(payload.get("tag") or "")
    if not tag:
        raise PatchValidationError("element tag is required")
    if tag in HARD_FORBIDDEN_ELEMENTS:
        raise PatchValidationError(f"forbidden element <{tag}>")
    namespace = root.tag.split("}", 1)[0][1:] if root.tag.startswith("{") else ""
    qualified = f"{{{namespace}}}{tag}" if namespace else tag
    element = ET.Element(qualified)
    attributes = payload.get("attributes") or {}
    if not isinstance(attributes, dict):
        raise PatchValidationError("element attributes must be an object")
    for name, value in attributes.items():
        element.set(str(name), str(value))
    if payload.get("text") is not None:
        element.text = str(payload["text"])
    return element


def _reject_forbidden_elements(root: ET.Element, recipe: RepairRecipe) -> None:
    forbidden = HARD_FORBIDDEN_ELEMENTS | {name for name in recipe.forbidden if name != "network_url"}
    for element in root.iter():
        tag = _local_name(element.tag)
        if tag in forbidden:
            raise PatchValidationError(f"forbidden element <{tag}>")
        for value in element.attrib.values():
            if "network_url" in recipe.forbidden and str(value).lower().startswith(("http://", "https://")):
                raise PatchValidationError("patch introduced a forbidden network URL")


def _parent_of(root: ET.Element, target: ET.Element) -> ET.Element | None:
    for parent in root.iter():
        if target in list(parent):
            return parent
    return None


def _visible_text(root: ET.Element) -> str:
    return " ".join("".join(root.itertext()).split())


def _asset_hrefs(root: ET.Element) -> list[str]:
    return sorted(
        value
        for element in root.iter()
        for name, value in element.attrib.items()
        if name.rsplit("}", 1)[-1] == "href"
    )


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]
