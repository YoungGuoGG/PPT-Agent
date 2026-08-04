# PPT Agent V2 中文使用指南

## 1. 产品说明

PPT Agent V2 是一款 Windows 桌面演示文稿智能体。它可以读取 PDF、Word、现有 PPT、Markdown 或纯文本资料，根据你的要求自动规划内容和视觉风格，连续完成页面设计、SVG 生成、质量检查和 PPTX 导出，并在初稿完成后进入统一评审。

主要特点：

- 生成期间不反复要求确认，完成初稿后再集中评审。
- 输出为可编辑的 `.pptx`，并提供应用内逐页预览。
- 可以修改整份演示文稿，也可以只修改指定页面。
- 每次修改都会形成新版本，可以采用候选版本或回滚。
- 可以自动选择内置风格，也可以从现有 PPTX 提取自己的设计风格。
- 安装版自带 Python sidecar，普通用户无需安装 Python。

当前版本为 `0.1.0`，仅支持 Windows x64。

## 2. 使用前准备

### 2.1 普通用户

需要准备：

- Windows 10 或 Windows 11 x64。
- 可访问模型服务的网络环境。
- 一个有效的 OpenAI 或 OpenAI-compatible API Key。
- 对应服务的 Base URL 和模型名称。
- 至少一份受支持的资料文件。

Windows 安装程序会按需安装或下载 WebView2 运行组件。公司网络如果拦截下载，请先让管理员安装 WebView2 Runtime。

### 2.2 源码构建用户

除上述条件外，还需要：

- Python 3.11 或更高版本。
- Node.js 和 npm。
- Rust stable 工具链。
- Microsoft Visual Studio C++ Build Tools，并安装“使用 C++ 的桌面开发”工作负载。
- 可下载 Python、npm、Cargo 和 WebView2 依赖的网络环境。

