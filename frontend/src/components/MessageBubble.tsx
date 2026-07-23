import { useRef, useState, useEffect, memo, type ReactNode } from "react";
import { MarkdownRenderer } from "./MarkdownRenderer";
import {
  ChevronRight,
  Wrench,
  Loader2,
  Brain,
  Check,
  Copy,
} from "lucide-react";
import type {
  Message,
  TimelineEntry,
  ReasoningSegment,
  ToolSegment,
} from "../types";

// ── Helpers ──────────────────────────────────────────────────────

export const isVisible = (m: Message) =>
  m.role === "user" || !!m.content || (m.timeline?.length ?? 0) > 0;

export const isActive = (m: Message) =>
  m.timeline?.some(
    (e) =>
      (e.kind === "reasoning" && e.active) ||
      (e.kind === "tool" && e.tool.status === "running"),
  ) ?? false;

const fmtDur = (s: number) =>
  s < 1 ? `${Math.round(s * 1000)}ms` : `${s.toFixed(1)}s`;

const prettyJson = (raw: string) => {
  try {
    return JSON.stringify(JSON.parse(raw), null, 2);
  } catch {
    return raw;
  }
};

/**
 * Build a human-friendly display label for a tool call based on its name and args.
 * For skill-related tools, extracts the skill name (and script path) from the args.
 */
const toolDisplayLabel = (name: string, rawArgs: string) => {
  if (name === "get_skill_instructions") {
    try {
      const { skill_name } = JSON.parse(rawArgs);
      if (skill_name) return `get_skill_instructions(${skill_name})`;
    } catch {
      // fall through
    }
  }
  if (name === "get_skill_script") {
    try {
      const { skill_name, script_path } = JSON.parse(rawArgs);
      if (skill_name && script_path)
        return `get_skill_script(${skill_name}, ${script_path})`;
    } catch {
      // fall through
    }
  }
  return name;
};

/** Compact one-line-per-param format: `key: value` without outer braces. */
const compactArgs = (raw: string) => {
  try {
    const parsed = JSON.parse(raw);
    return Object.entries(parsed)
      .map(([k, v]) => {
        const val = typeof v === "string" ? v : JSON.stringify(v);
        const truncated = val.length > 100 ? val.slice(0, 97) + "…" : val;
        return `${k}: ${truncated}`;
      })
      .join("\n");
  } catch {
    return raw;
  }
};

// ── ThinkingDots ─────────────────────────────────────────────────

export function ThinkingDots() {
  return (
    <div className="flex justify-start py-3 px-2">
      <div className="flex items-center gap-1">
        <span className="text-xs text-[var(--color-dim)]">Thinking</span>
        <div className="h-1.5 w-1.5 animate-pulse rounded-full bg-[var(--color-dim)] ml-1.5" />
        <div className="h-1.5 w-1.5 animate-pulse rounded-full bg-[var(--color-dim)] [animation-delay:120ms]" />
        <div className="h-1.5 w-1.5 animate-pulse rounded-full bg-[var(--color-dim)] [animation-delay:240ms]" />
      </div>
    </div>
  );
}

// ── CollapsibleCard (shared shell) ───────────────────────────────

function CollapsibleCard({
  expanded,
  onToggle,
  headerClassName = "",
  header,
  children,
}: {
  expanded: boolean;
  onToggle: () => void;
  headerClassName?: string;
  header: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="mb-1.5">
      <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-panel)] overflow-hidden">
        <div
          onClick={onToggle}
          className={`flex items-center gap-2 px-3 py-2 text-xs cursor-pointer select-none ${headerClassName}`}
        >
          <ChevronRight
            className={`h-3 w-3 shrink-0 text-[var(--color-dim)] transition-transform duration-150 ${expanded ? "rotate-90" : ""}`}
          />
          {header}
        </div>
        {expanded && children}
      </div>
    </div>
  );
}

// ── Segment views ────────────────────────────────────────────────

