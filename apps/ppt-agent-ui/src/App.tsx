import { useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowDownToLine,
  Check,
  ChevronRight,
  CircleAlert,
  FileText,
  Library,
  LoaderCircle,
  MessageSquareText,
  Paperclip,
  Plus,
  RotateCcw,
  Send,
  Settings,
  SlidersHorizontal,
  Sparkles,
  Trash2,
  Upload,
  X,
} from "lucide-react";
import { api, configureGateway } from "./api";
import { downloadDeckArtifact, suggestedDeckName } from "./platform/download";
import type { Conversation, Snapshot, StyleCase, StyleImport, WorkflowCapability } from "./types";

const terminal = new Set(["review_ready", "succeeded", "failed", "cancelled"]);

export default function App() {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [conversationId, setConversationId] = useState("");
  const [styles, setStyles] = useState<StyleCase[]>([]);
  const [workflows, setWorkflows] = useState<WorkflowCapability[]>([]);
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [prompt, setPrompt] = useState("");
  const [files, setFiles] = useState<Array<{ name: string; path: string; size: number }>>([]);
  const [selectedStyle, setSelectedStyle] = useState("");
  const [pages, setPages] = useState(12);
  const [busy, setBusy] = useState(false);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [serviceConnected, setServiceConnected] = useState(false);
  const [gatewayLoading, setGatewayLoading] = useState(true);
  const [reviewScope, setReviewScope] = useState<"whole_deck" | "selected_pages">("whole_deck");
  const [reviewPages, setReviewPages] = useState("");
  const [reviewInstruction, setReviewInstruction] = useState("");
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [styleImport, setStyleImport] = useState<StyleImport | null>(null);
  const [styleBusy, setStyleBusy] = useState(false);
  const [modelSettings, setModelSettings] = useState({ apiKey: "", baseUrl: "", model: "" });
  const fileInput = useRef<HTMLInputElement>(null);
  const styleFileInput = useRef<HTMLInputElement>(null);
  const historyRequest = useRef(0);

  useEffect(() => {
    void boot();
  }, []);

  useEffect(() => {
    const id = snapshot?.session.id;
    if (!id || terminal.has(snapshot.session.status)) return;
    const source = new EventSource(api.eventUrl(id));
    for (const event of ["step_started", "step_succeeded", "subworkflow_failed", "review_ready", "session_failed", "revision_ready", "revision_failed"]) {
      source.addEventListener(event, () => void refresh(id));
    }
    source.onerror = () => {
      source.close();
      window.setTimeout(() => void refresh(id), 1200);
    };
    return () => source.close();
  }, [snapshot?.session.id, snapshot?.session.status]);

  async function boot(): Promise<boolean> {
    setGatewayLoading(true);
    setError("");
    try {
      if ("__TAURI_INTERNALS__" in window) {
        const { invoke } = await import("@tauri-apps/api/core");
        const gateway = await invoke<{ baseUrl: string; token: string }>("gateway_config");
        configureGateway(gateway.baseUrl, gateway.token);
      }
      await waitForGateway();
      setServiceConnected(true);
      const [existing, styleCases, workflowCatalog] = await Promise.all([api.conversations(), api.styles(), api.workflows()]);
      const first = existing[0] ?? await api.createConversation("新建演示文稿");
      const available = existing.length ? existing : [first];
      setConversations(available);
      let selected = available.find((conversation) => conversation.id === conversationId);
      if (!selected) {
        for (const conversation of available) {
          if ((await api.sessions(conversation.id)).length > 0) {
            selected = conversation;
            break;
          }
        }
      }
      selected ??= first;
      setConversationId(selected.id);
      setStyles(styleCases);
      setWorkflows(workflowCatalog);
      await restoreConversation(selected.id);
      setError("");
      return true;
    } catch (cause) {
      setServiceConnected(false);
      setWorkflows([]);
      setError(messageOf(cause, "无法连接本地 PPT Agent 服务"));
      return false;
    } finally {
      setGatewayLoading(false);
    }
  }

  async function refresh(sessionId: string) {
    try {
      setSnapshot(await api.snapshot(sessionId));
    } catch (cause) {
      setError(messageOf(cause, "无法更新运行状态"));
    }
  }

  async function restoreConversation(id: string) {
    const requestId = ++historyRequest.current;
    setHistoryLoading(true);
    setError("");
    try {
      const sessions = await api.sessions(id);
      const restored = sessions[0] ? await api.snapshot(sessions[0].id) : null;
      if (historyRequest.current === requestId) setSnapshot(restored);
    } catch (cause) {
      if (historyRequest.current === requestId) {
        setSnapshot(null);
        setError(messageOf(cause, "无法恢复历史任务"));
      }
    } finally {
      if (historyRequest.current === requestId) setHistoryLoading(false);
    }
  }

  async function selectConversation(id: string) {
    setConversationId(id);
    await restoreConversation(id);
  }

  async function newConversation() {
    const conversation = await api.createConversation("新建演示文稿");
    setConversations((current) => [conversation, ...current]);
    setConversationId(conversation.id);
    historyRequest.current += 1;
    setHistoryLoading(false);
    setSnapshot(null);
    setPrompt("");
    setFiles([]);
  }

  async function uploadFiles(list: FileList | null) {
    if (!list?.length) return;
    setBusy(true);
    setError("");
    try {
      for (const file of Array.from(list)) {
        const uploaded = await api.upload(file);
        setFiles((current) => [...current, uploaded]);
      }
    } catch (cause) {
      setError(messageOf(cause, "文件导入失败"));
    } finally {
      setBusy(false);
      if (fileInput.current) fileInput.current.value = "";
    }
  }

  async function submit() {
    if (!conversationId || !prompt.trim()) return;
    setBusy(true);
    setError("");
    try {
      const result = await api.submit(conversationId, prompt.trim(), files.map((file) => file.path), {
        pages,
        language: "zh-CN",
        style_id: selectedStyle || undefined,
      });
      setSnapshot(result);
    } catch (cause) {
      setError(messageOf(cause, "生成任务创建失败"));
    } finally {
      setBusy(false);
    }
  }

  async function importStyle(list: FileList | null) {
    const file = list?.[0];
    if (!file) return;
    setStyleBusy(true);
    setError("");
    try {
      const uploaded = await api.upload(file);
      let current = await api.createStyleImport(uploaded.path, file.name.replace(/\.pptx$/i, ""));
      setStyleImport(current);
      for (let attempt = 0; attempt < 300 && !["review_ready", "published", "failed", "discarded"].includes(current.status); attempt += 1) {
        await new Promise((resolve) => window.setTimeout(resolve, 1000));
        current = await api.styleImport(current.id);
        setStyleImport(current);
      }
      if (current.status === "failed") setError(current.error?.message || "PPT 风格解析失败");
    } catch (cause) {
      setError(messageOf(cause, "PPT 风格导入失败"));
    } finally {
      setStyleBusy(false);
      if (styleFileInput.current) styleFileInput.current.value = "";
    }
  }

  async function publishImportedStyle() {
    if (!styleImport) return;
    setStyleBusy(true);
    setError("");
    try {
      const published = await api.publishStyleImport(styleImport.id);
      setStyles(await api.styles());
      setSelectedStyle(published.id);
      setNotice(`风格“${published.name}”已发布并选中`);
      setStyleImport(null);
    } catch (cause) {
      setError(messageOf(cause, "风格发布失败"));
    } finally {
      setStyleBusy(false);
    }
  }

  async function discardImportedStyle() {
    if (!styleImport) return;
    try {
      await api.discardStyleImport(styleImport.id);
      setStyleImport(null);
    } catch (cause) {
      setError(messageOf(cause, "无法丢弃风格草稿"));
    }
  }

  async function revise() {
    if (!snapshot || !reviewInstruction.trim()) return;
    const pageNumbers = reviewScope === "selected_pages"
      ? reviewPages.split(/[，,\s]+/).map(Number).filter((value) => Number.isInteger(value) && value > 0)
      : [];
    if (reviewScope === "selected_pages" && pageNumbers.length === 0) {
      setError("请填写要修改的页面，例如：3, 5, 8");
      return;
    }
    setBusy(true);
    try {
      await api.revise(snapshot.session.id, reviewScope, reviewInstruction.trim(), pageNumbers);
      setReviewInstruction("");
      window.setTimeout(() => void refresh(snapshot.session.id), 600);
    } catch (cause) {
      setError(messageOf(cause, "修订任务创建失败"));
    } finally {
      setBusy(false);
    }
  }

  async function downloadDeck() {
    if (!snapshot || !latestArtifact) return;
    setDownloading(true);
    setError("");
    setNotice("");
    try {
      const title = conversations.find((conversation) => conversation.id === conversationId)?.title ?? "PPT-Agent-演示文稿";
      const destination = await downloadDeckArtifact(snapshot.session.id, latestArtifact, suggestedDeckName(title));
      setNotice(`PPTX 已保存到：${destination}`);
    } catch (cause) {
      setError(messageOf(cause, "PPTX 下载失败"));
    } finally {
      setDownloading(false);
    }
  }

  async function saveSettings() {
    if (!("__TAURI_INTERNALS__" in window)) {
      setError("网页模式请通过服务端环境变量配置模型；桌面版会写入 Windows 凭据管理器。");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const { invoke } = await import("@tauri-apps/api/core");
      const entries = [
        ["OPENAI_API_KEY", modelSettings.apiKey],
        ["OPENAI_BASE_URL", modelSettings.baseUrl],
        ["OPENAI_MODEL", modelSettings.model],
      ];
      for (const [name, value] of entries) {
        if (value.trim()) await invoke("save_model_setting", { name, value });
      }
      await invoke("restart_agent_sidecar");
      const connected = await boot();
      if (!connected) return;
      setSettingsOpen(false);
      setModelSettings({ apiKey: "", baseUrl: "", model: "" });
    } catch (cause) {
      setError(messageOf(cause, "模型设置保存失败"));
    } finally {
      setBusy(false);
    }
  }

  const progress = useMemo(() => {
    const steps = snapshot?.plan?.steps ?? [];
    if (!steps.length) return 0;
    return Math.round((steps.filter((step) => step.status === "succeeded").length / steps.length) * 100);
  }, [snapshot]);
  const latestArtifact = snapshot?.artifacts.filter((artifact) => artifact.role === "deck" || artifact.role === "deck_revision").at(-1);
  const latestPreviewVersion = Math.max(
    0,
    ...(snapshot?.artifacts
      .map((artifact) => /^page_preview_v(\d+)_/.exec(artifact.role)?.[1])
      .filter(Boolean)
      .map(Number) ?? []),
  );
  const pagePreviews = snapshot?.artifacts.filter((artifact) => artifact.role.startsWith(`page_preview_v${latestPreviewVersion}_`)) ?? [];
  const reviewReady = snapshot?.session.status === "review_ready" || snapshot?.session.status === "succeeded";
  const documentWorkflow = workflows.find((workflow) => workflow.id === "document_to_ppt");
  const topicWorkflow = workflows.find((workflow) => workflow.id === "topic_research");
  const requestedWorkflow = files.length ? documentWorkflow : topicWorkflow;
  const canGenerate = requestedWorkflow?.executable ?? false;
  const executableCount = workflows.filter((workflow) => workflow.executable).length;

  return (
    <div className="app-shell">
      <aside className="sidebar" aria-label="任务导航">
        <div className="brand"><span className="brand-mark"><Sparkles size={17} /></span><span>PPT Agent</span></div>
        <button className="new-button" onClick={() => void newConversation()}><Plus size={17} />新建演示文稿</button>
        <div className="nav-label">最近任务</div>
        <nav className="conversation-list">
          {conversations.map((conversation) => (
            <button key={conversation.id} className={`conversation ${conversation.id === conversationId ? "active" : ""}`} onClick={() => void selectConversation(conversation.id)}>
              <MessageSquareText size={16} /><span>{conversation.title}</span><ChevronRight size={14} />
            </button>
          ))}
        </nav>
        <button className="settings-button" onClick={() => setSettingsOpen(true)}><Settings size={16} />模型设置</button>
        <button className="sidebar-footer" disabled={gatewayLoading} onClick={() => void boot()} title={serviceConnected ? "Agent 服务运行正常" : "点击重新连接本地 Agent 服务"}><span className={`service-dot ${serviceConnected ? "connected" : "disconnected"}`} />{gatewayLoading ? "正在连接 Agent 服务" : serviceConnected ? "本地 Agent 服务已连接" : "连接失败，点击重试"}</button>
      </aside>

      <main className="workspace">
        <header className="topbar">
          <div><span className="eyebrow">交互式制作</span><h1>{snapshot ? "演示文稿制作进度" : "从资料到可编辑 PPT"}</h1></div>
          {snapshot && <StatusPill status={snapshot.session.status} />}
        </header>

        <section className="conversation-pane" aria-live="polite">
          {historyLoading ? (
            <div className="empty-state"><LoaderCircle className="spin" size={28} /><h2>正在恢复任务</h2><p>正在读取最近一次生成状态、页面预览和版本记录。</p></div>
          ) : !snapshot ? (
            <div className="empty-state">
              <div className="empty-icon"><FileText size={28} /></div>
              <h2>把资料交给 Agent，直接生成完整初稿</h2>
              <p>导入 PDF、Word 或现有 PPT，说明用途与重点。Agent 会连续完成读取、设计、SVG 生成、检查和导出，不在中途打断你。</p>
              <div className="flow-strip" aria-label="端到端工作流">
                {['读取资料', '设计文稿', '生成 SVG', '质量修复', '导出 PPTX'].map((item, index) => <span key={item}><b>{index + 1}</b>{item}</span>)}
              </div>
            </div>
          ) : (
            <div className="run-view">
              <div className="user-request"><span>你的要求</span><p>{snapshot.session.prompt}</p></div>
              <div className="agent-progress">
                <div className="progress-heading"><div><span className="eyebrow">Agent 正在工作</span><h2>{reviewReady ? "初稿已经可以评审" : "连续生成中，请稍候"}</h2></div><strong>{progress}%</strong></div>
                <div className="progress-track"><i style={{ width: `${progress}%` }} /></div>
                <div className="steps">
                  {snapshot.plan.steps.map((step, index) => <StepRow key={step.id} step={step} index={index + 1} />)}
                </div>
              </div>
              {snapshot.session.status === "failed" && (
                <div className="error-panel"><CircleAlert size={19} /><div><strong>生成未完成</strong><p>{String(snapshot.session.error?.message ?? "请查看运行详情后重试")}</p></div></div>
              )}
              {snapshot.session.status !== "failed" && snapshot.session.error && (
                <div className="error-panel"><CircleAlert size={19} /><div><strong>最近一次修订未完成</strong><p>{String(snapshot.session.error.message ?? "当前版本仍可继续评审")}</p></div></div>
              )}
              {reviewReady && (
                <section className="review-card">
                  <div className="review-head"><div><span className="eyebrow">Review</span><h2>先看完整结果，再决定怎么改</h2></div>{latestArtifact && <button className="download-button" disabled={downloading} onClick={() => void downloadDeck()}>{downloading ? <LoaderCircle className="spin" size={17} /> : <ArrowDownToLine size={17} />}{downloading ? "正在保存" : "下载 PPTX"}</button>}</div>
                  {pagePreviews.length > 0 && <div className="preview-grid" aria-label="页面预览">{pagePreviews.map((artifact, index) => <figure key={artifact.id}><img src={api.artifactUrl(snapshot.session.id, artifact.id)} alt={`第 ${index + 1} 页预览`} /><figcaption>第 {index + 1} 页</figcaption></figure>)}</div>}
                  <div className="scope-switch" role="group" aria-label="修订范围">
                    <button className={reviewScope === "whole_deck" ? "selected" : ""} onClick={() => setReviewScope("whole_deck")}>整份修改</button>
                    <button className={reviewScope === "selected_pages" ? "selected" : ""} onClick={() => setReviewScope("selected_pages")}>指定页面</button>
                  </div>
                  {reviewScope === "selected_pages" && <label className="field-label">页码<input value={reviewPages} onChange={(event) => setReviewPages(event.target.value)} placeholder="例如 3, 5, 8" /></label>}
                  <label className="field-label grow">修改要求<textarea value={reviewInstruction} onChange={(event) => setReviewInstruction(event.target.value)} placeholder="例如：第 3 页减少文字，改成更清晰的流程图；整体保留当前配色。" /></label>
                  <button className="primary-button" disabled={busy || !reviewInstruction.trim()} onClick={() => void revise()}>{busy ? <LoaderCircle className="spin" size={17} /> : <Sparkles size={17} />}生成新版本</button>
                </section>
              )}
            </div>
          )}
        </section>

        {!snapshot && !historyLoading && (
          <section className="composer" aria-label="演示文稿需求">
            {files.length > 0 && <div className="attachment-row">{files.map((file) => <span key={file.path}><FileText size={15} />{file.name}<button aria-label={`移除 ${file.name}`} onClick={() => setFiles((current) => current.filter((item) => item.path !== file.path))}><X size={14} /></button></span>)}</div>}
            <textarea value={prompt} onChange={(event) => setPrompt(event.target.value)} placeholder="描述你要制作的演示文稿，例如：根据论文生成 12 页中文汇报，重点讲方法、实验和 Agent 架构……" />
            <div className="composer-tools">
              <input ref={fileInput} type="file" multiple hidden accept=".pdf,.docx,.pptx,.md,.txt" onChange={(event) => void uploadFiles(event.target.files)} />
              <button className="icon-text-button" title="导入资料" onClick={() => fileInput.current?.click()}><Paperclip size={17} />导入资料</button>
              <label className="compact-field"><span>页数</span><input type="number" min={1} max={80} value={pages} onChange={(event) => setPages(Number(event.target.value))} /></label>
              <button className="send-button" disabled={busy || !prompt.trim() || !canGenerate} title={!canGenerate ? "此入口仍在验收中，请先导入 PDF、Word、PPTX 或 Markdown" : undefined} onClick={() => void submit()}>{busy ? <LoaderCircle className="spin" size={18} /> : <Send size={18} />}开始生成</button>
            </div>
            {!canGenerate && workflows.length > 0 && <p className="capability-hint">纯主题研究工作流仍在验收中；当前请先导入资料，系统不会把开发中能力伪装为可用。</p>}
          </section>
        )}
        {error && <button className="toast" onClick={() => setError("")}><CircleAlert size={17} /><span>{error}</span><X size={15} /></button>}
        {notice && <button className="toast success" onClick={() => setNotice("")}><Check size={17} /><span>{notice}</span><X size={15} /></button>}
      </main>

      <aside className="inspector" aria-label="设计与版本">
        <div className="inspector-section"><div className="section-title"><Library size={17} /><h2>设计风格库</h2></div><p className="section-help">Planner 选择一套完整 design.md；生成器按其令牌与布局规则执行。</p>
          <select value={selectedStyle} onChange={(event) => setSelectedStyle(event.target.value)} disabled={Boolean(snapshot)} aria-label="选择设计风格"><option value="">由 Planner 自动选择</option>{styles.map((style) => <option key={style.id} value={style.id}>{style.name}</option>)}</select>
          {selectedStyle && <div className="style-detail">{styles.find((style) => style.id === selectedStyle)?.description || "已选择此设计系统"}</div>}
          <input ref={styleFileInput} type="file" hidden accept=".pptx" onChange={(event) => void importStyle(event.target.files)} />
          <button className="style-import-button" disabled={styleBusy || Boolean(snapshot)} onClick={() => styleFileInput.current?.click()}>{styleBusy ? <LoaderCircle className="spin" size={16} /> : <Upload size={16} />}{styleBusy ? "正在解析 PPT" : "从 PPT 导入风格"}</button>
          {snapshot?.plan?.style_selection && <div className="style-selection"><strong>本次采用：{styles.find((style) => style.id === snapshot.plan.style_selection?.style_id)?.name ?? snapshot.plan.style_selection.style_id}</strong><span>{snapshot.plan.style_selection.reason}</span><code>{snapshot.plan.style_selection.design_hash.slice(0, 10)}</code></div>}
        </div>
        <div className="inspector-section"><div className="section-title"><SlidersHorizontal size={17} /><h2>生成设置</h2></div><dl><div><dt>输出</dt><dd>可编辑 PPTX</dd></div><div><dt>画布</dt><dd>16:9</dd></div><div><dt>交互</dt><dd>仅评审阶段</dd></div><div><dt>工作流</dt><dd>{gatewayLoading ? "连接中" : serviceConnected ? workflows.length ? `${executableCount}/${workflows.length} 已验收` : "暂无可用能力" : <button className="inline-retry" onClick={() => void boot()}>重新连接</button>}</dd></div></dl></div>
        {snapshot && <div className="inspector-section versions"><div className="section-title"><RotateCcw size={17} /><h2>版本</h2></div>{snapshot.versions.map((version) => <div className="version-row" key={version.id}><div><strong>V{version.version}</strong><span>{version.pages.length ? `第 ${version.pages.join('、')} 页` : '整份'}</span></div><span className={`version-status ${version.status}`}>{version.status === 'accepted' ? '已采用' : '候选'}</span>{reviewReady && version.status !== 'accepted' && <div className="version-actions"><button onClick={() => void api.rollback(snapshot.session.id, version.id).then(setSnapshot)}>回到此版</button><button onClick={() => void api.accept(snapshot.session.id, version.id).then(setSnapshot)}><Check size={14} />采用</button></div>}</div>)}</div>}
      </aside>
      {settingsOpen && <div className="modal-backdrop" role="presentation" onMouseDown={() => setSettingsOpen(false)}><section className="settings-modal" role="dialog" aria-modal="true" aria-labelledby="settings-title" onMouseDown={(event) => event.stopPropagation()}><div className="modal-title"><div><span className="eyebrow">Windows 凭据管理器</span><h2 id="settings-title">模型连接设置</h2></div><button aria-label="关闭" onClick={() => setSettingsOpen(false)}><X size={18} /></button></div><p>桌面版会安全保存密钥并重启本地 Agent 服务；不会把密钥写入前端文件。</p><label className="field-label">API Key<input type="password" autoComplete="off" value={modelSettings.apiKey} onChange={(event) => setModelSettings({ ...modelSettings, apiKey: event.target.value })} placeholder="仅在需要更新时填写" /></label><label className="field-label">Base URL<input value={modelSettings.baseUrl} onChange={(event) => setModelSettings({ ...modelSettings, baseUrl: event.target.value })} placeholder="https://api.openai.com/v1" /></label><label className="field-label">模型<input value={modelSettings.model} onChange={(event) => setModelSettings({ ...modelSettings, model: event.target.value })} placeholder="模型名称" /></label><div className="modal-actions"><button onClick={() => setSettingsOpen(false)}>取消</button><button className="primary-button" disabled={busy} onClick={() => void saveSettings()}>{busy ? <LoaderCircle className="spin" size={17} /> : <Check size={17} />}保存并重启服务</button></div></section></div>}
      {styleImport && <div className="modal-backdrop" role="presentation"><section className="style-review-modal" role="dialog" aria-modal="true" aria-labelledby="style-review-title">
        <div className="modal-title"><div><span className="eyebrow">PPT → Design System</span><h2 id="style-review-title">{styleImport.status === "review_ready" ? "评审风格草稿" : styleImport.status === "failed" ? "风格解析未完成" : "正在解析风格"}</h2></div>{["review_ready", "failed"].includes(styleImport.status) && <button aria-label="关闭" onClick={() => setStyleImport(null)}><X size={18} /></button>}</div>
        {styleImport.status !== "review_ready" ? <div className="style-import-progress"><LoaderCircle className="spin" size={28} /><strong>{styleImport.progress}%</strong><p>{styleImport.steps.find((step) => step.status === "running")?.title ?? (styleImport.status === "failed" ? "解析未完成" : "正在准备")}</p><div className="progress-track"><i style={{ width: `${styleImport.progress}%` }} /></div>{styleImport.error?.message && <div className="error-panel"><CircleAlert size={18} /><p>{styleImport.error.message}</p></div>}</div> : <>
          <p className="style-review-description">Agent 已提取可复用设计规则并移除原文、图片与外部链接。确认后它才会进入 Planner 的风格目录。</p>
          <div className="style-review-summary"><div><strong>{styleImport.summary?.name}</strong><span>{styleImport.summary?.description}</span></div><dl><div><dt>源页面</dt><dd>{styleImport.summary?.slide_count}</dd></div><div><dt>参考页</dt><dd>{styleImport.summary?.reference_count}</dd></div><div><dt>分析方式</dt><dd>{styleImport.summary?.analysis_mode}</dd></div></dl></div>
          {styleImport.summary?.warning && <p className="style-warning">{styleImport.summary.warning}</p>}
          <div className="token-row">{styleImport.summary?.colors?.map((color) => <span key={color} title={color} style={{ backgroundColor: color }} />)}</div>
          {styleImport.summary?.fonts?.length ? <p className="font-list">字体：{styleImport.summary.fonts.join(" · ")}</p> : null}
          <div className="style-reference-grid">{styleImport.references?.map((name, index) => <figure key={name}><img src={api.styleImportReferenceUrl(styleImport.id, name)} alt={`脱敏参考页 ${index + 1}`} /><figcaption>参考 {index + 1}</figcaption></figure>)}</div>
          <details className="design-source"><summary>查看完整 design.md</summary><pre>{styleImport.design}</pre></details>
          <div className="style-review-actions"><button className="danger-ghost" disabled={styleBusy} onClick={() => void discardImportedStyle()}><Trash2 size={16} />丢弃草稿</button><button className="primary-button" disabled={styleBusy} onClick={() => void publishImportedStyle()}>{styleBusy ? <LoaderCircle className="spin" size={17} /> : <Check size={17} />}确认并发布</button></div>
        </>}
      </section></div>}
    </div>
  );
}

