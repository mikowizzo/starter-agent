import { useState, useEffect, useRef, useMemo } from "react";
import { History, RotateCw, Folder, X } from "lucide-react";
import { HistoryModal } from "./HistoryModal";
import { ModelSelector } from "./ModelSelector";
import {
  fetchModel,
  fetchClones,
  fetchQuota,
  fetchSyntheticQuota,
  type CloneInfo,
  type QuotaInfo,
  type ActiveRunInfo,
} from "../lib/api";
import { isRunStopping, pruneStoppingRuns } from "../lib/session";


// ── Quota formatting ─────────────────────────────────────────────

function formatQuota(quota: QuotaInfo): string | null {
  const { percentage: pct, reset_at: resetAt } = quota;
  if (pct == null) return null;

  let suffix = "";
  if (resetAt) {
    const diff = Math.max(0, resetAt - Date.now());
    const h = Math.floor(diff / 3_600_000);
    const m = Math.floor((diff % 3_600_000) / 60_000);
    suffix = ` ${h}h${String(m).padStart(2, "0")}m`;
  }
  return `${pct}%${suffix}`;
}

// ── Elapsed time for active runs ─────────────────────────────────

function formatElapsed(iso: string | null): string {
  if (!iso) return "";
  const ms = Date.now() - new Date(iso).getTime();
  if (Number.isNaN(ms) || ms < 0) return "";
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m${String(s % 60).padStart(2, "0")}s`;
  const h = Math.floor(m / 60);
  return `${h}h${String(m % 60).padStart(2, "0")}m`;
}

export function BottomBar({
  onNewChat,
  currentSessionId,
  onSelectSession,
  onOpenFiles,
  activeRuns,
  cloneCounts,
  onReconnectRun,
}: {
  onNewChat: () => void;
  currentSessionId: string | null;
  onSelectSession: (sessionId: string) => void;
  onOpenFiles: () => void;
  activeRuns: ActiveRunInfo[];
  cloneCounts?: Record<string, number>;
  onReconnectRun: (run: ActiveRunInfo) => void;
}) {
  const [modelName, setModelName] = useState("");
  const [modelProvider, setModelProvider] = useState("");
  const [quota, setQuota] = useState<QuotaInfo | null>(null);
  const [synthQuota, setSynthQuota] = useState<QuotaInfo | null>(null);
  const [showModelSelector, setShowModelSelector] = useState(false);
  const [showHistory, setShowHistory] = useState(false);
  const [showRuns, setShowRuns] = useState(false);
  const runsRef = useRef<HTMLDivElement>(null);
  const [restarting, setRestarting] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [switcher, setSwitcher] = useState<{
    self_name: string;
    self_port: number;
    parent: { name: string; frontend_port: number } | null;
    clones: CloneInfo[];
  } | null>(null);
  // Close the runs popover on outside click
  useEffect(() => {
    if (!showRuns) return;
    const onDown = (e: MouseEvent) => {
      if (runsRef.current && !runsRef.current.contains(e.target as Node)) {
        setShowRuns(false);
      }
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [showRuns]);

  // Decorate runs with their stopping state, and prune markers for runs
  // that are no longer active (so the marker store doesn't grow forever).
  const runs = useMemo(
    () => activeRuns.map((r) => ({ ...r, stopping: isRunStopping(r.run_id) })),
    [activeRuns],
  );
  useEffect(() => {
    if (activeRuns.length > 0) {
      pruneStoppingRuns(activeRuns.map((r) => r.run_id));
    }
  }, [activeRuns]);

  async function refreshModel() {
    const info = await fetchModel();
    if (info?.name) setModelName(info.name);
    if (info?.provider) setModelProvider(info.provider);
  }

  useEffect(() => {
    refreshModel();
    fetchClones().then(setSwitcher);
    fetchQuota().then((q) => { if (q) setQuota(q); });
    fetchSyntheticQuota().then((q) => { if (q) setSynthQuota(q); });
  }, []);

  async function handleRestart() {
    if (restarting) return;
    setRestarting(true);
    setElapsed(0);
    const startedAt = Date.now();
    const ticker = setInterval(() => {
      setElapsed(Math.floor((Date.now() - startedAt) / 1000));
    }, 1000);
    try {
      await fetch("/settings/restart", { method: "POST" });
    } catch { /* expected — connection drops during restart */ }
    // Poll until the server comes back
    let attempts = 0;
    const poll = setInterval(async () => {
      attempts++;
      try {
        const res = await fetch("/settings/models", { method: "GET" });
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
  }

  // Quota badge — only for ZAI models
  const provider = modelProvider.toLowerCase();
  let quotaExtra: string | null = null;
  if (provider.includes("zai") && quota) {
    quotaExtra = formatQuota(quota);
  }

  const currentPort =
    window.location.port ||
    (window.location.protocol === "https:" ? "443" : "80");
  // The instance we're viewing = the clone whose frontend port matches the
  // URL we're on; if no clone matches, it's the self pill.
  const currentName =
    switcher?.clones.find((c) => c.ports.frontend === Number(currentPort))
      ?.name ?? switcher?.self_name ?? "";

  return (
    <div className="relative pb-[calc(0.5rem+env(safe-area-inset-bottom,0px))]">
      {/* History modal */}
      <HistoryModal
        open={showHistory}
        currentSessionId={currentSessionId}
        onClose={() => setShowHistory(false)}
        onSelect={onSelectSession}
        onNewChat={onNewChat}
      />

      {/* Model selector popover */}
      <ModelSelector
        open={showModelSelector}
        onClose={() => {
          setShowModelSelector(false);
          refreshModel();
        }}
      />

      <div className="flex items-center h-10 px-1">
        {/* Model badge — click to switch */}
        <button
          onClick={() => setShowModelSelector(!showModelSelector)}
          title="Switch model"
          className="flex items-center gap-1.5 rounded-lg px-1.5 py-1 text-[10px] font-medium text-[var(--color-dim)] transition hover:text-[var(--color-accent)] active:scale-[0.95]"
        >
          {modelName || "..."}
          {quotaExtra && (
            <span className="text-[var(--color-accent)]">({quotaExtra})</span>
          )}
        </button>

        {/* Restart button */}
        <button
          onClick={handleRestart}
          title="Restart backend"
          className={`flex items-center gap-1 rounded-lg p-1.5 text-[var(--color-dim)] transition active:scale-[0.95] ${
            restarting
              ? "text-[var(--color-accent)]"
              : "hover:text-[var(--color-accent)]"
          }`}
        >
          <RotateCw className={`h-3 w-3 ${restarting ? "animate-spin" : ""}`} />
          {restarting && (
            <span className="text-[10px] font-medium tabular-nums">{elapsed}s</span>
          )}
        </button>

        <div className="flex-1" />

        {/* Active runs pill — persistent so its state is always observable.
            order-first renders it at the far left, before the model badge. */}
        <div className="relative order-first" ref={runsRef}>
          <button
            onClick={() => setShowRuns(!showRuns)}
            title="Background runs"
            className={`flex items-center gap-1.5 rounded-lg px-1.5 py-1 text-[10px] font-medium transition hover:bg-[var(--color-border)]/40 active:scale-[0.95] ${
              runs.length > 0
                ? "text-[var(--color-accent)]"
                : "text-[var(--color-dim)]"
            }`}
          >
            {runs.length > 0 ? (
              <span className="relative flex h-2 w-2">
                <span className="absolute inline-flex h-2 w-2 rounded-full bg-[var(--color-accent)] animate-glow-pulse"></span>
                <span className="relative inline-flex h-2 w-2 rounded-full bg-[var(--color-accent)]"></span>
              </span>
            ) : (
              <span className="inline-flex h-2 w-2 rounded-full bg-[var(--color-dim)]/50"></span>
            )}
            {(runs.filter((r) => !r.stopping).length || runs.length) > 0 &&
              (runs.filter((r) => !r.stopping).length || runs.length)}
          </button>

            {showRuns && (
              <div className="absolute bottom-8 left-0 z-50 w-80 rounded-xl border border-[var(--color-border)] bg-[var(--color-panel)] p-2 shadow-xl">
                <div className="flex items-center justify-between px-2 pb-1.5 pt-1">
                  <span className="text-[11px] font-semibold text-[var(--color-dim)]">
                    Background runs
                  </span>
                  <button
                    onClick={() => setShowRuns(false)}
                    className="rounded p-0.5 text-[var(--color-dim)] transition hover:text-[var(--color-accent)]"
                  >
                    <X className="h-3 w-3" />
                  </button>
                </div>
                <div className="max-h-72 overflow-y-auto scrollbar-thin">
                  {runs.length === 0 && (
                    <div className="px-2 py-3 text-center">
                      <div className="text-[10px] text-[var(--color-dim)]">No active runs</div>
                      <div className="mt-0.5 text-[9px] leading-relaxed text-[var(--color-dim)]/60">
                        Runs keep executing in the background when you disconnect — they'll appear here.
                      </div>
                    </div>
                  )}
                  {runs.map((run) => (
                    <div
                      key={run.run_id}
                      className="flex items-start justify-between gap-2 rounded-lg px-2 py-1.5 hover:bg-[var(--color-border)]/30"
                    >
                      <div className="min-w-0 flex-1">
                        <div className="truncate text-[11px] text-[var(--color-fg)]">
                          {run.input_preview || "(no input)"}
                        </div>
                        <div className="mt-0.5 flex items-center gap-1.5 text-[9px] text-[var(--color-dim)]">
                          <span className="tabular-nums">
                            {formatElapsed(run.created_at)}
                          </span>
                          {run.stopping ? (
                            <span className="italic">· stopping…</span>
                          ) : run.live ? (
                            <span className="text-[var(--color-accent)]">· live</span>
                          ) : (
                            <span className="text-[var(--color-dim)]">· orphaned (backend restarted)</span>
                          )}
                        </div>
                      </div>
                      {run.live && !run.stopping ? (
                        <button
                          onClick={() => {
                            setShowRuns(false);
                            onReconnectRun(run);
                          }}
                          className="shrink-0 rounded-md border border-[var(--color-accent)]/40 px-2 py-0.5 text-[10px] font-medium text-[var(--color-accent)] transition hover:bg-[var(--color-accent)]/10 active:scale-[0.95]"
                        >
                          Reconnect
                        </button>
                      ) : (
                        <span className="shrink-0 text-[9px] text-[var(--color-dim)]">
                          not resumable
                        </span>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>

        {/* Files */}
        <button
          onClick={onOpenFiles}
          title="Files"
          className="flex items-center justify-center rounded-lg p-1.5 text-[var(--color-dim)] transition hover:text-[var(--color-accent)] active:scale-[0.95]"
        >
          <Folder className="h-4 w-4" />
        </button>

        {/* History */}
        <button
          onClick={() => setShowHistory(true)}
          title="History"
          className="flex items-center justify-center rounded-lg p-1.5 text-[var(--color-dim)] transition hover:text-[var(--color-accent)] active:scale-[0.95]"
        >
          <History className="h-4 w-4" />
        </button>
      </div>

      {/* Instance switcher — running clones only */}
      {switcher && (
        <div className="flex items-center gap-1.5 overflow-x-auto px-1 pb-1 scrollbar-thin">
          {switcher.clones
            .filter((c) => c.status === "running")
            .map((c) => (
              <InstanceLink
                key={c.name}
                name={c.name}
                port={c.ports.frontend}
                isCurrent={c.name === currentName}
                runCount={
                  c.name === currentName
                    ? runs.filter((r) => !r.stopping).length
                    : cloneCounts?.[c.name] ?? 0
                }
              />
            ))}
        </div>
      )}
    </div>
  );
}

const CLONE_COLORS: Record<string, string> = {
  nami: "text-orange-400",
  zoro: "text-green-400",
  sanji: "text-blue-400",
  luffy: "text-red-400",
  robin: "text-purple-400",
  usopp: "text-yellow-400",
  franky: "text-cyan-400",
  bella: "text-red-400",
  scm: "text-teal-400",
  hollard: "text-purple-400",
};

function InstanceLink({
  name,
  port,
  isCurrent,
  runCount,
}: {
  name: string;
  port: number;
  isCurrent: boolean;
  runCount?: number;
}) {
  const cls =
    "rounded-md px-2 py-0.5 text-[10px] font-medium whitespace-nowrap transition " +
    (isCurrent
      ? "bg-[var(--color-accent)] text-[var(--color-bg)] cursor-default"
      : (CLONE_COLORS[name] ?? "text-[var(--color-accent)]") +
        " hover:bg-[var(--color-border)]/40 active:scale-[0.95]");
  const href = `${window.location.protocol}//${window.location.hostname}:${port}`;
  const badge = runCount ? (
    <span className="ml-1 rounded-full bg-[var(--color-accent)] px-1 text-[9px] leading-4 text-[var(--color-bg)]">
      {runCount}
    </span>
  ) : null;
  return isCurrent ? (
    <span className={cls} title={`${name} — current instance`}>
      {name}
      {badge}
    </span>
  ) : (
    <a href={href} className={cls} title={`Open ${name} (${href})`}>
      {name}
      {badge}
    </a>
  );
}
