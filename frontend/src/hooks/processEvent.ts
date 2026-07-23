/** SSE event processor — maps agno events to timeline mutations.
 *
 * Given the timeline mutation callbacks and a small set of run/session
 * controllers, returns a single `processEvent` function that handles all
 * agno event types: reasoning, content, tool calls, errors.
 */

import type {
  Message,
  TimelineEntry,
  ToolCall,
  ReasoningSegment,
} from "../types";

export type StreamState = {
  isReasoningActive: boolean;
  reasoningSegCounter: number;
};

export function newStreamState(): StreamState {
  return { isReasoningActive: false, reasoningSegCounter: 0 };
}

export type ProcessEventDeps = {
  appendTimeline: (assistantId: number, entry: TimelineEntry) => void;
  appendContent: (assistantId: number, text: string) => void;
  patchTimelineTool: (
    assistantId: number,
    toolCallId: string,
    updates: Partial<ToolCall>,
  ) => void;
  updateAssistant: (assistantId: number, updates: Partial<Message>) => void;
  setMessages: React.Dispatch<React.SetStateAction<Message[]>>;
  updateSessionId: (id: string | null) => void;
  sessionIdRef: { current: string | null };
  activeRunIdRef: { current: string | null };
};

export function makeProcessEvent(deps: ProcessEventDeps) {
  const {
    appendTimeline,
    appendContent,
    patchTimelineTool,
    updateAssistant,
    setMessages,
    updateSessionId,
    sessionIdRef,
    activeRunIdRef,
  } = deps;

  // ── Reasoning segment helpers ──────────────────────────────

  function startReasoning(assistantId: number, state: StreamState, d: any) {
    state.isReasoningActive = true;
    state.reasoningSegCounter++;
    appendTimeline(assistantId, {
      kind: "reasoning",
      id: `reasoning-${state.reasoningSegCounter}`,
      content: "",
      active: true,
    });
  }

  function deactivateReasoning(assistantId: number, state: StreamState) {
    state.isReasoningActive = false;
    const segId = `reasoning-${state.reasoningSegCounter}`;
    setMessages((prev) =>
      prev.map((m) => {
        if (m.id !== assistantId) return m;
        const tl = (m.timeline || []).map((en) =>
          en.kind === "reasoning" && en.id === segId
            ? { ...en, active: false }
            : en,
        );
        return { ...m, timeline: tl };
      }),
    );
  }

  // ── Main event processor ────────────────────────────────────

  return function processEvent(
    d: any,
    eventType: string,
    assistantId: number,
    state: StreamState,
  ) {
    const e = eventType.replace(/^Team/, "");

    // Capture session_id
    if (
      !sessionIdRef.current &&
      typeof d.session_id === "string" &&
      d.session_id
    ) {
      updateSessionId(d.session_id);
    }

    // Capture run_id
    if (!activeRunIdRef.current && typeof d.run_id === "string" && d.run_id) {
      activeRunIdRef.current = d.run_id;
    }

    // ── Reasoning events ──────────────────────────────────

    if (e === "ReasoningStarted") {
      startReasoning(assistantId, state, d);
    } else if (e === "ReasoningContentDelta") {
      const delta = d.reasoning_content || "";
      if (delta) {
        appendTimeline(assistantId, {
          kind: "reasoning",
          id: `reasoning-${state.reasoningSegCounter}`,
          content: delta,
          active: true,
        });
      }
    } else if (e === "ReasoningStep") {
      const stepReasoning = d.reasoning_content || "";
      if (stepReasoning) {
        setMessages((prev) =>
          prev.map((m) => {
            if (m.id !== assistantId) return m;
            const tl = [...(m.timeline || [])];
            const segId = `reasoning-${state.reasoningSegCounter}`;
            const idx = tl.findIndex(
              (en) => en.kind === "reasoning" && en.id === segId,
            );
            if (idx !== -1) {
              const seg = tl[idx] as ReasoningSegment;
              if (stepReasoning.length > seg.content.length) {
                tl[idx] = {
                  ...seg,
                  content: stepReasoning,
                  active: true,
                };
              }
            }
            return { ...m, timeline: tl };
          }),
        );
      }
    } else if (e === "ReasoningCompleted") {
      deactivateReasoning(assistantId, state);
    }

    // ── Content events ────────────────────────────────────

    else if (e === "RunContent" || e === "RunIntermediateContent") {
      if (d.reasoning_content) {
        if (!state.isReasoningActive) {
          startReasoning(assistantId, state, d);
        }
        appendTimeline(assistantId, {
          kind: "reasoning",
          id: `reasoning-${state.reasoningSegCounter}`,
          content: d.reasoning_content,
          active: true,
        });
      } else if (state.isReasoningActive) {
        deactivateReasoning(assistantId, state);
      }

      if (d.content) {
        appendContent(assistantId, d.content);
      }
    }

    // ── Tool call events ──────────────────────────────────

    else if (
      e === "ToolCallStarted" ||
      e === "ToolCallCompleted" ||
      e === "ToolCallError" ||
      e === "ToolCallEvent"
    ) {
      const toolName =
        d.tool_name ||
        d.name ||
        d.tool?.tool_name ||
        d.tool?.function?.name ||
        d.function?.name ||
        "unknown";
      const rawArgs = d.tool_args ?? d.tool?.tool_args;
      const toolArgs = rawArgs
        ? typeof rawArgs === "string"
          ? rawArgs
          : JSON.stringify(rawArgs, null, 2)
        : undefined;
      const rawResult = d.result ?? d.tool_result ?? d.tool?.result;
      const result =
        rawResult != null
          ? (typeof rawResult === "string"
              ? rawResult
              : JSON.stringify(rawResult, null, 2)
            ).slice(0, 2000)
          : undefined;
      let content: string | undefined =
        d.content || d.tool?.content || undefined;
      if (content) {
        const idx = content.indexOf(" completed");
        if (idx !== -1) content = content.substring(0, idx);
      }
      const tcId = d.tool_call_id || d.id || d.tool?.tool_call_id;
      const duration =
        d.tool?.metrics?.duration ?? d.metrics?.duration ?? undefined;

      const isError =
        d.tool?.tool_call_error === true ||
        d.tool_call_error === true ||
        e === "ToolCallError";

      const isCompleted =
        e === "ToolCallCompleted" ||
        e === "ToolCallError" ||
        d.status === "completed";

      if (!isCompleted) {
        appendTimeline(assistantId, {
          kind: "tool",
          id: tcId || `tool-${Date.now()}`,
          tool: {
            name: toolName,
            args: toolArgs,
            status: "running",
            toolCallId: tcId,
            startedAt: Date.now(),
          },
        });
      } else if (tcId) {
        const completionUpdates: Partial<ToolCall> = {
          status: "done",
          args: toolArgs,
          result,
          content,
          duration,
          isError,
        };
        patchTimelineTool(assistantId, tcId, completionUpdates);
      }
    }

    // ── RunCompleted / RunCancelled ───────────────────────

    else if (e === "RunCompleted" || e === "RunCancelled") {
      // Capture token metrics from the completed run
      if (e === "RunCompleted" && d.metrics) {
        updateAssistant(assistantId, {
          metrics: {
            input_tokens: d.metrics.input_tokens,
            output_tokens: d.metrics.output_tokens,
          },
        });
      }
      activeRunIdRef.current = null;
    }

    // ── Error events ──────────────────────────────────────

    else if (e === "RunError" || e === "error") {
      const errorContent = d.content || d.error || "Unknown error";
      appendContent(assistantId, `\n\nERROR: ${errorContent}`);
      updateAssistant(assistantId, { role: "error" });
      activeRunIdRef.current = null;
    }
  };
}
