import { useState, useRef, useEffect, useLayoutEffect, useCallback } from "react";
import { Loader2, ChevronDown } from "lucide-react";
import { InputBar } from "./components/InputBar";
import { BottomBar } from "./components/BottomBar";
import { FileExplorer } from "./components/FileExplorer";
import {
  MessageBubble,
  ThinkingDots,
  isVisible,
  isActive,
} from "./components/MessageBubble";
import { useAgentStream } from "./hooks/useAgentStream";
import { useActiveRuns } from "./hooks/useActiveRuns";
import { loadSessionHistory, fetchTeamId } from "./lib/api";
import { isRunStopping } from "./lib/session";

// ── Backend-ready gate ──────────────────────────────────────────────
// Polls /health until the backend responds, then renders the app.
// Plain English: don't load the chat UI until the API server is awake.

function useBackendReady() {
  const [ready, setReady] = useState(false);

  useEffect(() => {
    let cancelled = false;

    async function poll() {
      while (!cancelled) {
        try {
          const res = await fetch("/health");
          if (res.ok) {
            setReady(true);
            return;
          }
        } catch {
          // backend not up yet — keep trying
        }
        await new Promise((r) => setTimeout(r, 1000));
      }
    }

    poll();
    return () => {
      cancelled = true;
    };
  }, []);

  return ready;
}

// ── App ─────────────────────────────────────────────────────────────

export default function App() {
  const ready = useBackendReady();
  const stream = useAgentStream();

  if (!ready) {
    return (
      <div className="flex h-dvh items-center justify-center bg-[var(--color-bg)]">
        <Loader2 className="h-6 w-6 animate-spin text-[var(--color-accent)]" />
      </div>
    );
  }

  return <AppContent stream={stream} />;
}

// ── App content ─────────────────────────────────────────────────────

