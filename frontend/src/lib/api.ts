import type { Message } from "../types";

// ── Agent ID (fetched from backend at startup) ────────────────────

let _agentId: string | null = null;

export async function fetchTeamId(): Promise<string> {
  if (_agentId) return _agentId;
  const res = await fetch("/teams");
  if (!res.ok) throw new Error("No team found");
  const list = await res.json();
  const first = list?.[0];
  if (!first?.id) throw new Error("No team found");
  _agentId = first.id;
  return first.id;
}

export function getTeamId(): string {
  if (!_agentId) throw new Error("Team ID not loaded – call fetchTeamId() first");
  return _agentId;
}

export function runBase(): string {
  return `/teams/${getTeamId()}`;
}

// ── Session history ──────────────────────────────────────────────

export interface SessionListItem {
  session_id: string;
  session_name: string;
  session_type: string;
  created_at: string;
  updated_at: string;
}

export async function fetchSessions(limit = 10): Promise<SessionListItem[]> {
  try {
    const res = await fetch(`/sessions?limit=${limit}`);
    if (!res.ok) return [];
    const data = await res.json();
    return data?.data ?? [];
  } catch {
    return [];
  }
}

// Deterministic ID for history messages, derived from the backend's own
// message id. Negative numbers can never collide with Date.now()-based IDs
// assigned to live-streamed messages, so React keys stay unique even when a
// run's seeded messages are appended onto a loaded history.
function historyMessageId(sessionId: string, msgId: unknown): number {
  const key = `${sessionId}:${String(msgId)}`;
  let h = 0;
  for (let i = 0; i < key.length; i++) {
    h = (h * 31 + key.charCodeAt(i)) | 0;
  }
  return -(Math.abs(h) % 2_000_000_000) - 1;
}

export async function loadSessionHistory(sessionId: string): Promise<Message[]> {
  try {
    const res = await fetch(`/sessions/${sessionId}?type=agent`);
    if (!res.ok) return [];
    const session = await res.json();
    const messages: Message[] = [];
    if (session.chat_history && Array.isArray(session.chat_history)) {
      for (const msg of session.chat_history) {
        if (msg.role === "user" || msg.role === "assistant") {
          // History includes assistant messages that only contain tool calls
          // (content === null). Skip them so they don't crash the renderer or
          // render as blank bubbles — the following assistant message carries
          // the actual answer.
          if (msg.content == null) continue;
          messages.push({
            id: historyMessageId(sessionId, msg.id),
            role: msg.role,
            content: msg.content,
          });
        }
      }
    }
    return messages;
  } catch {
    return [];
  }
}

// ── Models ───────────────────────────────────────────────────────

export interface ModelInfo {
  current: string;
  id: string;
  name: string;
  provider: string;
}

export interface ModelOption {
  key: string;
  id: string;
  name: string;
  provider: string;
}

export async function fetchModels(): Promise<Record<string, ModelOption>> {
  try {
    const res = await fetch("/settings/models");
    if (!res.ok) return {};
    const data = await res.json();
    return data?.models ?? {};
  } catch {
    return {};
  }
}

export async function fetchModel(): Promise<ModelInfo | null> {
  try {
    const res = await fetch("/settings/model");
    if (!res.ok) return null;
    return await res.json();
  } catch {
    return null;
  }
}

export async function setModel(model: string): Promise<ModelInfo | null> {
  try {
    const res = await fetch("/settings/model", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model }),
    });
    if (!res.ok) return null;
    return await res.json();
  } catch {
    return null;
  }
}

// ── Quota ────────────────────────────────────────────────────────

export interface QuotaInfo {
  percentage: number | null;
  reset_at: number | null;
  error?: string;
}

export async function fetchQuota(): Promise<QuotaInfo | null> {
  try {
    const res = await fetch("/providers/zai/quota");
    if (!res.ok) return null;
    return await res.json();
  } catch {
    return null;
  }
}

export async function fetchSyntheticQuota(): Promise<QuotaInfo | null> {
  try {
    const res = await fetch("/providers/synthetic/quota");
    if (!res.ok) return null;
    return await res.json();
  } catch {
    return null;
  }
}

// ── Instance switcher (parent + clones) ──────────────────────────

export interface CloneInfo {
  name: string;
  ports: { backend: number; frontend: number };
  status: string;
}

export async function fetchClones(): Promise<{
  self_name: string;
  self_port: number;
  parent: { name: string; frontend_port: number } | null;
  clones: CloneInfo[];
} | null> {
  try {
    const res = await fetch("/settings/clones");
    if (!res.ok) return null;
    return await res.json();
  } catch {
    return null;
  }
}

/** Active background-run count per running clone (from our registry). */
export async function fetchCloneRunCounts(): Promise<Record<string, number>> {
  try {
    const res = await fetch("/runs/clone-counts");
    if (!res.ok) return {};
    const body = await res.json();
    return body?.data ?? {};
  } catch {
    return {};
  }
}

// ── Active runs (cross-session visibility) ────────────────────────

export interface ActiveRunInfo {
  run_id: string;
  session_id: string | null;
  status: string;
  created_at: string | null;
  last_updated: string | null;
  event_count: number | null;
  last_event_index: number | null;
  input_preview: string | null;
  /** false = persisted RUNNING but the process died (backend restart); not resumable */
  live: boolean;
}

export async function fetchActiveRuns(): Promise<ActiveRunInfo[]> {
  try {
    const res = await fetch("/runs/active");
    if (!res.ok) return [];
    const data = await res.json();
    return data?.data ?? [];
  } catch {
    return [];
  }
}

