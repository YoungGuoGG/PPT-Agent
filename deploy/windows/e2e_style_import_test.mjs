import { readFile } from "node:fs/promises";
import { basename, resolve } from "node:path";

function argument(name, fallback = "") {
  const index = process.argv.indexOf(name);
  return index >= 0 ? process.argv[index + 1] : fallback;
}

const debugPort = Number(argument("--debug-port", "9335"));
const sourcePath = resolve(argument("--source"));
const sleep = (milliseconds) => new Promise((done) => setTimeout(done, milliseconds));

async function waitForDebugTarget() {
  const deadline = Date.now() + 90_000;
  while (Date.now() < deadline) {
    try {
      const targets = await (await fetch(`http://127.0.0.1:${debugPort}/json/list`)).json();
      const page = targets.find((target) => target.type === "page" && target.webSocketDebuggerUrl);
      if (page) return page;
    } catch {
      // WebView debug endpoint is not ready yet.
    }
    await sleep(500);
  }
  throw new Error("desktop WebView debug target did not become ready");
}

async function evaluate(webSocketUrl, expression) {
  return new Promise((done, reject) => {
    const socket = new WebSocket(webSocketUrl);
    // First installed launch may still be extracting the bundled Python runtime
    // while the WebView debug target already exists.
    const timer = setTimeout(() => reject(new Error("CDP evaluation timed out")), 180_000);
    socket.addEventListener("open", () => socket.send(JSON.stringify({
      id: 1,
      method: "Runtime.evaluate",
      params: { expression, awaitPromise: true, returnByValue: true },
    })));
    socket.addEventListener("message", (event) => {
      const message = JSON.parse(String(event.data));
      if (message.id !== 1) return;
      clearTimeout(timer);
      socket.close();
      if (message.error || message.result?.exceptionDetails) {
        reject(new Error(JSON.stringify(message.error ?? message.result.exceptionDetails)));
      } else {
        done(message.result?.result?.value);
      }
    });
    socket.addEventListener("error", () => reject(new Error("failed to connect to WebView")));
  });
}

async function request(gateway, path, init = {}) {
  const headers = new Headers(init.headers);
  headers.set("X-PPT-Agent-Token", gateway.token);
  const response = await fetch(`${gateway.baseUrl}${path}`, { ...init, headers });
  if (!response.ok) throw new Error(`${path} failed (${response.status}): ${await response.text()}`);
  return (response.headers.get("content-type") ?? "").includes("application/json")
    ? response.json()
    : response.arrayBuffer();
}

async function main() {
  if (!argument("--source")) throw new Error("--source is required");
  let state;
  const appDeadline = Date.now() + 240_000;
  while (!state && Date.now() < appDeadline) {
    const target = await waitForDebugTarget();
    try {
      state = await evaluate(target.webSocketDebuggerUrl, `(async () => {
        for (let attempt = 0; attempt < 600; attempt += 1) {
          if (window.__TAURI_INTERNALS__?.invoke) {
            const gateway = await window.__TAURI_INTERNALS__.invoke("gateway_config");
            return { gateway, hasStyleImport: document.body.innerText.includes("从 PPT 导入风格") };
          }
          await new Promise((done) => setTimeout(done, 250));
        }
        throw new Error("Tauri internals did not become ready");
      })()`);
    } catch {
      // Initial navigation replaces the execution context after runtime extraction.
      await sleep(500);
    }
  }
  if (!state) throw new Error("installed desktop did not become ready");
  if (!state?.hasStyleImport) throw new Error("installed UI does not expose PPT style import");
  const gateway = state.gateway;
  const healthDeadline = Date.now() + 90_000;
  while (Date.now() < healthDeadline) {
    try {
      if ((await request(gateway, "/health")).ok) break;
    } catch {
      await sleep(500);
    }
  }
  const form = new FormData();
  form.append("file", new Blob([await readFile(sourcePath)]), basename(sourcePath));
  const upload = await request(gateway, "/v1/uploads", { method: "POST", body: form });
  let imported = await request(gateway, "/v1/style-imports", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ source: upload.path, name: `Installed Smoke ${Date.now()}`, use_model: false }),
  });
  const deadline = Date.now() + 120_000;
  while (Date.now() < deadline && !["review_ready", "failed"].includes(imported.status)) {
    await sleep(1_000);
    imported = await request(gateway, `/v1/style-imports/${imported.id}`);
  }
  if (imported.status !== "review_ready" || !imported.references?.length || !imported.design) {
    throw new Error(`installed style import failed: ${JSON.stringify(imported.error ?? imported)}`);
  }
  const reference = Buffer.from(await request(gateway, `/v1/style-imports/${imported.id}/references/${imported.references[0]}`)).toString("utf8");
  if (!/\[(TITLE|BODY|LABEL) \d+\]/.test(reference)) throw new Error("installed reference is not sanitized");
  const published = await request(gateway, `/v1/style-imports/${imported.id}/publish`, { method: "POST" });
  const styles = await request(gateway, "/v1/style-cases");
  if (!styles.some((style) => style.id === published.id && style.design_hash)) {
    throw new Error("installed published style is missing from catalog");
  }
  console.log(JSON.stringify({
    status: "passed",
    installedUi: true,
    importStatus: imported.status,
    publishedStyleId: published.id,
    referenceCount: imported.references.length,
    catalogCount: styles.length,
  }, null, 2));
}

await main();
