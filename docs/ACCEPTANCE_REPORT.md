# PPT Agent V2 Acceptance Report

## 结论

2026-08-03，PPT Agent V2 满足本轮验收目标：Windows NSIS 安装版在不依赖用户 Python 的情况下，由桌面应用启动的 V2 sidecar 使用真实模型完成真实 DOCX 到 PPTX 的端到端生成，并进入 `review_ready`。

## 最终真实验收

- 安装器：NSIS，静默安装退出码 `0`
- 安装目录：`%LOCALAPPDATA%\PPT Agent V2`
- sidecar：`ppt-agent-v2-sidecar.exe`，无控制台窗口
- 数据目录：`%LOCALAPPDATA%\OfficeAgent\PPTAgentV2`
- 模型：`qwen3.7-plus`，DashScope OpenAI-compatible endpoint
- 输入：真实 DOCX，20,729 bytes
- 请求：生成 2 页中文演示文稿
- 工作流：`document_to_ppt`
- 最终会话：`session_cc6b2d2d6373421591c596ca1ce80770`
- 状态：`review_ready`
- 版本：1
- 应用内 SVG 预览：2 页，均可按 UTF-8 解析为合法 XML
- PPTX：36,169 bytes，包含 2 个 slide XML
- PPTX SHA-256：`daccdb65c9617dc18b5a5f373bf71fac02908be659085caea50bacdce5391110`
- 验收 Artifact：`.scratch/installed-desktop-final-e2e/`

## 自动验证

- Planner/Compiler/Provider：6 tests passed
- Core/Gateway/Review/Storage：12 tests passed
- Rust runtime path：1 test passed
- React UI：TypeScript + Vite build passed
- sidecar smoke：passed
- Tauri Release：passed
- MSI：built
- NSIS：built and installed
- 安装包 sidecar 与构建 sidecar SHA-256：一致
- V2 Python 预览进程：0
- UI 源码树与旧版：一致，SHA-256 `2e56488c0217d4115921df5558ca55717a1b54d3f92d2e75f3c40867d5746318`

## 真实失败与修复记录

1. 打包后脚本把 sidecar 当 Python 再启动，导致网关端口冲突（Windows 10048）。修复为只允许 V2 包内脚本目录的受控工具分发器，并加入实际创建项目的冒烟测试。
2. 模型偶发返回多个或损坏的 JSON 对象。Provider 现在返回字符级解析位置、明确修正规则并自动重试；后续尝试使用专用 JSON 语法修复上下文，禁止 Markdown、第二对象、截断和局部补丁。
3. PowerShell 默认文本解码曾把合法中文 SVG 误判为 XML 错误；最终验收改用显式 UTF-8 与 `ElementTree` 解析。

## 尚未覆盖

- 本轮未以真实模型完整回归 Review 阶段的整份修改和指定页面修改；对应状态机与 Fake Executor 测试已通过。
- MSI 产物已构建，但实际安装验收使用的是 NSIS。
- 当前仅支持 Windows 桌面交付；Gateway 边界已为未来 Web 客户端保留。

## 2026-08-04 风格系统验收

本轮将“任意案例路径提示”替换为版本化 Style Catalog，并完成安装态 PPTX 风格导入。

- 内置风格：5 套，每套均以有效、已发布的独立 `design.md` 存在。
- Planner：计划记录 `style_id`、选择方式、原因、版本与 SHA-256。
- 设计执行：`design.compose` 和 `svg.generate` 均加载同一份完整 `design.md`。
- PPTX 导入：真实 PPTX 解析到 `review_ready`，输出 2 张脱敏 SVG 和完整 `design.md`。
- 隐私检查：参考 SVG 不包含源文本，文本改为语义占位符；图片、外链与脚本执行脱敏规则。
- Review 发布：发布前不在 Planner 目录，用户确认后立即进入目录并可明确选择。
- Release 桌面 E2E：真实模型从 `README.md` 生成 2 页 PPTX，状态 `review_ready`；随后将产物导入、评审并发布为风格。
- Release 会话：`session_25aa4e54df9248cabfe31f6b475d8a71`。
- 生成 PPTX：35,394 bytes，SHA-256 `5bf001e3d66511b1fc3ee26bb5978ae8aa46b4008fbe5cae31cc92e152c8b261`。
- 选中风格：`neutral-professional`，设计 SHA-256 `e352c07b4dbe6a20803822f824e59d6f4e4ddf43528e4fd31c635417de0d9864`。
- Release 验收 Artifact：`.scratch/style-library-release-e2e-env/`。
- NSIS：静默安装退出码 `0`；最终安装目录 sidecar ZIP 与构建产物 SHA-256 均为 `99E1711BFE6D06A86090CBE68EA06607B1E38A12384DC8D42BB7A9A073910538`。
- 安装版 UI：显示“从 PPT 导入风格”；最终安装版 sidecar 完成导入、脱敏、Review 与发布，验收后测试风格已从用户目录清理。
- Python：9 项规划/Provider/风格测试 + 15 项 Core/Gateway/Runtime 测试通过；包含 Markdown `spec_lock.md` 风格来源写入与幂等性测试。
- Rust：3 项通过；前端 TypeScript/Vite 构建通过；Skill 校验通过；sidecar smoke 通过。

第一次 release E2E 使用 Windows 凭据管理器内的旧连接配置时在 `design_commit` 遇到 WinError 10060。项目 `.env` 中的 DashScope/Qwen 配置经最小调用验证可用；增加了仅在显式设置时生效的 `PPT_AGENT_PREFER_ENV_MODEL_SETTINGS` 验收开关，未覆盖用户凭据，随后真实生成通过。安装版首次运行还会经历一次较长的 runtime 解压；验收脚本现已容忍首次 WebView 导航替换。