Tauri 的最新 Windows 前置条件可参考其[官方 prerequisites 文档](https://v2.tauri.app/start/prerequisites/)。

## 3. 安装

### 3.1 从 GitHub Release 安装

如果 [GitHub Releases](https://github.com/bigdanguo-ui/PPT-Agent/releases) 页面已经发布安装包：

1. 下载名称类似 `PPT Agent V2_0.1.0_x64-setup.exe` 的 NSIS 安装程序。
2. 双击安装；如 Windows 显示来源提示，请确认下载地址确实是本项目 Release 页面。
3. 安装结束后，从开始菜单打开 `PPT Agent V2`。

安装目录默认是：

```text
%LOCALAPPDATA%\PPT Agent V2
```

### 3.2 从源码构建

在 PowerShell 中执行：

```powershell
git clone https://github.com/bigdanguo-ui/PPT-Agent.git
cd PPT-Agent
.\deploy\windows\build.ps1
```

第一次构建会安装 Python 和 Node 构建依赖，耗时取决于网络和机器性能。构建完成后可在以下目录找到安装包：

```text
apps\ppt-agent-desktop\src-tauri\target\release\bundle\nsis\
apps\ppt-agent-desktop\src-tauri\target\release\bundle\msi\
```

如果依赖已经安装，只重建 sidecar 和前端且暂不生成安装包：

```powershell
.\deploy\windows\build.ps1 -SkipInstall -SkipInstaller
```

只重新构建 Tauri Release 和安装包：

```powershell
npm --prefix apps\ppt-agent-desktop run build
```

## 4. 首次启动与模型配置

1. 打开 `PPT Agent V2`。
2. 点击界面顶部的“模型设置”。
3. 填写以下内容：

   - **API Key**：模型服务提供的密钥。只有需要设置或更新时才填写。
   - **Base URL**：模型服务的 OpenAI-compatible API 地址，例如 `https://api.openai.com/v1`。
   - **模型**：服务端实际提供的模型名称。

4. 点击“保存并重启服务”。
5. 等待本地 Agent 服务重新连接，右侧“工作流”状态应显示已验收能力数量。

桌面版将密钥保存在 Windows 凭据管理器中，不会写入前端源码、项目 `.env` 或 Git 仓库。

注意：Base URL、模型名称和 API Key 必须属于同一个服务。不同模型供应商支持的模型名、额度和上下文限制并不相同。

## 5. 生成第一份 PPT

### 5.1 准备资料

当前界面支持导入：

```text
.pdf  .docx  .pptx  .md  .txt
```

可以一次选择多个文件。建议优先提供结构清晰、可复制文本的资料。扫描版 PDF 的识别效果取决于其是否包含可提取文字。

当前纯主题研究工作流仍在验收中，因此请至少附加一份受支持的资料；没有资料时，“开始生成”可能不可用。

### 5.2 编写生成要求

在输入框中说明：

- 演示文稿用途，例如答辩、课程汇报、项目路演或工作总结。
- 目标受众。
- 期望页数。
- 输出语言。
- 必须覆盖的重点。
- 视觉风格偏好。
- 是否需要图表、流程图、时间线或对比页。

示例：

```text
根据上传的论文生成 12 页中文组会汇报，面向熟悉深度学习的研究生。
重点说明研究问题、方法结构、训练数据、实验结果和局限性；方法部分使用流程图，
实验部分优先使用对比图表，整体采用简洁的学术答辩风格。
```

### 5.3 选择风格

右侧“设计风格库”提供两种方式：

- 保持“由 Planner 自动选择”，由 Planner 根据用途和内容匹配已发布风格。
- 手动选择一套风格，强制本次生成使用对应的 `design.md` 设计契约。

风格需要在创建任务前选择。任务开始后，当前会话的风格和版本哈希会被锁定，以避免页面之间逐步漂移。

### 5.4 开始生成

1. 点击“导入资料”，选择一个或多个文件。
2. 等待文件名出现在输入区。
3. 输入生成要求。
4. 点击“开始生成”。

Agent 会依次完成资料读取、风格选择、内容与页面设计、SVG 生成、质量检查和 PPTX 导出。任务进入 `review_ready` 后，界面会显示逐页预览和下载按钮。

生成时间取决于页数、资料体积、模型速度和网络情况。请不要在生成过程中反复关闭应用。若应用意外退出，再次启动时会尝试恢复最近任务、预览和版本记录。

## 6. 评审、修改与版本管理

### 6.1 下载初稿

任务显示“等待评审”后，先浏览页面预览。如果初稿可以直接使用，点击“下载 PPTX”并选择保存位置。

### 6.2 整份修改

选择“整份修改”，填写对整体内容或视觉的要求，例如：

```text
整体减少文字密度，保留当前配色；所有章节页强化层级，图表标题使用结论式表达。
```

点击“生成新版本”。

### 6.3 指定页面修改

选择“指定页面”，填写页码，支持用逗号、中文逗号或空格分隔：

```text
3, 5, 8
```

再填写明确的修改要求，例如：

```text
第 3 页减少段落文字并改成三阶段流程图；第 5 页突出基线与本方法的差异；
第 8 页保留数据但改成横向条形图。
```

### 6.4 采用或回滚版本

每次修改会产生一个候选版本。右侧版本区可以：

- 点击“采用”，把候选版本设为当前接受版本。
- 点击“回到此版”，将会话恢复到对应版本。

下载前请确认预览和当前版本符合预期。

## 7. 从现有 PPTX 导入设计风格

1. 新建一个尚未开始生成的会话。
2. 在右侧“设计风格库”点击“从 PPT 导入风格”。
3. 选择一个未加密的 `.pptx` 文件。
4. 等待 Agent 解析主题、字体、颜色、画布、布局和页面密度。
5. 在风格 Review 中检查：

   - 风格名称与说明。
   - 颜色和字体规则。
   - 脱敏参考页面。
   - 完整 `design.md` 设计契约。

6. 确认无误后点击“确认并发布”；不希望保留时点击“丢弃草稿”。

发布后，新风格会进入 Planner 可选目录并自动选中。

导入时会将参考页面中的原始文字替换为语义占位符，将图片和 Logo 替换为中性占位块，并移除脚本、外链和元数据。未确认的草稿不会进入 Planner 的已发布风格目录。

更多规则见 [`STYLE_LIBRARY.md`](./STYLE_LIBRARY.md)。

## 8. 数据、隐私与网络边界

### 8.1 本地目录

```text
应用安装目录：%LOCALAPPDATA%\PPT Agent V2
用户数据目录：%LOCALAPPDATA%\OfficeAgent\PPTAgentV2
用户风格目录：%LOCALAPPDATA%\OfficeAgent\PPTAgentV2\style-library
风格草稿目录：%LOCALAPPDATA%\OfficeAgent\PPTAgentV2\style-imports
```

用户数据目录可能包含上传资料、SQLite 状态、生成 Artifact、预览、PPTX 和历史版本。卸载应用不一定会删除这些数据。

如需重置，请先退出应用并备份整个用户数据目录，再重命名或删除该目录。不要在应用运行时手动编辑其中的 SQLite 文件。

### 8.2 模型服务

PPT Agent V2 是本地桌面应用，但不是离线模型。为了规划、撰写和设计演示文稿，资料内容或其提取结果可能发送到你配置的模型服务。

请在上传机密资料前确认：

- 模型供应商的数据保留和隐私政策。
- API Key 所属账号和计费范围。
- 公司或研究机构对第三方模型服务的使用要求。

不要把 API Key 写进 Markdown、截图、问题报告或 Git 提交。

## 9. 常见问题

### 9.1 应用一直显示连接中

- 首次运行可能需要解压 sidecar 运行时，请等待一段时间。
- 检查 WebView2 Runtime 是否可用。
- 打开“模型设置”，核对 Base URL、模型名和网络代理。
- 保存设置并让应用重启本地服务。

### 9.2 保存设置后模型仍不可用

- 确认 Base URL 是完整 API 根地址，很多 OpenAI-compatible 服务要求包含 `/v1`。
- 确认模型名称由当前服务实际提供。
- 确认 API Key 尚未过期并有足够额度。
- 使用同一网络环境测试服务是否可访问。

### 9.3 “开始生成”按钮不可用

- 确保生成要求不是空白。
- 至少导入一个 `.pdf`、`.docx`、`.pptx`、`.md` 或 `.txt` 文件。
- 等待右侧工作流状态完成连接。

### 9.4 风格导入失败

- 确认文件扩展名为 `.pptx`，而不是旧版 `.ppt`。
- 确认文件没有密码或加密保护。
- 先用 PowerPoint 或兼容软件打开并重新保存，再重试。
- 避免导入损坏或只有外部链接内容的文件。

### 9.5 下载 PPTX 失败

- 选择当前账号有写入权限的目录。
- 避免使用被其他程序占用的同名文件。
- 检查安全软件是否阻止桌面应用保存文件。

### 9.6 源码构建失败

依次检查：

```powershell
python --version
node --version
npm --version
rustc --version
cargo --version
```

确认已安装 Visual Studio C++ Build Tools。第一次构建不要使用 `-SkipInstall`；该参数只适用于依赖已经成功安装的环境。

## 10. 开发验证

在仓库根目录运行：

```powershell
$env:PYTHONPATH="$PWD\backend;$PWD\vendor\ppt_master_runtime"
python -m unittest discover -s tests -p 'test_*.py' -v
python -m unittest discover -s backend\ppt_agent\tests -t backend -p 'test_*.py' -v
npm --prefix apps\ppt-agent-ui run build
cargo test --manifest-path apps\ppt-agent-desktop\src-tauri\Cargo.toml
```

完整构建、冒烟测试和 Skill 校验命令见 [`../COMMANDS.md`](../COMMANDS.md)。架构边界见 [`../ARCHITECTURE.md`](../ARCHITECTURE.md)，验收结果与尚未覆盖的范围见 [`ACCEPTANCE_REPORT.md`](./ACCEPTANCE_REPORT.md)。

## 11. 当前限制

- 仅支持 Windows 桌面环境。
- 纯主题、无资料的研究生成路径仍在验收中。
- 实际质量受模型能力、资料完整度和请求清晰度影响。
- 安装包属于构建产物，不直接提交到 Git；发布时应作为 GitHub Release 资产提供。
