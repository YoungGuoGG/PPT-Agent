import type { Conversation, Snapshot, StyleCase, StyleImport, WorkflowCapability } from "./types";

let API = import.meta.env.VITE_PPT_AGENT_API ?? "http://127.0.0.1:8765";
let TOKEN = import.meta.env.VITE_PPT_AGENT_TOKEN ?? "";

export function configureGateway(baseUrl: string, token: string) {
  API = baseUrl.replace(/\/$/, "");
  TOKEN = token;
}

function authenticated(init?: RequestInit): RequestInit {
  const headers = new Headers(init?.headers);
  if (TOKEN) headers.set("X-PPT-Agent-Token", TOKEN);
  return { ...init, headers };
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(API + path, authenticated(init));
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(detail || `请求失败 (${response.status})`);
  }
  return response.json() as Promise<T>;
}

async function requestBlob(path: string): Promise<Blob> {
  const response = await fetch(API + path, authenticated());
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(detail || `下载失败 (${response.status})`);
  }
  return response.blob();
}

export const api = {
  health: () => request<{ ok: boolean }>("/health"),
  workflows: () => request<WorkflowCapability[]>("/v1/workflows"),
  conversations: () => request<Conversation[]>("/v1/conversations"),
  sessions: (conversationId: string) => request<Snapshot["session"][]>(`/v1/conversations/${conversationId}/sessions`),
  createConversation: (title: string) => request<Conversation>("/v1/conversations", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ title }) }),
  submit: (conversationId: string, prompt: string, attachments: string[], options: Record<string, unknown>) => request<Snapshot>(`/v1/conversations/${conversationId}/messages`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ prompt, attachments, options }) }),
  snapshot: (sessionId: string) => request<Snapshot>(`/v1/sessions/${sessionId}?include_events=false`),
  upload: async (file: File) => {
    const body = new FormData();
    body.append("file", file);
    return request<{ path: string; name: string; size: number }>("/v1/uploads", { method: "POST", body });
  },
  revise: (sessionId: string, scope: "whole_deck" | "selected_pages", instruction: string, pages: number[]) => request<{ status: string }>(`/v1/sessions/${sessionId}/revisions`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ scope, instruction, pages }) }),
  accept: (sessionId: string, versionId: string) => request<Snapshot>(`/v1/sessions/${sessionId}/versions/${versionId}/accept`, { method: "POST" }),
  rollback: (sessionId: string, versionId: string) => request<Snapshot>(`/v1/sessions/${sessionId}/versions/${versionId}/rollback`, { method: "POST" }),
  styles: () => request<StyleCase[]>("/v1/style-cases"),
  createStyleImport: (source: string, name: string) => request<StyleImport>("/v1/style-imports", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ source, name }) }),
  styleImport: (importId: string) => request<StyleImport>(`/v1/style-imports/${importId}`),
  publishStyleImport: (importId: string) => request<StyleCase>(`/v1/style-imports/${importId}/publish`, { method: "POST" }),
  discardStyleImport: (importId: string) => request<StyleImport>(`/v1/style-imports/${importId}/discard`, { method: "POST" }),
  styleImportReferenceUrl: (importId: string, name: string) => `${API}/v1/style-imports/${importId}/references/${encodeURIComponent(name)}${TOKEN ? `?access_token=${encodeURIComponent(TOKEN)}` : ""}`,
  artifactBlob: (sessionId: string, artifactId: string) => requestBlob(`/v1/sessions/${sessionId}/artifacts/${artifactId}`),
  artifactUrl: (sessionId: string, artifactId: string) => `${API}/v1/sessions/${sessionId}/artifacts/${artifactId}${TOKEN ? `?access_token=${encodeURIComponent(TOKEN)}` : ""}`,
  eventUrl: (sessionId: string, after = 0) => {
    const query = new URLSearchParams({ after: String(after) });
    if (TOKEN) query.set("access_token", TOKEN);
    return `${API}/v1/sessions/${sessionId}/events?${query}`;
  },
};
