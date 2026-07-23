// ── Timeline entries (ordered by arrival) ───────────────────────────

export interface ReasoningSegment {
  kind: "reasoning";
  /** Stable id for expand/collapse tracking */
  id: string;
  content: string;
  /** True while this segment is still receiving deltas */
  active: boolean;
}

export interface ToolSegment {
  kind: "tool";
  id: string;
  tool: ToolCall;
}

export interface ContentSegment {
  kind: "content";
  id: string;
  content: string;
}

export type TimelineEntry =
  | ReasoningSegment
  | ToolSegment
  | ContentSegment;

// ── Tool call ───────────────────────────────────────────────────────

export interface ToolCall {
  name: string;
  args?: string;
  result?: string;
  status: "running" | "done";
  toolCallId?: string;
  content?: string;
  duration?: number;
  isError?: boolean;
  startedAt?: number;
}

// ── Message ─────────────────────────────────────────────────────────

export interface MessageMetrics {
  input_tokens?: number;
  output_tokens?: number;
}

export interface Message {
  id: number;
  role: "user" | "assistant" | "error";
  /** Plain text content (used for user messages and legacy/error content) */
  content: string;
  /** Ordered timeline of reasoning, tool calls, and content segments */
  timeline?: TimelineEntry[];
  /** Token usage metrics from the completed run */
  metrics?: MessageMetrics;
}

// ── Helpers ─────────────────────────────────────────────────────────

/**
 * Push-or-merge a segment into the timeline.
 *
 * - Reasoning deltas merge into the tail if it's also an active reasoning
 *   segment; otherwise a new segment is pushed.
 * - Content deltas merge into the tail if it's also a content segment.
 * - Tool calls always get their own entry (found by id for completion).
 */
export function appendToTimeline(
  timeline: TimelineEntry[],
  entry: TimelineEntry,
): TimelineEntry[] {
  const next = [...timeline];
  const last = next[next.length - 1];

  if (entry.kind === "reasoning" && last?.kind === "reasoning" && last.active) {
    // Merge into the active reasoning segment at the tail
    next[next.length - 1] = {
      ...last,
      content: last.content + entry.content,
      active: entry.active,
    };
  } else if (entry.kind === "content" && last?.kind === "content") {
    // Merge into the tail content segment
    next[next.length - 1] = {
      ...last,
      content: last.content + entry.content,
    };
  } else {
    next.push(entry);
  }

  return next;
}

/**
 * Find a tool entry by toolCallId and update it in-place.
 */
export function updateToolInTimeline(
  timeline: TimelineEntry[],
  toolCallId: string,
  updates: Partial<ToolCall>,
): TimelineEntry[] {
  return timeline.map((entry) =>
    entry.kind === "tool" && entry.id === toolCallId
      ? { ...entry, tool: { ...entry.tool, ...updates } }
      : entry,
  );
}
