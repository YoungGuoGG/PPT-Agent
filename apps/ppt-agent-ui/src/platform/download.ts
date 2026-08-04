import { api } from "../api";
import type { Artifact } from "../types";

export function suggestedDeckName(title: string): string {
  const safeTitle = title
    .replace(/[<>:"/\\|?*\u0000-\u001f]/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 96);
  return `${safeTitle || "PPT-Agent-演示文稿"}.pptx`;
}

export async function downloadDeckArtifact(
  sessionId: string,
  artifact: Artifact,
  fileName: string,
): Promise<string> {
  if ("__TAURI_INTERNALS__" in window) {
    const { invoke } = await import("@tauri-apps/api/core");
    return invoke<string>("save_artifact_to_downloads", {
      sourcePath: artifact.path,
      suggestedName: fileName,
    });
  }

  const blob = await api.artifactBlob(sessionId, artifact.id);
  const objectUrl = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = objectUrl;
  anchor.download = fileName;
  anchor.hidden = true;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1_000);
  return fileName;
}
