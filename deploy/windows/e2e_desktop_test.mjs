import { createHash } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { basename, resolve } from "node:path";

function argument(name, fallback = "") {
  const index = process.argv.indexOf(name);
  return index >= 0 ? process.argv[index + 1] : fallback;
}

const debugPort = Number(argument("--debug-port", "9333"));
const sourcePath = resolve(argument("--source"));
const outputRoot = resolve(argument("--output", ".scratch/desktop-e2e"));
const timeoutMs = Number(argument("--timeout-ms", String(20 * 60 * 1000)));

if (!sourcePath || sourcePath === resolve(".")) {
  throw new Error("--source is required");
}

const sleep = (milliseconds) => new Promise((resolvePromise) => setTimeout(resolvePromise, milliseconds));

async function waitForDebugTarget() {
  const deadline = Date.now() + 90_000;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`http://127.0.0.1:${debugPort}/json/list`);
      const targets = await response.json();
      const page = targets.find((target) => target.type === "page" && target.webSocketDebuggerUrl);
      if (page) return page;
    } catch {
      // The WebView debug endpoint appears after the desktop window is ready.
    }
    await sleep(500);
  }
  throw new Error("desktop WebView debug target did not become ready");
}

async function evaluate(webSocketUrl, expression) {
  return new Promise((resolvePromise, rejectPromise) => {
    const socket = new WebSocket(webSocketUrl);
    const timer = setTimeout(() => {
      socket.close();
      rejectPromise(new Error("CDP evaluation timed out"));
    }, 30_000);
    socket.addEventListener("open", () => {
      socket.send(JSON.stringify({
        id: 1,
        method: "Runtime.evaluate",
        params: { expression, awaitPromise: true, returnByValue: true },
      }));
    });
    socket.addEventListener("message", (event) => {
      const message = JSON.parse(String(event.data));
      if (message.id !== 1) return;
      clearTimeout(timer);
      socket.close();
      if (message.error || message.result?.exceptionDetails) {
        rejectPromise(new Error(JSON.stringify(message.error ?? message.result.exceptionDetails)));
        return;
      }
      resolvePromise(message.result?.result?.value);
    });
    socket.addEventListener("error", () => {
      clearTimeout(timer);
      rejectPromise(new Error("failed to connect to desktop WebView debug target"));
    });
  });
}

async function request(gateway, path, init = {}) {
  const headers = new Headers(init.headers);
  headers.set("X-PPT-Agent-Token", gateway.token);
  const response = await fetch(`${gateway.baseUrl}${path}`, { ...init, headers });
  if (!response.ok) {
    throw new Error(`${init.method ?? "GET"} ${path} failed (${response.status}): ${await response.text()}`);
  }
  const contentType = response.headers.get("content-type") ?? "";
  return contentType.includes("application/json") ? response.json() : response.arrayBuffer();
}

async function waitForRestoredReview(webSocketUrl, sessionId) {
  const deadline = Date.now() + 30_000;
  while (Date.now() < deadline) {
    try {
      const state = await evaluate(
        webSocketUrl,
        `({
          sessionPrompt: document.querySelector('.user-request p')?.textContent ?? '',
          hasReview: !!document.querySelector('.review-card'),
          hasDownload: !!document.querySelector('.download-button'),
          loading: document.body.innerText.includes('正在恢复任务')
        })`,
      );
      if (state?.hasReview && state?.hasDownload && !state?.loading) return state;
    } catch {
      // Reloading replaces the JavaScript execution context for a moment.
    }
    await sleep(250);
  }
  throw new Error(`desktop UI did not restore review state for ${sessionId}`);
}

async function waitForDownloadNotice(webSocketUrl) {
  const deadline = Date.now() + 30_000;
  while (Date.now() < deadline) {
    const notice = await evaluate(
      webSocketUrl,
      `document.querySelector('.toast.success span')?.textContent ?? ''`,
    );
    if (notice.startsWith("PPTX 已保存到：")) return notice.slice("PPTX 已保存到：".length);
    await sleep(250);
  }
  throw new Error("desktop UI did not report a completed PPTX download");
}