function ReasoningSegmentView({ seg }: { seg: ReasoningSegment }) {
  const [expanded, setExpanded] = useState(true);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (expanded && seg.active && ref.current)
      ref.current.scrollTop = ref.current.scrollHeight;
  }, [expanded, seg.content, seg.active]);

  return (
    <CollapsibleCard
      expanded={expanded}
      onToggle={() => setExpanded((v) => !v)}
      header={
        <>
          <Brain className="h-3.5 w-3.5 shrink-0 text-[var(--color-accent)]" />
          <span className="truncate text-white">Reasoning</span>
          {seg.content && (
            <span className="ml-auto text-[10px] text-[var(--color-dim)] tabular-nums shrink-0">
              {seg.content.length > 1000
                ? `${Math.round(seg.content.length / 1000)}k`
                : `${seg.content.length}`}
            </span>
          )}
        </>
      }
    >
      <div className="px-3 py-2 text-xs border-t border-[var(--color-border)]">
        <div
          ref={ref}
          className="whitespace-pre-wrap break-words text-[var(--color-dim)] max-h-64 overflow-y-auto scrollbar-thin prose prose-invert prose-sm prose-p:my-1 prose-headings:my-1.5 prose-headings:text-pink-300"
        >
          <MarkdownRenderer>{seg.content}</MarkdownRenderer>
        </div>
      </div>
    </CollapsibleCard>
  );
}

function ToolSegmentView({ seg }: { seg: ToolSegment }) {
  const tc = seg.tool;
  const isRunning = tc.status === "running";
  const [userExpanded, setUserExpanded] = useState(true);
  const expanded = userExpanded || isRunning;

  const [elapsed, setElapsed] = useState(0);
  useEffect(() => {
    if (!isRunning || !tc.startedAt) return;
    const tick = () => setElapsed((Date.now() - tc.startedAt!) / 1000);
    tick();
    const id = setInterval(tick, 100);
    return () => clearInterval(id);
  }, [isRunning, tc.startedAt]);

  return (
    <CollapsibleCard
      expanded={expanded && (!!tc.args || !!tc.result || isRunning)}
      onToggle={() => setUserExpanded((v) => !v)}
      headerClassName={tc.isError ? "text-red-400" : ""}
      header={
        <>
          {isRunning ? (
            <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin text-[var(--color-dim)]" />
          ) : (
            <Wrench
              className={`h-3.5 w-3.5 shrink-0 ${tc.isError ? "text-red-400" : "text-[var(--color-accent)]"}`}
            />
          )}
          <span
            className={`truncate ${tc.isError ? "text-red-400" : "text-[var(--color-text)]"}`}
          >
            {toolDisplayLabel(tc.name, tc.args || "")}
          </span>
          {isRunning && (
            <span className="animate-pulse text-[var(--color-dim)] shrink-0 tabular-nums">
              {tc.startedAt ? fmtDur(elapsed) : "running…"}
            </span>
          )}
          {!isRunning && tc.duration != null && (
            <span className="ml-auto text-[10px] text-[var(--color-dim)] tabular-nums shrink-0">
              {fmtDur(tc.duration)}
            </span>
          )}
        </>
      }
    >
      <div
        className={`px-3 py-2 text-xs space-y-2 ${tc.isError ? "text-red-400" : ""}`}
      >
        {tc.args && (
          <div>
            <div className="text-[10px] uppercase tracking-wider text-[var(--color-dim)] mb-0.5">
              Input
            </div>
            <pre className="rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-2 py-1.5 whitespace-pre-wrap break-all text-[var(--color-dim)] max-h-24 overflow-y-auto scrollbar-thin font-mono text-[11px] leading-relaxed">
              {compactArgs(tc.args)}
            </pre>
          </div>
        )}
        {tc.result && (
          <div>
            <div
              className={`text-[10px] uppercase tracking-wider mb-0.5 ${tc.isError ? "text-red-400" : "text-[var(--color-dim)]"}`}
            >
              Output
            </div>
            <pre
              className={`rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-2 py-1.5 whitespace-pre-wrap break-words max-h-48 overflow-y-auto scrollbar-thin font-mono text-[11px] leading-relaxed ${tc.isError ? "border-red-500/30 text-red-400" : "text-[var(--color-dim)]"}`}
            >
              {prettyJson(tc.result)}
            </pre>
          </div>
        )}
      </div>
    </CollapsibleCard>
  );
}

// ── TimelineView ─────────────────────────────────────────────────

