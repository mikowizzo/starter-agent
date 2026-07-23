import { useState, useRef, useEffect, useLayoutEffect } from "react";
import { Loader2 } from "lucide-react";
import { InputBar } from "./components/InputBar";
import { BottomBar } from "./components/BottomBar";
import {
  MessageBubble,
  ThinkingDots,
  isVisible,
  isActive,
} from "./components/MessageBubble";
import { useAgentStream } from "./hooks/useAgentStream";
import { loadSessionHistory, fetchTeamId } from "./lib/api";

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
    sessionId,
    setSessionId,
  } = stream;

  const [loadingHistory, setLoadingHistory] = useState(true);
  const scrollRef = useRef<HTMLDivElement>(null);
  const isNearBottomRef = useRef(true);
  const loadIdRef = useRef(0);

  const hasActiveRun = loading;

  // ── Init ──────────────────────────────────────────────────────────

  useEffect(() => {
    (async () => {
      try {
        await fetchTeamId();
        if (sessionId) setMessages(await loadSessionHistory(sessionId));
      } finally {
        setLoadingHistory(false);
      }
    })();
  }, [setMessages]); // eslint-disable-line react-hooks/exhaustive-deps

  // useLayoutEffect: jump before paint, no flash.
  useLayoutEffect(() => {
    if (!isNearBottomRef.current) return;
    scrollRef.current?.scrollTo({
      top: scrollRef.current.scrollHeight,
      behavior: "smooth",
    });
  }, [messages, hasActiveRun]);

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
  const lastUserMsg = visibleMessages.findLast((m) => m.role === "user");
  const showThinking = hasActiveRun && lastMsg && !isActive(lastMsg);

  // ── Render ────────────────────────────────────────────────────────

  return (
    <div className="flex h-dvh overflow-hidden bg-[var(--color-bg)] font-sans">
      {/* Main content column (chat) */}
      <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
        <div className="mx-auto flex h-full w-full max-w-3xl flex-col px-5">
          {/* Messages */}
          <div
            ref={scrollRef}
            onScroll={() => {
              const el = scrollRef.current;
              if (!el) return;
              isNearBottomRef.current =
                el.scrollHeight - el.scrollTop - el.clientHeight < 100;
            }}
            className="flex-1 min-h-0 overflow-y-auto overflow-x-hidden overscroll-contain space-y-6 pr-1 pb-2 scrollbar-thin"
          >
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
            />
          </div>
        </div>
      </div>
    </div>
  );
}
