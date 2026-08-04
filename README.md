# PPT Agent V2

[中文使用指南](./docs/USER_GUIDE.zh-CN.md) · [English User Guide](./docs/USER_GUIDE.en.md)

PPT Agent V2 is a Windows desktop presentation agent that turns source material into an editable PPTX, keeps generation running until a reviewable draft is ready, and supports versioned revisions inside the app.

PPT Agent V2 是一款 Windows 桌面演示文稿智能体，可将资料连续转换为可编辑的 PPTX，在初稿完成后统一进入评审，并在应用内进行版本化修改。

> Current release: `0.1.0` · Platform: Windows x64 · Source installation requires Python 3.11+, Node.js/npm, Rust, and the Windows C++ build toolchain.

## Highlights / 核心能力

- **Planner-driven execution / Planner 驱动执行**：用户请求会被编译为可校验的工作流计划，模型不能直接执行任意 Python 或 Shell。
- **Review-oriented workflow / 面向评审的工作流**：生成过程不中途打断，完成后可整份修改、指定页面修改、采用版本或回滚。
- **Editable output / 可编辑输出**：最终交付标准 `PPTX`，同时在应用内显示逐页 SVG 预览。
- **Multiple source formats / 多种资料格式**：支持导入 `.pdf`、`.docx`、`.pptx`、`.md` 和 `.txt`。
- **Versioned style library / 版本化风格库**：内置多套 `design.md` 风格，也可从现有 PPTX 提取、脱敏、评审并发布自定义风格。
- **Desktop credential isolation / 桌面凭据隔离**：API Key 保存在 Windows 凭据管理器，不写入前端文件或仓库。
- **Self-contained installed runtime / 安装态自带运行时**：正式安装包封装 Python sidecar，终端用户无需单独安装 Python。

## Quick Start / 快速开始

### Use a packaged installer / 使用安装包

如果 [GitHub Releases](https://github.com/bigdanguo-ui/PPT-Agent/releases) 已提供安装包，优先下载 NSIS 安装程序并安装。首次启动后打开“模型设置”，填写 API Key、OpenAI-compatible Base URL 和模型名，然后导入资料并输入生成要求。

When an installer is available on [GitHub Releases](https://github.com/bigdanguo-ui/PPT-Agent/releases), install the NSIS package, open **模型设置 (Model Settings)**, enter an API key, an OpenAI-compatible base URL, and a model name, then attach source files and describe the deck you need. The current desktop UI uses Chinese labels; the English guide shows each important Chinese label with its translation.

### Build from source / 从源码构建

```powershell
git clone https://github.com/bigdanguo-ui/PPT-Agent.git
cd PPT-Agent
.\deploy\windows\build.ps1
```

Build outputs:

```text
apps/ppt-agent-desktop/src-tauri/target/release/bundle/nsis/
apps/ppt-agent-desktop/src-tauri/target/release/bundle/msi/
```

完整的环境准备、应用操作、风格导入和故障排查请阅读[中文使用指南](./docs/USER_GUIDE.zh-CN.md)。For prerequisites, application workflow, style import, and troubleshooting, see the [English User Guide](./docs/USER_GUIDE.en.md).

## Supported Workflow / 当前可用流程

```text
attach sources -> plan -> ingest -> select style -> compose design
               -> generate SVG -> inspect and repair -> export PPTX
               -> review -> revise/accept/rollback -> download
```

当前建议至少附加一个受支持的资料文件。纯主题研究工作流仍在验收中，应用不会把尚未完成验收的能力显示为可执行主流程。

Attach at least one supported source file. Topic-only research remains under validation and is not exposed as a production-ready route.

## Repository Layout / 仓库结构

```text
PPT-Agent/
├── apps/                  # React UI and Tauri desktop shell
├── backend/ppt_agent/     # Agent core, Planner, Gateway, Review, storage
├── deploy/windows/        # PyInstaller, Tauri, smoke, and desktop E2E tools
├── docs/                  # User, architecture, planning, and acceptance docs
├── skills/                # Planner, design, and style-extraction skills
├── style-library/         # Built-in published design.md style packages
├── tests/                 # Planner, compiler, provider, and runtime tests
└── vendor/                # Selected PPT Master runtime algorithms and assets
```

## Development

Common commands are documented in [`COMMANDS.md`](./COMMANDS.md). Architecture and trust boundaries are described in [`ARCHITECTURE.md`](./ARCHITECTURE.md).

```powershell
$env:PYTHONPATH="$PWD\backend;$PWD\vendor\ppt_master_runtime"
python -m unittest discover -s tests -p 'test_*.py' -v
python -m unittest discover -s backend\ppt_agent\tests -t backend -p 'test_*.py' -v
npm --prefix apps\ppt-agent-ui run build
cargo test --manifest-path apps\ppt-agent-desktop\src-tauri\Cargo.toml
```

## Data and Security / 数据与安全

- Installed data / 安装态数据：`%LOCALAPPDATA%\OfficeAgent\PPTAgentV2`
- Installed application / 安装目录：`%LOCALAPPDATA%\PPT Agent V2`
- Model credentials / 模型凭据：Windows Credential Manager
- Source material may be sent to the model endpoint you configure; the app is local-first, not an offline model runtime.
- 生成过程中，资料内容可能发送到你配置的模型服务；本应用是本地桌面应用，但不是离线模型。

See [`THIRD_PARTY_NOTICES.md`](./THIRD_PARTY_NOTICES.md) for vendored component attribution. Acceptance evidence and known limitations are recorded in [`docs/ACCEPTANCE_REPORT.md`](./docs/ACCEPTANCE_REPORT.md).
