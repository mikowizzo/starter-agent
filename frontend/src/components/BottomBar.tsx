import { useState, useEffect } from "react";
import { History } from "lucide-react";
import { HistoryModal } from "./HistoryModal";
import { ModelSelector } from "./ModelSelector";
import { fetchModel } from "../lib/api";

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

  async function refreshModel() {
    const info = await fetchModel();
    if (info?.name) setModelName(info.name);
  }

  useEffect(() => {
    refreshModel();
  }, []);

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
    </div>
  );
}