function TimelineView({ timeline }: { timeline: TimelineEntry[] }) {
  return (
    <>
      {timeline.map((entry, i) => {
        switch (entry.kind) {
          case "reasoning":
            return <ReasoningSegmentView key={entry.id} seg={entry} />;
          case "tool":
            return <ToolSegmentView key={entry.id} seg={entry} />;
          case "content":
            return (
              <div key={`content-${i}`} className="prose prose-invert">
                <MarkdownRenderer>{entry.content}</MarkdownRenderer>
              </div>
            );
          default:
            return null;
        }
      })}
    </>
  );
}

// ── ActionButtons ─────────────────────────────────────────────

function ActionButtons({
  msg,
  isLast,
  isLastUser,
}: {
  msg: Message;
  isLast: boolean;
  isLastUser: boolean;
}) {
  const [copied, setCopied] = useState(false);

  async function handleCopy() {
    let text = msg.content;
    if (msg.timeline) {
      const tlText = msg.timeline
        .filter((e) => e.kind === "content")
        .map((e) => (e as any).content)
        .join("");
      if (tlText) text = tlText;
    }
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      /* ignore */
    }
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }

  const btnClass =
    "flex h-7 w-7 items-center justify-center rounded-md text-[var(--color-dim)] hover:bg-[var(--color-border)] hover:text-[var(--color-text)] transition active:scale-90";

  const metrics = msg.metrics;
  const hasMetrics = metrics && (metrics.input_tokens || metrics.output_tokens);

  return (
    <div
      className={`flex items-center gap-0.5 pt-1 ${msg.role === "user" ? "justify-end" : ""}`}
    >
      {hasMetrics && (
        <span
          className="mr-1.5 select-none text-[10px] text-[var(--color-dim)] tabular-nums"
          title="Token usage"
        >
          ↑{metrics.input_tokens?.toLocaleString() ?? "?"} ↓
          {metrics.output_tokens?.toLocaleString() ?? "?"}
        </span>
      )}
      <button onClick={handleCopy} className={btnClass} title="Copy">
        {copied ? (
          <Check className="h-3.5 w-3.5 text-emerald-400" />
        ) : (
          <Copy className="h-3.5 w-3.5" />
        )}
      </button>
    </div>
  );
}

// ── MessageBubble ────────────────────────────────────────────

const bubbleStyles: Record<string, string> = {
  user: "max-w-[82%] rounded-3xl px-5 py-3 bg-[var(--color-user)] text-white shadow-sm whitespace-pre-wrap",
  error:
    "w-full rounded-2xl px-5 py-3 border border-red-900/60 bg-red-950/70 text-red-400",
  assistant: "w-full text-[var(--color-text)]",
};

export const MessageBubble = memo(
  function MessageBubble({
    msg,
    running,
    isLast,
    isLastUser,
  }: {
    msg: Message;
    running: boolean;
    isLast?: boolean;
    isLastUser?: boolean;
  }) {
    const hasTimeline = msg.timeline && msg.timeline.length > 0;
    const showActions = !running;

    const showAvatar = msg.role === "assistant";
    return (
      <div
        className={`flex flex-col ${msg.role === "user" ? "items-end" : "items-start"} ${showAvatar ? "gap-2" : ""}`}
      >
        {showAvatar && (
          <img
            src="/avatar.png"
            alt="AI"
            className="w-full max-w-[144px] h-auto rounded-2xl object-cover ring-1 ring-[var(--color-border)] shadow-md"
          />
        )}
        <div
          className={`flex w-full min-w-0 flex-col ${msg.role === "user" ? "items-end" : "items-start"}`}
        >
          <div
            className={`text-[15px] leading-relaxed transition-all ${bubbleStyles[msg.role]}`}
          >
          {hasTimeline ? (
            <TimelineView timeline={msg.timeline!} />
          ) : msg.role === "user" ? (
            msg.content
          ) : (
            <div className="prose prose-invert">
              <MarkdownRenderer>{msg.content}</MarkdownRenderer>
            </div>
          )}
        </div>

        {showActions && msg.role !== "error" && (
          <ActionButtons
            msg={msg}
            isLast={!!isLast}
            isLastUser={!!isLastUser}
          />
        )}
        </div>
      </div>
    );
  },
  (prev, next) =>
    prev.msg === next.msg &&
    prev.running === next.running &&
    prev.isLast === next.isLast &&
    prev.isLastUser === next.isLastUser,
);
