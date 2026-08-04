from __future__ import annotations

from ..domain.models import ExecutionPlan, PlanStep, new_id


class WorkflowPlanner:
    """Builds composable plans while keeping provider/tool details outside the model."""

    def create(self, session_id: str, workflow_id: str) -> ExecutionPlan:
        if workflow_id in {"document_to_ppt", "topic_research", "brand_style", "image_heavy", "template_guided_svg"}:
            steps = [
                PlanStep("source", "source_ingest", "读取与结构化资料"),
                PlanStep("design", "design_commit", "生成并锁定设计文稿", depends_on=["source"]),
                PlanStep("svg", "svg_generation", "逐页生成 SVG", depends_on=["design"]),
                PlanStep("quality", "quality_repair", "质量检查与定向修复", depends_on=["svg"]),
                PlanStep("export", "pptx_export", "导出可编辑 PPTX", depends_on=["quality"]),
                PlanStep("review", "review_prepare", "生成评审版本", depends_on=["export"]),
            ]
        elif workflow_id == "revision":
            steps = [
                PlanStep("analyze", "revision_analyze", "分析当前版本与修改范围"),
                PlanStep("revise", "revision", "生成新版本", depends_on=["analyze"]),
                PlanStep("review", "review_prepare", "对比并等待确认", depends_on=["revise"]),
            ]
        else:
            steps = [PlanStep("execute", workflow_id, f"执行 {workflow_id}"), PlanStep("review", "review_prepare", "准备验收", depends_on=["execute"])]
        return ExecutionPlan(new_id("plan"), session_id, workflow_id, steps)
