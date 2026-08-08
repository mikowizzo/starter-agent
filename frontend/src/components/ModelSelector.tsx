import { useState, useEffect } from "react";
import { Loader2, Check } from "lucide-react";
import { fetchModel, setModel, fetchModels, fetchQuota, fetchSyntheticQuota, type ModelInfo, type ModelOption, type QuotaInfo } from "../lib/api";


function formatQuota(quota: QuotaInfo): string | null {
  const { percentage: pct, reset_at: resetAt } = quota;
  if (pct == null) return null;
  const pctStr = `${Math.round(pct)}%`;
  if (!resetAt) return pctStr;
  const diff = resetAt - Date.now();
  if (diff <= 0) return pctStr;
  const hours = Math.floor(diff / 3600000);
  const mins = Math.floor((diff % 3600000) / 60000);
  if (hours > 0) return `${pctStr} ${hours}h${mins}m`;
  return `${pctStr} ${mins}m`;
}

export function ModelSelector({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [current, setCurrent] = useState<ModelInfo | null>(null);
  const [models, setModels] = useState<Record<string, ModelOption>>({});
  const [switching, setSwitching] = useState<string | null>(null);
  const [quota, setQuota] = useState<QuotaInfo | null>(null);
  const [synthQuota, setSynthQuota] = useState<QuotaInfo | null>(null);

  // Refetch every time the popover opens — caches are pre-warmed so
  // this is instant after first load.
  useEffect(() => {
    if (!open) return;
    // Defensive: clear any leftover switching state from a previous session.
    setSwitching(null);
    let cancelled = false;
    (async () => {
      const [model, list] = await Promise.all([
        fetchModel(),
        fetchModels(),
      ]);
      const [q, sq] = await Promise.all([fetchQuota(), fetchSyntheticQuota()]);
      if (cancelled) return;
      setCurrent(model);
      setModels(list);
      if (q) setQuota(q);
      if (sq) setSynthQuota(sq);
    })();
    return () => { cancelled = true; };
  }, [open]);

  async function handleSelect(key: string) {
    if (key === current?.current || switching) return;
    setSwitching(key);
    const result = await setModel(key);
    if (result) setCurrent(result);
    setTimeout(onClose, 150);
  }

  if (!open) return null;

  return (
    <>
      <div className="fixed inset-0 z-[65]" onClick={onClose} />

      <div className="absolute left-0 right-0 bottom-full mb-2 mx-5 rounded-lg border border-[var(--color-border)] bg-[var(--color-panel)] shadow-xl animate-fade-in z-[70]">
        <div className="py-1">
          {Object.keys(models).length === 0 ? (
            <div className="flex justify-center py-4">
              <Loader2 className="h-3.5 w-3.5 animate-spin text-[var(--color-dim)]" />
            </div>
          ) : (
            Object.entries(models).map(([key, opt]) => {
              const isActive = current?.current === key;
              const isSwitching = switching === key;

              return (
                <button
                  key={key}
                  onClick={() => handleSelect(key)}
                  disabled={!!switching}
                  className={`w-full text-left px-3 py-1.5 flex items-center gap-2 text-[12px] transition ${
                    isActive
                      ? "text-[var(--color-accent)]"
                      : "text-[var(--color-dim)] hover:text-[var(--color-text)]"
                  } ${switching ? "opacity-60 cursor-wait" : ""}`}
                >
                  <span className="truncate font-medium">{opt.name}</span>
                  <span className="text-[10px] opacity-40">{opt.provider}</span>
                  {opt.provider.toLowerCase().includes("zai") && quota && formatQuota(quota) && (
                    <span className="text-[10px] text-[var(--color-accent)]">({formatQuota(quota)})</span>
                  )}
                  {opt.provider.toLowerCase().includes("synthetic") && synthQuota && formatQuota(synthQuota) && (
                    <span className="text-[10px] text-[var(--color-accent)]">({formatQuota(synthQuota)})</span>
                  )}
                  <div className="flex-1" />
                  {isSwitching ? (
                    <Loader2 className="h-3 w-3 animate-spin shrink-0" />
                  ) : isActive ? (
                    <Check className="h-3 w-3 shrink-0" />
                  ) : null}
                </button>
              );
            })
          )}
        </div>
      </div>
    </>
  );
}
