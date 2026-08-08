/** useAgentStream — orchestrates SSE streaming for the chat.

 * Thin coordinator that wires together three extracted modules:
 *   - useMessages     → message/timeline state and mutation helpers
 *   - processEvent    → maps agno SSE events to timeline mutations
 *   - sse.ts          → raw stream reading
 *
 * This hook owns session/run lifecycle and send/stop flows, including
 * reconnection: runs are started in background mode and their state is
 * persisted to localStorage so a browser refresh can resume them.
 */

import { useState, useRef, useCallback, useEffect } from "react";
import type { Attachment, Message } from "../types";
import {
  agnoSessionId,
  userId,
  getActiveRun,
  setActiveRun,
  setAgnoSessionId,
} from "../lib/session";
import type { ActiveRun } from "../lib/session";
import { runBase } from "../lib/api";
import { useMessages, MAX_MESSAGES } from "./useMessages";
import { makeProcessEvent, newStreamState } from "./processEvent";
import { readSSEStream } from "./sse";

const RESUME_MAX_RETRIES = 2;
const RESUME_DELAY_MS = 2000; // flat delay between retries

/** UUID with a fallback for non-secure contexts (LAN IP, older browsers)
 *  where crypto.randomUUID is unavailable and would throw. */
function uuid(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    const v = c === "x" ? r : (r & 0x3) | 0x8;
    return v.toString(16);
  });
}
// ── Attachment upload / poll helpers ──────────────────────────────
const POLL_INTERVAL_MS = 500;
const POLL_TIMEOUT_MS = 30_000;

async function uploadFiles(
  files: File[],
  sessionId: string,
): Promise<Attachment[]> {
  const form = new FormData();
  files.forEach((f) => form.append("files", f));
  form.append("session_id", sessionId);
  const res = await fetch("/attachments", { method: "POST", body: form });
  if (!res.ok) throw new Error(`upload failed: ${res.status}`);
  const data = await res.json();
  return (data?.attachments ?? []) as Attachment[];
}

async function waitForAttachment(a: Attachment): Promise<Attachment> {
  const deadline = Date.now() + POLL_TIMEOUT_MS;
  let cur = a;
  while (cur.status === "pending" || cur.status === "processing") {
    if (Date.now() > deadline)
      return { ...cur, status: "failed", error: "processing timed out" };
    await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS));
    const res = await fetch(`/attachments/${cur.id}`);
    if (!res.ok) break;
    cur = (await res.json()) as Attachment;
  }
  return cur;
}
function failAssistant(
  id: number,
  content: string,
  updateAssistant: (id: number, patch: Partial<Message>) => void,
  clearRun: () => void,
) {
  updateAssistant(id, { role: "error", content });
  clearRun();
}

