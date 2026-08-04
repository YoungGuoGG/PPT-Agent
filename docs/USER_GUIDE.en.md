# PPT Agent V2 User Guide

## 1. Overview

PPT Agent V2 is a Windows desktop presentation agent. It reads PDFs, Word documents, existing presentations, Markdown, or plain text, plans the content and visual direction, generates and checks SVG slides, exports an editable PPTX, and then moves the completed draft into a unified review stage.

Key capabilities:

- Generation runs continuously and asks for feedback after a complete draft is available.
- The output is an editable `.pptx` with per-slide previews inside the app.
- You can revise the entire deck or selected slides only.
- Every revision creates a version that can be accepted or rolled back.
- The Planner can select a built-in style, or you can extract a reusable style from an existing PPTX.
- The packaged application includes its Python sidecar; end users do not install Python separately.

The current version is `0.1.0` and supports Windows x64 only.

> The current desktop UI uses Chinese labels. This guide writes the actual label first and adds an English translation, for example **模型设置 (Model Settings)**.

## 2. Requirements

### 2.1 End users

You need:

- Windows 10 or Windows 11 x64.
- Network access to your model provider.
- A valid OpenAI or OpenAI-compatible API key.
- The provider's base URL and model name.
- At least one supported source file.

The Windows installer downloads or installs WebView2 when needed. If your organization blocks installer downloads, ask an administrator to install the WebView2 Runtime first.

### 2.2 Source builds

Building from source also requires:

- Python 3.11 or newer.
- Node.js and npm.
- The stable Rust toolchain.
- Microsoft Visual Studio C++ Build Tools with the **Desktop development with C++** workload.
- Network access for Python, npm, Cargo, and WebView2 dependencies.

