const ACTIVE_SESSION_KEY = "active_session";
const ACTIVE_RUN_KEY = "active_run";

export const userId = "user";

export let agnoSessionId: string | null =
  localStorage.getItem(ACTIVE_SESSION_KEY);

export function setAgnoSessionId(id: string | null) {
  agnoSessionId = id;
  if (id) localStorage.setItem(ACTIVE_SESSION_KEY, id);
  else localStorage.removeItem(ACTIVE_SESSION_KEY);
}

// ── Active run persistence (localStorage — survives browser close) ──

export interface ActiveRun {
  runId: string;
  sessionId: string;
  lastEventIndex: number;
  /** The user message text (to reconstruct the user bubble on reconnect) */
  userMessage: string;
  /** Timestamp when the run was created, for expiration */
  createdAt: number;
}

/** Runs older than this are considered stale and won't be reconnected. */
const RUN_TTL_MS = 30 * 60 * 1000; // 30 minutes

export function getActiveRun(): ActiveRun | null {
  try {
    const raw = localStorage.getItem(ACTIVE_RUN_KEY);
    if (!raw) return null;
    const run: ActiveRun = JSON.parse(raw);
    // Expire stale runs
    if (Date.now() - run.createdAt > RUN_TTL_MS) {
      localStorage.removeItem(ACTIVE_RUN_KEY);
      return null;
    }
    return run;
  } catch {
    localStorage.removeItem(ACTIVE_RUN_KEY);
    return null;
  }
}

export function setActiveRun(run: ActiveRun | null) {
  if (run) {
    localStorage.setItem(ACTIVE_RUN_KEY, JSON.stringify(run));
  } else {
    localStorage.removeItem(ACTIVE_RUN_KEY);
  }
}

export function updateActiveRunEventIndex(index: number) {
  const raw = localStorage.getItem(ACTIVE_RUN_KEY);
  if (!raw) return;
  try {
    const run: ActiveRun = JSON.parse(raw);
    run.lastEventIndex = index;
    localStorage.setItem(ACTIVE_RUN_KEY, JSON.stringify(run));
  } catch {
    // ignore
  }
}