export function useAgentStream() {
  const {
    messages,
    setMessages,
    updateAssistant,
    appendTimeline,
    appendContent,
    patchTimelineTool,
  } = useMessages();

  const [loading, setLoading] = useState(() => !!getActiveRun());
  const [activeRun, setActiveRunState] = useState<ActiveRun | null>(() =>
    getActiveRun(),
  );
  const [sessionId, setSessionId] = useState<string | null>(
    () => agnoSessionId,
  );

  // Keep localStorage in sync with reactive state
  const updateSessionId = useCallback((id: string | null) => {
    setAgnoSessionId(id);
    setSessionId(id);
  }, []);

  const updateActiveRun = useCallback((run: ActiveRun | null) => {
    setActiveRun(run);
    setActiveRunState(run);
  }, []);

  const abortRef = useRef<AbortController | null>(null);
  const activeRunIdRef = useRef<string | null>(null);
  const currentUserMsg = useRef("");

  // Refs that mirror reactive state, for use inside hot callbacks
  const sessionIdRef = useRef(sessionId);
  sessionIdRef.current = sessionId;

  useEffect(() => {
    const saved = getActiveRun();
    if (saved) {
      activeRunIdRef.current = saved.runId;
    }
  }, []);

  // ── Clear run state ──────────────────────────────────────

  const clearRun = useCallback(() => {
    updateActiveRun(null);
    activeRunIdRef.current = null;
  }, [updateActiveRun]);

  // ── Event processor ──────────────────────────────────────

  const processEvent = useCallback(
    makeProcessEvent({
      appendTimeline,
      appendContent,
      patchTimelineTool,
      updateAssistant,
      setMessages,
      updateSessionId,
      updateActiveRun,
      sessionIdRef,
      activeRunIdRef,
      currentUserMsg,
    }),
    [
      appendTimeline,
      appendContent,
      patchTimelineTool,
      updateAssistant,
      setMessages,
      updateSessionId,
      updateActiveRun,
    ],
  );

  // ── SSE reader ───────────────────────────────────────────

  const readStream = useCallback(
    (
      reader: ReadableStreamDefaultReader<Uint8Array>,
      assistantId: number,
      state: ReturnType<typeof newStreamState>,
    ) =>
      readSSEStream(reader, (d, eventType) =>
        processEvent(d, eventType, assistantId, state),
      ),
    [processEvent],
  );

  // ── Resume with automatic retry ──────────────────────────

  const resumeWithRetry = useCallback(
    async (
      assistantId: number,
      state: ReturnType<typeof newStreamState>,
      maxRetries = RESUME_MAX_RETRIES,
    ): Promise<boolean> => {
      for (let attempt = 0; attempt < maxRetries; attempt++) {
        const saved = getActiveRun();
        if (!saved || !saved.runId) return false;

        if (attempt > 0) {
          await new Promise((r) => setTimeout(r, RESUME_DELAY_MS));
        }

        try {
          const form = new FormData();
          form.append("last_event_index", String(saved.lastEventIndex));
          if (saved.sessionId) form.append("session_id", saved.sessionId);

          const ac = new AbortController();
          abortRef.current = ac;

          const res = await fetch(`${runBase()}/runs/${saved.runId}/resume`, {
            method: "POST",
            body: form,
            signal: ac.signal,
          });

          if (!res.ok || !res.body) return false;

          await readStream(res.body.getReader(), assistantId, state);
          return true;
        } catch (err: any) {
          if (err.name === "AbortError") throw err;
        }
      }
      return false;
    },
    [readStream],
  );

  // ── Stop run ─────────────────────────────────────────────

  const stopRun = useCallback(async () => {
    abortRef.current?.abort();
    const runId = activeRunIdRef.current;
    clearRun();
    setLoading(false);

    if (runId) {
      const params = new URLSearchParams();
      if (sessionIdRef.current) params.set("session_id", sessionIdRef.current);
      fetch(
        `${runBase()}/runs/${runId}/cancel${params.toString() ? `?${params}` : ""}`,
        { method: "POST" },
      ).catch(() => {});
    }
  }, [clearRun]);

  // ── Send message ─────────────────────────────────────────

  const send = useCallback(
    async (text: string, files?: File[]) => {
      if ((!text.trim() && !files?.length) || loading) return;

      const msg = text.trim();

      // ── Attachments: upload -> poll -> server-side assemble ─────────
      // The backend stores the raw file, extracts text in the background,
      // and builds a token-budgeted <attachments> block (inline / excerpt /
      // reference / failed-with-path). The exact block is appended to the
      // message so the model sees real content (or an explicit pointer) and
      // the snapshot is persisted server-side for deterministic replay.
      let fullMessage = msg;
      let attachmentIds: string[] = [];
      const attachmentMeta = files?.map((f) => ({ name: f.name, size: f.size }));

      if (files?.length) {
        // Make sure we have a session id before uploading (attachments are
        // session-scoped). Client-generated uuids are compatible with agno.
        if (!sessionIdRef.current) {
          const id = uuid();
          sessionIdRef.current = id;
          updateSessionId(id);
        }
        const sid = sessionIdRef.current;

        try {
          const uploaded = await uploadFiles(files, sid);
          const settled = await Promise.all(uploaded.map(waitForAttachment));
          attachmentIds = settled.map((a) => a.id);

          const messageId = uuid();
          const assembleRes = await fetch("/attachments/assemble", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              message_id: messageId,
              attachment_ids: attachmentIds,
              session_id: sid,
            }),
          });
          if (assembleRes.ok) {
            const data = await assembleRes.json();
            if (data.text) fullMessage = `${msg}\n\n${data.text}`.trim();
          }
        } catch {
          // Upload/assemble pipeline failed — fall back to a plain stub so
          // the agent at least knows files were attached (legacy behavior).
          const names = files.map((f) => f.name).join(", ");
          fullMessage = `${msg}\n\n--- **Attached files (conversion unavailable): ${names}** ---\n`.trim();
        }
      }

      currentUserMsg.current = msg;

      const userMsg: Message = {
        id: Date.now(),
        role: "user",
        content: msg,
        attachments: attachmentMeta,
      };
      setMessages((prev) => [...prev, userMsg].slice(-MAX_MESSAGES));

      setLoading(true);
      const ac = new AbortController();
      abortRef.current = ac;
      activeRunIdRef.current = null;

      const assistantId = Date.now() + 1;
      const assistantMsg: Message = {
        id: assistantId,
        role: "assistant",
        content: "",
        timeline: [],
      };
      setMessages((prev) => [...prev, assistantMsg].slice(-MAX_MESSAGES));

      const state = newStreamState();

      const fail = (content: string) =>
        failAssistant(assistantId, content, updateAssistant, clearRun);

      try {
        const form = new FormData();
        form.append("message", fullMessage);
        form.append("user_id", userId || "anonymous");
        if (sessionIdRef.current)
          form.append("session_id", sessionIdRef.current);
        if (attachmentIds.length)
          form.append("attachment_ids", JSON.stringify(attachmentIds));
        form.append("stream", "true");
        // Run in background mode: the team keeps executing even if the
        // SSE connection drops (browser refresh), and can be resumed.
        form.append("background", "true");

        const res = await fetch(`${runBase()}/runs`, {
          method: "POST",
          body: form,
          signal: ac.signal,
        });

        if (!res.ok) {
          updateAssistant(assistantId, {
            role: "error",
            content: `Error: ${res.status} ${res.statusText}`,
          });
          setLoading(false);
          return;
        }

        await readStream(res.body!.getReader(), assistantId, state);
      } catch (err: any) {
        if (err.name === "AbortError") {
          // Background runs survive disconnect
        } else {
          try {
            const recovered = await resumeWithRetry(assistantId, state);
            if (!recovered) {
              fail("Connection lost and could not reconnect to the run.");
            }
          } catch (retryErr: any) {
            if (retryErr.name !== "AbortError") {
              fail("Connection lost and could not reconnect to the run.");
            }
          }
        }
      } finally {
        setLoading(false);
        abortRef.current = null;
      }
    },
    [
      loading,
      readStream,
      resumeWithRetry,
      updateAssistant,
      updateSessionId,
      clearRun,
      setMessages,
    ],
  );
  // ── Reconnect (page load / tab reopen) ───────────────────

  const reconnect = useCallback(async () => {
    const saved = getActiveRun();
    if (!saved) return;

    const { sessionId: savedSessionId, userMessage } = saved;

    if (!sessionIdRef.current && savedSessionId) {
      updateSessionId(savedSessionId);
    }

    activeRunIdRef.current = saved.runId;
    setLoading(true);

    const assistantId = Date.now() + 1;
    setMessages([
      { id: Date.now(), role: "user", content: userMessage },
      { id: assistantId, role: "assistant", content: "", timeline: [] },
    ]);

    const state = newStreamState();

    try {
      const recovered = await resumeWithRetry(assistantId, state);
      if (!recovered) {
        failAssistant(
          assistantId,
          "Could not reconnect to run — it may have completed or expired.",
          updateAssistant,
          clearRun,
        );
      }
    } catch (err: any) {
      if (err.name !== "AbortError") {
        failAssistant(
          assistantId,
          `Reconnect error: ${err.message}`,
          updateAssistant,
          clearRun,
        );
      }
    } finally {
      setLoading(false);
      abortRef.current = null;
    }
  }, [
    resumeWithRetry,
    updateAssistant,
    updateSessionId,
    clearRun,
    setMessages,
  ]);

  useEffect(() => {
    const saved = getActiveRun();
    if (saved && saved.runId) {
      reconnect();
    }
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  return {
    messages,
    loading,
    send,
    stopRun,
    setMessages,
    activeRun,
    sessionId,
    setSessionId: updateSessionId,
  };
}
