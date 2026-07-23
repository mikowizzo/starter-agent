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

export async function loadSessionHistory(sessionId: string): Promise<Message[]> {
  try {
    const res = await fetch(`/sessions/${sessionId}?type=agent`);
    if (!res.ok) return [];
    const session = await res.json();
    const messages: Message[] = [];
    if (session.chat_history && Array.isArray(session.chat_history)) {
      for (const msg of session.chat_history) {
        if (msg.role === "user" || msg.role === "assistant") {
          messages.push({
            id: Date.now() + messages.length,
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
