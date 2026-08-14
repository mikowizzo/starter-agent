/** useActiveRuns — polls /runs/active for cross-session run visibility.
 *
 * While the user is NOT locally streaming, polls every 5s so the bottom bar
 * can offer reconnect to any background run (even ones this browser never
 * started). Also fetches per-clone active-run counts so the instance
 * switcher can badge each crew member. Pauses while `paused` (a local
 * stream is already on screen).
 */

import { useState, useEffect } from "react";
import {
  fetchActiveRuns,
  fetchCloneRunCounts,
  type ActiveRunInfo,
} from "../lib/api";

const POLL_MS = 5000;

export function useActiveRuns(
  paused: boolean,
): { runs: ActiveRunInfo[]; cloneCounts: Record<string, number> } {
  const [runs, setRuns] = useState<ActiveRunInfo[]>([]);
  const [cloneCounts, setCloneCounts] = useState<Record<string, number>>({});

  useEffect(() => {
    if (paused) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;

    const poll = async () => {
      const [list, counts] = await Promise.all([
        fetchActiveRuns(),
        fetchCloneRunCounts(),
      ]);
      if (cancelled) return;
      setRuns(list);
      setCloneCounts(counts);
      timer = setTimeout(poll, POLL_MS);
    };

    poll();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [paused]);

  return { runs, cloneCounts };
}