See the official [Tauri prerequisites](https://v2.tauri.app/start/prerequisites/) for the current Windows toolchain requirements.

## 3. Installation

### 3.1 Install from GitHub Releases

If an installer has been published on [GitHub Releases](https://github.com/bigdanguo-ui/PPT-Agent/releases):

1. Download the NSIS installer with a name similar to `PPT Agent V2_0.1.0_x64-setup.exe`.
2. Run it. If Windows displays a source warning, verify that the file came from this project's Releases page.
3. Open `PPT Agent V2` from the Start menu.

The default application directory is:

```text
%LOCALAPPDATA%\PPT Agent V2
```

### 3.2 Build from source

Run the following commands in PowerShell:

```powershell
git clone https://github.com/bigdanguo-ui/PPT-Agent.git
cd PPT-Agent
.\deploy\windows\build.ps1
```

The first build installs Python and Node build dependencies. Its duration depends on your network and hardware. Installers are written to:

```text
apps\ppt-agent-desktop\src-tauri\target\release\bundle\nsis\
apps\ppt-agent-desktop\src-tauri\target\release\bundle\msi\
```

If dependencies are already installed, rebuild the sidecar and frontend without producing an installer:

```powershell
.\deploy\windows\build.ps1 -SkipInstall -SkipInstaller
```

To rebuild only the Tauri release and installers:

```powershell
npm --prefix apps\ppt-agent-desktop run build
```

## 4. First Launch and Model Configuration

1. Open `PPT Agent V2`.
2. Click **模型设置 (Model Settings)** at the top of the window.
3. Enter:

   - **API Key**: the key issued by your provider. Enter it only when setting or replacing the stored key.
   - **Base URL**: the provider's OpenAI-compatible API root, such as `https://api.openai.com/v1`.
   - **Model**: a model name that the provider actually exposes.

4. Click **保存并重启服务 (Save and restart service)**.
5. Wait for the local Agent service to reconnect. The workflow panel should report the number of validated capabilities.

The desktop application stores the key in Windows Credential Manager. It is not written to the frontend source, a project `.env` file, or the Git repository.

The base URL, model name, and API key must belong to the same service. Available models, quotas, and context limits vary by provider.

## 5. Generate a Presentation

### 5.1 Attach source material

The application currently accepts:

```text
.pdf  .docx  .pptx  .md  .txt
```

You can attach multiple files. Prefer documents with clear structure and extractable text. Results from scanned PDFs depend on whether usable text is embedded in the document.

Topic-only research is still under validation, so attach at least one supported source file. The **Start generation** action may remain disabled when no source is attached.

### 5.2 Write a useful request

Describe:

- The purpose, such as a thesis defense, class presentation, pitch, or project review.
- The target audience.
- The desired slide count.
- The output language.
- Topics that must be covered.
- Visual preferences.
- Whether charts, process diagrams, timelines, or comparison slides are needed.

Example:

```text
Create a 12-slide English research presentation from the attached paper for an audience
of graduate students familiar with machine learning. Focus on the research problem,
method architecture, training data, experimental results, and limitations. Use a process
diagram for the method and comparison charts for the experiments. Keep the style concise
and appropriate for an academic defense.
```

### 5.3 Select a style

The **设计风格库 (Design Style Library)** offers two modes:

- Keep **由 Planner 自动选择 (Let the Planner choose)** to match a published style to the content and purpose.
- Select a specific style to require its `design.md` contract for the current deck.

Choose the style before starting the task. Once generation begins, the session records the style version and hash to prevent visual drift between slides and revisions.

### 5.4 Start generation

1. Click **导入资料 (Attach sources)** and select one or more files.
2. Wait for their names to appear in the composer.
3. Enter the generation request.
4. Click **开始生成 (Start generation)**.

The Agent ingests the material, selects a style, designs the deck, generates SVG slides, runs quality checks, and exports a PPTX. When the session reaches `review_ready`, slide previews and the download action appear.

Generation time depends on slide count, source size, provider latency, and network conditions. Avoid repeatedly closing the application while a task is running. If the application exits unexpectedly, the next launch attempts to restore the latest task, previews, and version history.

## 6. Review, Revision, and Versions

### 6.1 Download the draft

When the task shows **等待评审 (Ready for review)**, inspect the slide previews. If the result is ready to use, click **下载 PPTX (Download PPTX)** and choose a destination.

### 6.2 Revise the whole deck

Select **整份修改 (Whole deck)**, then enter a deck-wide instruction, for example:

```text
Reduce overall text density while preserving the current color palette. Strengthen the
hierarchy on section slides and rewrite chart titles as conclusions.
```

Click **生成新版本 (Generate new version)**.

### 6.3 Revise selected slides

Select **指定页面 (Selected slides)** and enter slide numbers separated by commas, Chinese commas, or spaces:

```text
3, 5, 8
```

Then provide a precise revision request:

```text
Turn slide 3 into a three-stage process diagram, emphasize the difference between the
baseline and our method on slide 5, and preserve the data on slide 8 while changing it
to a horizontal bar chart.
```

### 6.4 Accept or roll back a version

Each revision creates a candidate version. In the version panel you can:

- Click **采用 (Accept)** to make a candidate the accepted version.
- Click **回到此版 (Return to this version)** to restore the session to that version.

Confirm that the previews and active version are correct before downloading.

## 7. Import a Design Style from PPTX

1. Create a new conversation that has not started generation.
2. Click **从 PPT 导入风格 (Import style from PPT)** in the Design Style Library.
3. Select an unencrypted `.pptx` file.
4. Wait while the Agent analyzes its theme, fonts, colors, canvas, geometry, and slide density.
5. In the style review, inspect:

   - The proposed style name and description.
   - Color and typography rules.
   - De-identified reference slides.
   - The complete `design.md` contract.

6. Click **确认并发布 (Confirm and publish)** to keep it, or **丢弃草稿 (Discard draft)** to remove the draft.

After publication, the style enters the Planner catalog and becomes the selected style.

During import, source text in reference slides is replaced with semantic placeholders, images and logos are replaced with neutral blocks, and scripts, external links, and metadata are removed. An unconfirmed draft is not added to the Planner's published catalog.

See [`STYLE_LIBRARY.md`](./STYLE_LIBRARY.md) for the complete style contract.

## 8. Data, Privacy, and Network Boundaries

### 8.1 Local directories

```text
Application:       %LOCALAPPDATA%\PPT Agent V2
User data:         %LOCALAPPDATA%\OfficeAgent\PPTAgentV2
Published styles:  %LOCALAPPDATA%\OfficeAgent\PPTAgentV2\style-library
Style drafts:      %LOCALAPPDATA%\OfficeAgent\PPTAgentV2\style-imports
```

The user-data directory may contain uploaded sources, SQLite state, generated artifacts, previews, PPTX files, and version history. Uninstalling the application may not remove this directory.

To reset the application, close it and back up the entire user-data directory before renaming or deleting it. Do not manually edit its SQLite files while the application is running.

### 8.2 Model provider

PPT Agent V2 is a local desktop application, but it is not an offline model runtime. Source content or extracted material may be sent to the model endpoint you configure so that the Agent can plan, write, and design the presentation.

Before attaching confidential material, review:

- The provider's data retention and privacy policies.
- The account and billing scope of the API key.
- Your organization’s rules for third-party model services.

Never place an API key in Markdown files, screenshots, issue reports, or Git commits.

## 9. Troubleshooting

### 9.1 The application remains on “连接中 (Connecting)”

- The first launch may need time to extract the sidecar runtime.
- Verify that the WebView2 Runtime is available.
- Open **模型设置 (Model Settings)** and check the base URL, model name, and network proxy.
- Save the settings and allow the application to restart the local service.

### 9.2 The model is unavailable after saving settings

- Verify the complete API root. Many compatible providers require `/v1`.
- Confirm that the provider exposes the configured model name.
- Confirm that the API key is active and has sufficient quota.
- Test provider access from the same network environment.

### 9.3 “开始生成 (Start generation)” is disabled

- Make sure the request is not blank.
- Attach at least one `.pdf`, `.docx`, `.pptx`, `.md`, or `.txt` file.
- Wait for the workflow status to finish connecting.

### 9.4 Style import fails

- Confirm that the file uses `.pptx`, not the legacy `.ppt` format.
- Remove password or encryption protection.
- Open and re-save the file in PowerPoint or a compatible application, then retry.
- Avoid corrupted files or presentations whose content exists only through external links.

### 9.5 PPTX download fails

- Select a directory where the current account has write permission.
- Avoid a destination file that is open in another application.
- Check whether endpoint security software is blocking file saves.

### 9.6 A source build fails

Check the toolchain in order:

```powershell
python --version
node --version
npm --version
rustc --version
cargo --version
```

Verify that Visual Studio C++ Build Tools are installed. Do not use `-SkipInstall` for the first build; it is intended only for environments where dependencies were installed successfully earlier.

## 10. Development Verification

Run these commands from the repository root:

```powershell
$env:PYTHONPATH="$PWD\backend;$PWD\vendor\ppt_master_runtime"
python -m unittest discover -s tests -p 'test_*.py' -v
python -m unittest discover -s backend\ppt_agent\tests -t backend -p 'test_*.py' -v
npm --prefix apps\ppt-agent-ui run build
cargo test --manifest-path apps\ppt-agent-desktop\src-tauri\Cargo.toml
```

See [`../COMMANDS.md`](../COMMANDS.md) for full build, smoke-test, and skill-validation commands. Trust boundaries are described in [`../ARCHITECTURE.md`](../ARCHITECTURE.md). Acceptance evidence and uncovered scenarios are documented in [`ACCEPTANCE_REPORT.md`](./ACCEPTANCE_REPORT.md).

## 11. Current Limitations

- Windows desktop only.
- Topic-only generation without attached source material remains under validation.
- Output quality depends on model capability, source completeness, and request clarity.
- Installers are build artifacts and are not committed to Git; publish them as GitHub Release assets.
