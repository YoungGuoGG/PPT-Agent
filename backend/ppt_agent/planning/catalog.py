from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ToolDefinition:
    id: str
    title: str
    consumes: tuple[str, ...]
    produces: tuple[str, ...]
    executable: bool = True
    interactive: bool = False
    loads_design_skill: bool = False

    def summary(self) -> dict[str, object]:
        return asdict(self)


DEFAULT_TOOLS = (
    ToolDefinition("research.prepare", "研究并准备来源", (), ("research_brief",)),
    ToolDefinition("source.ingest", "读取并结构化资料", (), ("source_bundle",)),
    ToolDefinition("template.inspect", "分析演示模板", (), ("template_context",)),
    ToolDefinition("style.retrieve", "解析已选设计系统", ("source_bundle",), ("style_contract",)),
    ToolDefinition("design.compose", "生成设计包", ("source_bundle", "style_contract"), ("design_package",), loads_design_skill=True),
    ToolDefinition("assets.prepare", "准备图片公式与图表素材", ("design_package",), ("asset_manifest",)),
    ToolDefinition("svg.generate", "逐页生成 SVG", ("design_package", "style_contract", "asset_manifest"), ("svg_deck",), loads_design_skill=True),
    ToolDefinition("quality.check_repair", "检查并定向修复", ("svg_deck",), ("verified_svg_deck", "quality_report"), loads_design_skill=True),
    ToolDefinition("charts.verify", "校验图表坐标", ("verified_svg_deck",), ("chart_report",)),
    ToolDefinition("pptx.export", "导出可编辑 PPTX", ("verified_svg_deck",), ("pptx",)),
    ToolDefinition("review.prepare", "准备应用内 Review", ("pptx",), ("review_package",)),
    ToolDefinition("deck.revise", "修改整份或指定页面", ("review_request", "pptx"), ("pptx",), loads_design_skill=True),
    ToolDefinition("template.fill", "填充原生 PPTX 模板", ("source_bundle", "template_context"), ("pptx",), executable=False),
    ToolDefinition("audio.generate", "生成旁白音频", ("pptx",), ("narrated_pptx",), executable=False),
    ToolDefinition("animations.customize", "定制对象动画", ("pptx",), ("animated_pptx",), executable=False),
    ToolDefinition("template.create", "创建布局模板", (), ("template_package",), executable=False, interactive=True),
    ToolDefinition("brand.create", "创建品牌预设", (), ("brand_package",), executable=False, interactive=True),
)


class ToolCatalog:
    def __init__(self, tools: tuple[ToolDefinition, ...] = DEFAULT_TOOLS) -> None:
        self._tools = {tool.id: tool for tool in tools}

    def get(self, tool_id: str) -> ToolDefinition | None:
        return self._tools.get(tool_id)

    def require(self, tool_id: str) -> ToolDefinition:
        tool = self.get(tool_id)
        if tool is None:
            raise KeyError(f"unknown tool: {tool_id}")
        return tool

    def planner_summary(self) -> list[dict[str, object]]:
        return [tool.summary() for tool in self._tools.values() if tool.executable]

    def all(self) -> list[ToolDefinition]:
        return list(self._tools.values())