async function main() {
  let gateway;
  const gatewayDeadline = Date.now() + 90_000;
  while (!gateway && Date.now() < gatewayDeadline) {
    const target = await waitForDebugTarget();
    try {
      gateway = await evaluate(
        target.webSocketDebuggerUrl,
        `(async () => {
          for (let attempt = 0; attempt < 120; attempt += 1) {
            if (window.__TAURI_INTERNALS__?.invoke) {
              return window.__TAURI_INTERNALS__.invoke("gateway_config");
            }
            await new Promise((resolvePromise) => setTimeout(resolvePromise, 250));
          }
          throw new Error("Tauri internals did not become ready");
        })()`,
      );
    } catch {
      // The first WebView navigation can replace the JavaScript execution
      // context while the packaged application finishes booting.
      await sleep(500);
    }
  }
  if (!gateway?.baseUrl || !gateway?.token) {
    throw new Error("desktop window did not expose a valid gateway configuration");
  }

  const healthDeadline = Date.now() + 90_000;
  let healthy = false;
  while (Date.now() < healthDeadline) {
    try {
      const health = await request(gateway, "/health");
      if (health.ok) {
        healthy = true;
        break;
      }
    } catch {
      await sleep(500);
    }
  }
  if (!healthy) throw new Error("desktop-launched gateway did not become healthy");

  const conversation = await request(gateway, "/v1/conversations", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title: "V2 Release E2E" }),
  });
  const form = new FormData();
  form.append("file", new Blob([await readFile(sourcePath)]), basename(sourcePath));
  const upload = await request(gateway, "/v1/uploads", { method: "POST", body: form });
  const initial = await request(gateway, `/v1/conversations/${conversation.id}/messages`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      prompt: "将这份文档制作成2页中文演示文稿，内容准确、结构清晰、视觉简洁。",
      attachments: [upload.path],
      options: { pages: 2, language: "zh-CN" },
    }),
  });

  const sessionId = initial.session.id;
  const deadline = Date.now() + timeoutMs;
  let snapshot = initial;
  let lastStatus = "";
  while (Date.now() < deadline) {
    snapshot = await request(gateway, `/v1/sessions/${sessionId}`);
    if (snapshot.session.status !== lastStatus) {
      console.log(`desktop_e2e_status=${snapshot.session.status}`);
      lastStatus = snapshot.session.status;
    }
    if (["review_ready", "succeeded", "failed", "cancelled"].includes(snapshot.session.status)) break;
    await sleep(2_000);
  }

  if (snapshot.session.status !== "review_ready" && snapshot.session.status !== "succeeded") {
    const error = snapshot.session.error ?? { message: "desktop generation timed out" };
    throw new Error(`desktop generation ended as ${snapshot.session.status}: ${JSON.stringify(error)}`);
  }

  await mkdir(outputRoot, { recursive: true });
  const deck = snapshot.artifacts.find((artifact) => artifact.role === "deck" || artifact.role === "deck_revision");
  const previews = snapshot.artifacts.filter((artifact) => artifact.role.startsWith("page_preview_"));
  if (!deck || previews.length === 0) {
    throw new Error("desktop generation did not return both a PPTX and page previews");
  }

  const sessions = await request(gateway, `/v1/conversations/${conversation.id}/sessions`);
  if (sessions[0]?.id !== sessionId) {
    throw new Error("conversation history did not return the latest session first");
  }

  const deckBytes = Buffer.from(await request(gateway, `/v1/sessions/${sessionId}/artifacts/${deck.id}`));
  if (deckBytes.subarray(0, 2).toString("ascii") !== "PK") {
    throw new Error("downloaded deck is not a valid ZIP-based PPTX");
  }
  const deckPath = resolve(outputRoot, "release-desktop-e2e.pptx");
  await writeFile(deckPath, deckBytes);
  const previewPaths = [];
  for (let index = 0; index < previews.length; index += 1) {
    const bytes = Buffer.from(await request(gateway, `/v1/sessions/${sessionId}/artifacts/${previews[index].id}`));
    const path = resolve(outputRoot, `preview-${String(index + 1).padStart(2, "0")}.svg`);
    await writeFile(path, bytes);
    previewPaths.push(path);
  }

  if (!snapshot.plan?.style_selection?.style_id || !snapshot.plan?.style_selection?.design_hash) {
    throw new Error("generation plan did not record a versioned style selection");
  }

  const styleForm = new FormData();
  styleForm.append("file", new Blob([deckBytes]), "e2e-style-source.pptx");
  const styleUpload = await request(gateway, "/v1/uploads", { method: "POST", body: styleForm });
  let styleImport = await request(gateway, "/v1/style-imports", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ source: styleUpload.path, name: `E2E Imported ${Date.now()}`, use_model: false }),
  });
  const styleDeadline = Date.now() + 120_000;
  while (Date.now() < styleDeadline && !["review_ready", "failed"].includes(styleImport.status)) {
    await sleep(1_000);
    styleImport = await request(gateway, `/v1/style-imports/${styleImport.id}`);
  }
  if (styleImport.status !== "review_ready" || !styleImport.design || !styleImport.references?.length) {
    throw new Error(`style import did not reach review: ${JSON.stringify(styleImport.error ?? styleImport)}`);
  }
  const referenceBytes = Buffer.from(await request(gateway, `/v1/style-imports/${styleImport.id}/references/${styleImport.references[0]}`));
  if (!referenceBytes.toString("utf8").includes("[TITLE") && !referenceBytes.toString("utf8").includes("[BODY")) {
    throw new Error("style reference was not sanitized into semantic placeholders");
  }
  const publishedStyle = await request(gateway, `/v1/style-imports/${styleImport.id}/publish`, { method: "POST" });
  const publishedStyles = await request(gateway, "/v1/style-cases");
  if (!publishedStyles.some((style) => style.id === publishedStyle.id && style.design_hash)) {
    throw new Error("published style did not enter the Planner catalog");
  }


  let target = await waitForDebugTarget();
  await evaluate(target.webSocketDebuggerUrl, `window.location.reload(); true`);
  target = await waitForDebugTarget();
  const restored = await waitForRestoredReview(target.webSocketDebuggerUrl, sessionId);
  if (restored.sessionPrompt !== snapshot.session.prompt) {
    throw new Error("desktop UI restored a different task after reload");
  }
  await evaluate(
    target.webSocketDebuggerUrl,
    `(() => { const button = document.querySelector('.download-button'); if (!button) throw new Error('download button missing'); button.click(); return true; })()`,
  );
  const downloadedPath = await waitForDownloadNotice(target.webSocketDebuggerUrl);
  const downloadedBytes = await readFile(downloadedPath);
  if (downloadedBytes.subarray(0, 2).toString("ascii") !== "PK") {
    throw new Error("desktop download command did not save a valid PPTX");
  }

  const result = {
    schema: "ppt-agent.desktop-e2e.v1",
    status: snapshot.session.status,
    sessionId,
    workflowId: snapshot.session.workflow_id,
    versionCount: snapshot.versions.length,
    previewCount: previews.length,
    deckPath,
    deckBytes: deckBytes.length,
    deckSha256: createHash("sha256").update(deckBytes).digest("hex"),
    previewPaths,
    selectedStyleId: snapshot.plan.style_selection.style_id,
    selectedStyleHash: snapshot.plan.style_selection.design_hash,
    importedStyleId: publishedStyle.id,
    importedStyleReferenceCount: styleImport.references.length,
    historyRestored: true,
    downloadedPath,
    downloadedBytes: downloadedBytes.length,
  };
  await writeFile(resolve(outputRoot, "result.json"), `${JSON.stringify(result, null, 2)}\n`, "utf8");
  console.log(JSON.stringify(result, null, 2));
}

await main();
