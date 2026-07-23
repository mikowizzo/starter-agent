import { useEffect, useRef, useState } from "react";
import { Loader2, MessageSquare, Plus, RotateCw, X } from "lucide-react";
import { fetchSessions, type SessionListItem } from "../lib/api";

// ── History Modal ───────────────────────────────────────────────────

interface HistoryModalProps {
  open: boolean;
  currentSessionId: string | null;
  onClose: () => void;
  onSelect: (sessionId: string) => void;
  onNewChat: () => void;
}

function formatDate(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

export function HistoryModal({
  open,
  currentSessionId,
  onClose,
  onSelect,
  onNewChat,
}: HistoryModalProps) {
  const [sessions, setSessions] = useState<SessionListItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [restarting, setRestarting] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const timersRef = useRef<number[]>([]);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setLoading(true);
    fetchSessions()
      .then((list) => {
        if (!cancelled) setSessions(list);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open]);

  // Clear any pending intervals on unmount
  useEffect(() => {
    return () => {
      timersRef.current.forEach(clearInterval);
    };
  }, []);

  async function handleRestart() {
    if (restarting) return;
    setRestarting(true);
    setElapsed(0);
    const startedAt = Date.now();
    const ticker = window.setInterval(
      () => setElapsed(Math.floor((Date.now() - startedAt) / 1000)),
      1000,
    );
    timersRef.current.push(ticker);
    try {
      await fetch("/settings/restart", { method: "POST" });
    } catch { /* expected */ }
    let attempts = 0;
    const poll = window.setInterval(async () => {
      attempts++;
      try {
        const res = await fetch("/agents", { method: "GET" });
        if (res.ok) {
          clearInterval(poll);
          clearInterval(ticker);
          setRestarting(false);
          location.reload();
        }
      } catch { /* still down */ }
      if (attempts > 30) {
        clearInterval(poll);
        clearInterval(ticker);
        setRestarting(false);
      }
    }, 1000);
    timersRef.current.push(poll);
  }

  if (!open) return null;

  return (
    <>
      <div
        className="fixed inset-0 z-[75] bg-black/40 backdrop-blur-sm"
        onClick={onClose}
      />

      <div className="fixed inset-x-4 bottom-[calc(3.5rem+env(safe-area-inset-bottom,0px))] z-[80] max-h-[70vh] animate-slide-up rounded-2xl border border-[var(--color-border)] bg-[var(--color-panel)] shadow-2xl flex flex-col">
        {/* Header */}
        <div className="flex items-center justify-between border-b border-[var(--color-border)] px-4 py-3">
          <h2 className="text-sm font-semibold text-[var(--color-text)]">
            Chat history
          </h2>
          <button
            onClick={onClose}
            title="Close"
            className="rounded-lg p-1 text-[var(--color-dim)] transition hover:bg-[var(--color-border)] hover:text-[var(--color-text)]"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {/* List */}
        <div className="overflow-y-auto p-2 scrollbar-thin">
          {loading ? (
            <div className="flex justify-center py-8">
              <Loader2 className="h-5 w-5 animate-spin text-[var(--color-dim)]" />
            </div>
          ) : sessions.length === 0 ? (
            <div className="py-8 text-center text-sm text-[var(--color-dim)]">
              No previous chats
            </div>
          ) : (
            <div className="space-y-1">
              {sessions.map((s) => {
                const active = s.session_id === currentSessionId;
                return (
                  <button
                    key={s.session_id}
                    onClick={() => {
                      onSelect(s.session_id);
                      onClose();
                    }}
                    className={`w-full rounded-xl px-3 py-2.5 text-left transition ${
                      active
                        ? "bg-[var(--color-accent)]/10 ring-1 ring-[var(--color-accent)]/30"
                        : "hover:bg-[var(--color-border)]/60"
                    }`}
                  >
                    <div className="flex items-start gap-2.5">
                      <MessageSquare className="mt-0.5 h-4 w-4 shrink-0 text-[var(--color-dim)]" />
                      <div className="min-w-0 flex-1">
                        <div
                          className={`truncate text-[13px] font-medium ${
                            active
                              ? "text-[var(--color-accent)]"
                              : "text-[var(--color-text)]"
                          }`}
                        >
                          {s.session_name || "Untitled chat"}
                        </div>
                        <div className="text-[11px] text-[var(--color-dim)]">
                          {formatDate(s.updated_at)}
                        </div>
                      </div>
                    </div>
                  </button>
                );
              })}
            </div>
          )}
        </div>

        {/* Footer: restart on left, new chat on right */}
        <div className="flex items-center justify-between border-t border-[var(--color-border)] px-3 py-2">
          <button
            onClick={handleRestart}
            title="Restart backend"
            className={`flex items-center gap-1.5 rounded-lg px-2 py-1.5 text-sm font-medium transition active:scale-[0.95] ${
              restarting
                ? "text-[var(--color-accent)]"
                : "text-[var(--color-dim)] hover:text-[var(--color-accent)]"
            }`}
          >
            <RotateCw className={`h-4 w-4 ${restarting ? "animate-spin" : ""}`} />
            {restarting && (
              <span className="text-[10px] font-medium tabular-nums">{elapsed}s</span>
            )}
          </button>

          <button
            onClick={() => {
              onNewChat();
              onClose();
            }}
            className="inline-flex items-center gap-2 rounded-full px-4 py-2 text-sm font-medium text-[var(--color-text)] transition hover:text-[var(--color-accent)] active:scale-[0.97]"
          >
            <Plus className="h-4 w-4" />
            New chat
          </button>
        </div>
      </div>
    </>
  );
}
