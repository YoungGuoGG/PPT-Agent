# PPT Agent V2 Commands

以下命令在 `E:\OfficeAgent\ppt-agent-v2` 下执行。

## Python 测试

```powershell
$env:PYTHONPATH="$PWD\backend;$PWD\vendor\ppt_master_runtime"
python -m unittest discover -s tests -p 'test_*.py' -v
python -m unittest discover -s backend\ppt_agent\tests -t backend -p 'test_*.py' -v
```

## Skill 与风格契约校验

```powershell
$env:PYTHONUTF8="1"
python C:\Users\ASUS\.codex\skills\.system\skill-creator\scripts\quick_validate.py .\skills\ppt-style-extractor
$env:PYTHONPATH="$PWD\backend"
python -m unittest tests.unit.test_style_library -v
```

## 前端与 Rust 测试

```powershell
npm --prefix apps\ppt-agent-ui run build
cargo test --manifest-path apps\ppt-agent-desktop\src-tauri\Cargo.toml
```

## Windows 构建

首次构建（安装 Python 与 Node 构建依赖）：

```powershell
.\deploy\windows\build.ps1
```

已有依赖时仅重建 sidecar 和前端：

```powershell
.\deploy\windows\build.ps1 -SkipInstall -SkipInstaller
```

仅重新构建 Tauri Release 与安装包：

```powershell
npm --prefix apps\ppt-agent-desktop run build
```

## sidecar 冒烟

```powershell
.\deploy\windows\smoke_test.ps1
```

该测试验证模型提示词、两个 V2 Skill、工作流注册表、受限脚本分发、鉴权、Tauri CORS 和可执行工作流目录。

自动化验收需要临时覆盖 Windows 凭据时，可在启动桌面应用的进程环境中设置 `PPT_AGENT_PREFER_ENV_MODEL_SETTINGS=1` 并提供 `OPENAI_API_KEY`、`OPENAI_BASE_URL`、`OPENAI_MODEL`。该开关不修改凭据管理器；普通启动不设置它。
