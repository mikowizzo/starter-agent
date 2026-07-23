/** useAgentStream — orchestrates SSE streaming for the chat.

 * Thin coordinator that wires together three extracted modules:
 *   - useMessages     → message/timeline state and mutation helpers
 *   - processEvent    → maps agno SSE events to timeline mutations
 *   - sse.ts          → raw stream reading
 *
 * This hook owns session/run lifecycle and send/stop flows.
 */

import { useState, useRef, useCallback } from "react";
import type { Message } from "../types";
import { agnoSessionId, userId, setAgnoSessionId } from "../lib/session";
import { runBase } from "../lib/api";
import { useMessages, MAX_MESSAGES } from "./useMessages";
import { makeProcessEvent, newStreamState } from "./processEvent";
import { readSSEStream } from "./sse";

export function useAgentStream() {
  const {
    messages,
    setMessages,
    updateAssistant,
    appendTimeline,
    appendContent,
    patchTimelineTool,
  } = useMessages();

  const [loading, setLoading] = useState(false);
  const [sessionId, setSessionIdState] = useState<string | null>(
    () => agnoSessionId,
  );

  // Keep localStorage in sync with reactive state
  const updateSessionId = useCallback((id: string | null) => {
    setAgnoSessionId(id);
    setSessionIdState(id);
  }, []);

  const abortRef = useRef<AbortController | null>(null);
  const activeRunIdRef = useRef<string | null>(null);
  // Refs that mirror reactive state, for use inside hot callbacks
  const sessionIdRef = useRef(sessionId);
  sessionIdRef.current = sessionId;

  // ── Event processor ──────────────────────────────────────

  const processEvent = useCallback(
    makeProcessEvent({
      appendTimeline,
      appendContent,
      patchTimelineTool,
      updateAssistant,
      setMessages,
      updateSessionId,
      sessionIdRef,
      activeRunIdRef,
    }),
    [
      appendTimeline,
      appendContent,
      patchTimelineTool,
      updateAssistant,
      setMessages,
      updateSessionId,
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

  // ── Stop run ─────────────────────────────────────────────

  const stopRun = useCallback(async () => {
    abortRef.current?.abort();
    const runId = activeRunIdRef.current;
    activeRunIdRef.current = null;
    setLoading(false);

    if (runId) {
      const params = new URLSearchParams();
      if (sessionIdRef.current) params.set("session_id", sessionIdRef.current);
      fetch(
        `${runBase()}/runs/${runId}/cancel${params.toString() ? `?${params}` : ""}`,
        { method: "POST" },
      ).catch(() => {});
    }
  }, []);

  // ── Send message ─────────────────────────────────────────

  const send = useCallback(
    async (text: string) => {
      if (!text.trim() || loading) return;

      const msg = text.trim();
      const userMsg: Message = {
        id: Date.now(),
        role: "user",
        content: msg,
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

      try {
        const form = new FormData();
        form.append("message", msg);
        form.append("user_id", userId || "anonymous");
        if (sessionIdRef.current)
          form.append("session_id", sessionIdRef.current);
        form.append("stream", "true");
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
        if (err.name !== "AbortError") {
          updateAssistant(assistantId, {
            role: "error",
            content: `Connection error: ${err.message}`,
          });
          activeRunIdRef.current = null;
        }
      } finally {
        setLoading(false);
        abortRef.current = null;
      }
    },
    [loading, readStream, updateAssistant, setMessages],
  );

  return {
    messages,
    loading,
    send,
    stopRun,
    setMessages,
    sessionId,
    setSessionId: updateSessionId,
  };
}
