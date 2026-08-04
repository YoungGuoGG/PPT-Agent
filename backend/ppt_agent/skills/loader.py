from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SkillBundle:
    name: str
    instructions: str
    references: dict[str, str]

    def prompt(self) -> str:
        sections = [self.instructions.strip()]
        for name, content in self.references.items():
            sections.append(f"## Loaded reference: {name}\n\n{content.strip()}")
        return "\n\n".join(sections).strip() + "\n"


class SkillLoader:
    """Load built-in skills from the V2 resource root with explicit references."""

    DESIGN_REFERENCES = {
        "design.compose": ("design-contract.md", "layout-and-style.md"),
        "svg.generate": ("svg-authoring.md",),
        "quality.visual_review": ("quality-repair.md",),
        "quality.repair_page": ("quality-repair.md",),
        "deck.revise_design": ("design-contract.md", "quality-repair.md"),
    }

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def load(self, name: str, references: tuple[str, ...] = ()) -> SkillBundle:
        skill_root = (self.root / name).resolve()
        if not skill_root.is_relative_to(self.root) or not skill_root.is_dir():
            raise FileNotFoundError(f"unknown built-in skill: {name}")
        instructions = (skill_root / "SKILL.md").read_text(encoding="utf-8")
        loaded: dict[str, str] = {}
        for reference in references:
            path = (skill_root / "references" / reference).resolve()
            if not path.is_relative_to(skill_root / "references") or not path.is_file():
                raise FileNotFoundError(f"unknown skill reference: {name}/{reference}")
            loaded[reference] = path.read_text(encoding="utf-8")
        return SkillBundle(name, instructions, loaded)

    def planner(self, *, recovery: bool = False) -> SkillBundle:
        refs = ["plan-contract.md", "workflow-selection.md"]
        if recovery:
            refs.append("recovery-policy.md")
        return self.load("ppt-agent-planner", tuple(refs))

    def design_for(self, tool_id: str) -> SkillBundle:
        try:
            refs = self.DESIGN_REFERENCES[tool_id]
        except KeyError as exc:
            raise ValueError(f"tool does not permit ppt-design: {tool_id}") from exc
        return self.load("ppt-design", refs)

    def style_extractor(self) -> SkillBundle:
        return self.load("ppt-style-extractor", ("design-md-contract.md",))
