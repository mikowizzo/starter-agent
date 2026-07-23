/** Message and timeline state management.
 *
 * Provides setMessages plus mutation helpers for appending content,
 * reasoning segments, and tool-call entries to assistant messages.
 * All helpers are pure setMessages wrappers — no side effects.
 */

import { useCallback, useState } from "react";
import type { Message, TimelineEntry, ToolCall } from "../types";
import { appendToTimeline, updateToolInTimeline } from "../types";

const MAX_MESSAGES = 200;

export function useMessages() {
  const [messages, setMessages] = useState<Message[]>([]);

  const updateAssistant = useCallback(
    (assistantId: number, updates: Partial<Message>) => {
      setMessages((prev) =>
        prev.map((m) => (m.id === assistantId ? { ...m, ...updates } : m)),
      );
    },
    [],
  );

  const appendTimeline = useCallback(
    (assistantId: number, entry: TimelineEntry) => {
      setMessages((prev) =>
        prev.map((m) =>
          m.id === assistantId
            ? { ...m, timeline: appendToTimeline(m.timeline || [], entry) }
            : m,
        ),
      );
    },
    [],
  );

  const appendContent = useCallback(
    (assistantId: number, text: string) => {
      appendTimeline(assistantId, {
        kind: "content",
        id: "content",
        content: text,
      });
    },
    [appendTimeline],
  );

  const patchTimelineTool = useCallback(
    (assistantId: number, toolCallId: string, updates: Partial<ToolCall>) => {
      setMessages((prev) =>
        prev.map((m) =>
          m.id === assistantId
            ? {
                ...m,
                timeline: updateToolInTimeline(
                  m.timeline || [],
                  toolCallId,
                  updates,
                ),
              }
            : m,
        ),
      );
    },
    [],
  );

  return {
    messages,
    setMessages,
    updateAssistant,
    appendTimeline,
    appendContent,
    patchTimelineTool,
  };
}

export { MAX_MESSAGES };
