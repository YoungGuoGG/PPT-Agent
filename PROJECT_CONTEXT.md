# Project Context

- 当前目标：Windows-only、安装后无需 Python 的交互式 PPT Agent V2。
- 当前状态：核心框架、Planner/Design Skills、文档到 PPTX 工作流、应用内 SVG 预览、Review 版本、历史任务恢复和 Windows 下载均已实现；风格系统已重构为可版本化 `design.md` 目录，并新增 PPTX 到风格草稿的解析、脱敏、Review 与发布链。
- 最终验收：2026-08-04，新 release 桌面应用以真实模型完成 2 页 PPTX 并进入 `review_ready`，随后把该 PPTX 解析为风格草稿、生成 2 张脱敏参考页、Review 后发布；NSIS 安装版也完成相同风格导入/发布边界检查。
- UI：保持原有三栏透明毛玻璃结构，右侧新增设计风格库、Planner 选择证据和 PPTX 风格导入/Review 模态框。
- 独立安装：`%LOCALAPPDATA%\PPT Agent V2`。
- 独立数据：`%LOCALAPPDATA%\OfficeAgent\PPTAgentV2`。
- 已知约束：当前 Windows-only；PPT Master partial workflows 仍不可由 Planner 选为可执行主路由；旧迁移适配器保留在内部，不能作为新公开接口扩展。
- 风格选择：Planner 只读取已发布风格摘要，计划记录 style ID、原因、版本和 SHA-256；设计与 SVG 工具读取完整 `design.md`。
- 风格导入：用户风格保存在 `%LOCALAPPDATA%\OfficeAgent\PPTAgentV2\style-library`，未确认草稿保存在相邻 `style-imports` 目录，原文和图片不会进入参考 SVG。
- 推荐下一步：用真实模型回归导入风格后的完整 PPT 生成质量，以及 Review 阶段的整份/指定页修改；之后逐步把迁移适配器替换为原生类型化工具实现。
- 重要文档：`docs/PROJECT_PLAN.md`、`docs/ACCEPTANCE_REPORT.md`、`ARCHITECTURE.md`、`COMMANDS.md`。