function AppContent({ stream }: { stream: ReturnType<typeof useAgentStream> }) {
  const {
    messages,
    loading,
    send,
    stopRun,
    setMessages,
    activeRun,
    sessionId,
    setSessionId,
    reconnectToRun,
  } = stream;
  // Poll for background runs + per-clone run counts only when not streaming
  // locally — avoids double-indicators while a stream is already on screen.
  const { runs: backgroundRuns, cloneCounts } = useActiveRuns(
    loading || !!activeRun,
  );
  const [filesOpen, setFilesOpen] = useState(false);
  const [loadingHistory, setLoadingHistory] = useState(true);
  const [showScrollBtn, setShowScrollBtn] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const isNearBottomRef = useRef(true);
  // Suppresses onScroll while we are scrolling programmatically, so our own
  // scrolls can never flip isNearBottomRef to false (that used to kill
  // auto-follow mid-run — see the smooth-scroll race below).
  const autoScrollLockRef = useRef(false);
  const loadIdRef = useRef(0);

  const hasActiveRun = loading || !!activeRun;
  const hasActiveRunRef = useRef(hasActiveRun);
  hasActiveRunRef.current = hasActiveRun;

  // ── Auto-attach on load ────────────────────────────────────────
  // One run per agent now: if a live run is executing server-side and
  // we're idle, attach to it automatically instead of waiting for a
  // click. Guards against two races:
  //   - the poll list can be ~5s stale right after OUR run completes →
  //     never auto-attach to a run this tab watched to completion;
  //   - a run belonging to another session would yank the user out of
  //     the conversation they're reading → only attach in-place (same
  //     session or nothing on screen).
  // One attempt per run id — a failed attach falls back to the pill's
  // manual Reconnect rather than retry-looping.
  const autoAttachedRef = useRef<Set<string>>(new Set());
  const watchedRunRef = useRef<string | null>(null);

  useEffect(() => {
    if (activeRun?.runId) {
      watchedRunRef.current = activeRun.runId;
    } else if (watchedRunRef.current) {
      // Run we were watching just ended — mark as seen.
      autoAttachedRef.current.add(watchedRunRef.current);
      watchedRunRef.current = null;
    }
  }, [activeRun]);

  useEffect(() => {
    if (loadingHistory || loading || activeRun) return;
    const target = backgroundRuns.find(
      (r) =>
        r.live &&
        r.session_id &&
        !isRunStopping(r.run_id) &&
        !autoAttachedRef.current.has(r.run_id) &&
        // In-place only: same session, or nothing on screen to yank.
        (!sessionId || r.session_id === sessionId),
    );
    if (!target) return;
    autoAttachedRef.current.add(target.run_id);
    // Attach: switch to the run's session (loads history), then hand off to
    // the streaming hook which seeds the run state and calls the resume endpoint.
    if (target.session_id !== sessionId) loadSession(target.session_id);
    reconnectToRun(target);
  }, [backgroundRuns, loading, activeRun, loadingHistory, sessionId]); // eslint-disable-line react-hooks/exhaustive-deps

  // ── Init ──────────────────────────────────────────────────────────

  useEffect(() => {
    (async () => {
      try {
        await fetchTeamId();
        if (!activeRun) {
          if (sessionId) setMessages(await loadSessionHistory(sessionId));
        }
      } finally {
        setLoadingHistory(false);
      }
    })();
  }, [setMessages]); // eslint-disable-line react-hooks/exhaustive-deps

  // Pin to the bottom. Instant while streaming: smooth scrollTo animations get
  // cancelled and restarted on every SSE delta, so the scrollbar lags behind
  // and never catches up — and once that lag exceeds the 100px threshold, the
  // onScroll handler permanently disables auto-follow for the run.
  const scrollToBottom = useCallback((smooth: boolean) => {
    const el = scrollRef.current;
    if (!el) return;
    autoScrollLockRef.current = true;
    el.scrollTo({
      top: el.scrollHeight,
      behavior: smooth ? "smooth" : "auto",
    });
    requestAnimationFrame(() => {
      autoScrollLockRef.current = false;
    });
  }, []);

  // 1) State-driven growth: every SSE delta, tool patch, and card update flows
  //    through `messages`/`hasActiveRun`, so pin synchronously before paint.
  useLayoutEffect(() => {
    if (!isNearBottomRef.current) return;
    scrollToBottom(!hasActiveRun);
  }, [messages, hasActiveRun, scrollToBottom]);

  // 2) Non-state growth: async avatar/markdown images, webfonts, and card
  //    expansion change the message list's height without a React state change.
  //    Watch the list wrapper and re-pin whenever it grows.
  useLayoutEffect(() => {
    const el = contentRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => {
      if (!isNearBottomRef.current) return;
      scrollToBottom(!hasActiveRunRef.current);
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [scrollToBottom]);

  // ── Actions ───────────────────────────────────────────────────────

  async function clearChat() {
    if (loading) await stopRun();
    setMessages([]);
    setSessionId(null);
    isNearBottomRef.current = true;
  }

  function loadSession(id: string) {
    if (id === sessionId) return;
    const gen = ++loadIdRef.current;
    setSessionId(id);
    isNearBottomRef.current = true;
    setLoadingHistory(true);
    if (loading) stopRun();
    (async () => {
      try {
        const history = await loadSessionHistory(id);
        // A newer loadSession call superseded us — discard the stale response.
        if (gen !== loadIdRef.current) return;
        setMessages(history?.length ? history : []);
      } finally {
        if (gen === loadIdRef.current) setLoadingHistory(false);
      }
    })();
  }

  // ── Derived state ─────────────────────────────────────────────────

  const visibleMessages = messages.filter(isVisible);
  const lastMsg = visibleMessages.at(-1);
  const lastUserMsg = [...visibleMessages].reverse().find((m) => m.role === "user");
  const showThinking = hasActiveRun && lastMsg && !isActive(lastMsg);

  // ── Render ────────────────────────────────────────────────────────

  return (
    <div className="flex h-dvh overflow-hidden bg-[var(--color-bg)] font-sans">
      {/* Main content column (chat) */}
      <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
        <div className="mx-auto flex h-full w-full max-w-3xl flex-col px-5">
          {/* Messages */}
          <div className="relative flex min-h-0 flex-1 flex-col">
            <div
              ref={scrollRef}
              onScroll={() => {
                const el = scrollRef.current;
                // Ignore our own programmatic scrolls — only a real user scroll
                // (or its final resting state) may flip the near-bottom flag.
                if (!el || autoScrollLockRef.current) return;
                isNearBottomRef.current =
                  el.scrollHeight - el.scrollTop - el.clientHeight < 100;
                setShowScrollBtn(
                  el.scrollHeight - el.scrollTop - el.clientHeight > 200,
                );
              }}
              className="min-h-0 flex-1 overflow-y-auto overflow-x-hidden overscroll-contain scrollbar-thin pr-1 pb-2"
            >
            <div ref={contentRef} className="space-y-6">
              {loadingHistory && (
                <div className="flex justify-center py-12">
                  <Loader2 className="h-5 w-5 animate-spin text-[var(--color-dim)]" />
                </div>
              )}

              {!loadingHistory &&
                messages.map((msg) =>
                  !isVisible(msg) ? null : (
                    <MessageBubble
                      key={msg.id}
                      msg={msg}
                      running={hasActiveRun}
                      isLast={msg.id === lastMsg?.id}
                      isLastUser={msg.id === lastUserMsg?.id}
                    />
                  ),
                )}

              {showThinking && <ThinkingDots />}
            </div>
            </div>

            {/* Scroll to bottom */}
            {showScrollBtn && (
              <button
                onClick={() => scrollToBottom(true)}
                className="absolute bottom-4 left-1/2 z-10 flex h-9 w-9 -translate-x-1/2 items-center justify-center rounded-full border border-[var(--color-border)] bg-[var(--color-panel)] text-[var(--color-dim)] shadow-lg transition-colors hover:text-[var(--color-accent)]"
                aria-label="Scroll to bottom"
              >
                <ChevronDown className="h-5 w-5" />
              </button>
            )}
          </div>

          {/* Input */}
          <div className="shrink-0 pt-2">
            <InputBar
              onSend={send}
              onStop={stopRun}
              disabled={loading}
              hasActiveRun={hasActiveRun}
            />
            <BottomBar
              onNewChat={clearChat}
              currentSessionId={sessionId}
              onSelectSession={loadSession}
              onOpenFiles={() => setFilesOpen(true)}
              activeRuns={backgroundRuns}
              liveNow={hasActiveRun}
              cloneCounts={cloneCounts}
            />
          </div>
      </div>
      </div>

      {/* Full-screen file editor */}
      <FileExplorer open={filesOpen} onClose={() => setFilesOpen(false)} />
    </div>
  );
}
