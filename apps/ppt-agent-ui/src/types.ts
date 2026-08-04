export type Status = "queued" | "running" | "review_ready" | "revising" | "succeeded" | "failed" | "cancelled";

export interface Conversation { id: string; title: string; updated_at: string }
export interface Step { id: string; workflow_id: string; title: string; status: string }
export interface Version { id: string; version: number; pptx_path: string; status: string; instruction: string; pages: number[] }
export interface Artifact { id: string; role: string; path: string; size: number }
export interface StyleCase {
  id: string;
  name: string;
  version: string;
  description: string;
  tags: string[];
  suitable_for: string[];
  avoid_for: string[];
  design_hash: string;
  design_path: string;
  reference_paths: string[];
  origin: "bundled" | "imported";
}
export interface StyleImport {
  id: string;
  status: "uploaded" | "extracting" | "analyzing" | "drafting" | "validating" | "review_ready" | "published" | "failed" | "discarded";
  source_name: string;
  progress: number;
  current_step?: string;
  steps: Array<{ id: string; title: string; status: string }>;
  design?: string;
  references?: string[];
  summary?: {
    id: string;
    name: string;
    description: string;
    colors: string[];
    fonts: string[];
    slide_count: number;
    reference_count: number;
    analysis_mode: string;
    warning?: string | null;
  };
  error?: { message?: string } | null;
}
export interface WorkflowCapability {
  id: string;
  maturity: "stub" | "partial" | "candidate" | "complete" | "unavailable";
  executable: boolean;
  initial_interaction_policy: "never" | "explicit_only" | "review_only";
  issues: Array<{ code: string; message: string }>;
}
export interface Snapshot {
  session: { id: string; conversation_id: string; status: Status; workflow_id: string; prompt: string; error?: Record<string, unknown> };
  plan: { steps: Step[]; style_selection?: { style_id: string; mode: string; reason: string; design_version: string; design_hash: string } };
  versions: Version[];
  artifacts: Artifact[];
  events: Array<{ seq: number; type: string; payload: Record<string, unknown> }>;
}
