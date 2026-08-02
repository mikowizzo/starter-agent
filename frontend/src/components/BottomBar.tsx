import { useState, useEffect } from "react";
import { History, RotateCw } from "lucide-react";
import { HistoryModal } from "./HistoryModal";
import { ModelSelector } from "./ModelSelector";
import { fetchModel, fetchClones, type CloneInfo } from "../lib/api";

export function BottomBar({
  onNewChat,
  currentSessionId,
  onSelectSession,
}: {
  onNewChat: () => void;
  currentSessionId: string | null;
  onSelectSession: (sessionId: string) => void;
}) {
  const [modelName, setModelName] = useState("");
  const [showModelSelector, setShowModelSelector] = useState(false);
  const [showHistory, setShowHistory] = useState(false);
  const [restarting, setRestarting] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [switcher, setSwitcher] = useState<{
    self_name: string;
    self_port: number;
    parent: { name: string; frontend_port: number } | null;
    clones: CloneInfo[];
  } | null>(null);

  async function refreshModel() {
    const info = await fetchModel();
    if (info?.name) setModelName(info.name);
  }

  useEffect(() => {
    refreshModel();
    fetchClones().then(setSwitcher);
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
          className="rounded-lg px-1.5 py-1 text-[10px] font-medium text-[var(--color-dim)] transition hover:text-[var(--color-accent)] active:scale-[0.95]"
        >
          {modelName || "..."}
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
};

function InstanceLink({
  name,
  port,
  isCurrent,
}: {
  name: string;
  port: number;
  isCurrent: boolean;
}) {
  const cls =
    "rounded-md px-2 py-0.5 text-[10px] font-medium whitespace-nowrap transition " +
    (isCurrent
      ? "bg-[var(--color-accent)] text-[var(--color-bg)] cursor-default"
      : (CLONE_COLORS[name] ?? "text-[var(--color-accent)]") +
        " hover:bg-[var(--color-border)]/40 active:scale-[0.95]");
  const href = `${window.location.protocol}//${window.location.hostname}:${port}`;
  return isCurrent ? (
    <span className={cls} title={`${name} — current instance`}>
      {name}
    </span>
  ) : (
    <a href={href} className={cls} title={`Open ${name} (${href})`}>
      {name}
    </a>
  );
}