function StepRow({ step, index }: { step: Snapshot["plan"]["steps"][number]; index: number }) {
  return <div className={`step-row ${step.status}`}><span className="step-index">{step.status === "succeeded" ? <Check size={15} /> : step.status === "running" ? <LoaderCircle className="spin" size={15} /> : index}</span><div><strong>{step.title}</strong><small>{labelFor(step.status)}</small></div></div>;
}

function StatusPill({ status }: { status: Snapshot["session"]["status"] }) {
  return <span className={`status-pill ${status}`}>{status === "review_ready" ? "等待评审" : status === "running" ? "生成中" : status === "failed" ? "需要处理" : status === "succeeded" ? "已完成" : status}</span>;
}

function labelFor(status: string) {
  if (status === "succeeded") return "已完成";
  if (status === "running") return "正在执行";
  if (status === "failed") return "执行失败";
  return "等待执行";
}

function messageOf(cause: unknown, fallback: string) {
  if (cause instanceof TypeError && /fetch|network/i.test(cause.message)) return fallback;
  return cause instanceof Error ? cause.message : fallback;
}

async function waitForGateway() {
  let lastError: unknown;
  for (let attempt = 0; attempt < 40; attempt += 1) {
    try {
      return await api.health();
    } catch (cause) {
      lastError = cause;
      await new Promise((resolve) => window.setTimeout(resolve, 500));
    }
  }
  throw lastError ?? new Error("本地 PPT Agent 服务启动超时");
}
