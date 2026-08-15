"""Active-run visibility endpoints.

Runs are streamed with background=true, so they keep executing server-side
even when the browser drops the SSE connection. This router answers
"what is running right now?" across ALL sessions, so the UI can offer
reconnect even when localStorage has no trace of the run.

Two sources, merged:

1. agno's event buffer (`agno.os.managers.event_buffer`, a module-level
   singleton) — knows every run this process has streamed events for, with
   monotonic event indices. CAVEAT: nothing in agno's teams router ever
   calls set_run_completed(), so buffer status stays RUNNING forever.
   The buffer alone would report ghosts.

2. SQLite (`agno_sessions.runs`, a double-encoded JSON string) — the
   authoritative run status. Records are persisted as RUNNING while
   executing and flipped to a terminal status on completion.

Algorithm: take runs the buffer is still streaming, cross-check against
the DB (drop ghosts whose DB status is terminal), and separately report
persisted-RUNNING runs the buffer has never seen (live: false — they died
with a backend restart and cannot be resumed).

Endpoint is a plain `def` so FastAPI runs the sqlite scan in a threadpool
instead of blocking the SSE-streaming event loop.
"""

import asyncio
import json
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from agno.os.managers import event_buffer
from agno.run.base import RunStatus

# The live agno DB sits next to the backend package (uvicorn's cwd), NOT at
# config.DB_PATH (which resolves against the workspace root). Resolve from
# this file so it works in container and host alike.
DB_PATH = str(Path(__file__).resolve().parents[2] / "agno.db")

router = APIRouter(tags=["runs"])

# A live run streams events near-constantly (deltas, tool calls); if the
# buffer has heard nothing from a run for this long, treat it as dead.
# agno never calls set_run_completed() for team runs, so buffer status
# alone cannot detect completion — staleness is the real liveness signal.
BUFFER_STALE_SECONDS = 5 * 60

# A persisted-RUNNING orphan (backend restart victim) is worth surfacing
# for this long after it started; older records are almost certainly
# crash-ghosts whose status was never flipped.
ORPHAN_MAX_AGE_SECONDS = 10 * 60

# Only consider runs in sessions touched within this window. Terminal flips
# happen at completion time (recent), so ghosts are always inside the window;
# genuinely long runs stay visible via the live buffer path regardless.
RECENT_WINDOW_SECONDS = 40 * 60

PREVIEW_MAX = 200


def _iso(ts) -> str | None:
    """Unix seconds (int/float/str) → ISO 8601 Z, best-effort."""
    try:
        return datetime.fromtimestamp(int(float(ts)), tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError, OSError):
        return None


def _preview_from_input(inp) -> str | None:
    if inp is None:
        return None
    if isinstance(inp, dict):
        inp = inp.get("input_content")
    if not isinstance(inp, str):
        try:
            inp = json.dumps(inp)
        except (TypeError, ValueError):
            return None
    return inp[:PREVIEW_MAX] or None


def _recent_db_runs() -> tuple[dict[str, dict], set[str]]:
    """(running_runs, terminal_run_ids) from sessions touched recently.

    One SQL pass over recent sessions; `json_each` walks each session's
    double-encoded runs array so the multi-MB blobs are never parsed in
    Python (run records can exceed 2 MB each).
    """
    running: dict[str, dict] = {}
    terminal: set[str] = set()
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT s.session_id AS sid, j.value AS run_json "
            "FROM agno_sessions s, json_each(json_extract(s.runs, '$')) j "
            "WHERE s.updated_at > ? AND j.value IS NOT NULL",
            (int(time.time()) - RECENT_WINDOW_SECONDS,),
        ).fetchall()
        conn.close()
    except sqlite3.Error:
        return running, terminal

    for row in rows:
        try:
            run = json.loads(row["run_json"]) if isinstance(row["run_json"], str) else row["run_json"]
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(run, dict):
            continue
        run_id = run.get("run_id")
        if not run_id:
            continue
        status = run.get("status")
        if status == RunStatus.running.value:
            # A run id maps to one session; keep the freshest record.
            running[run_id] = {
                "session_id": row["sid"],
                "created_at": run.get("created_at"),
                "input_preview": _preview_from_input(run.get("input")),
            }
        else:
            terminal.add(run_id)
    return running, terminal


# ── Shared cache for the DB scan ────────────────────────────────────
# Both /runs/active (5s poll) and the one-run-per-agent guard hit the
# same scan; a 2s memo dedupes bursts without ever serving stale data
# across poll cycles.

_RECENT_CACHE_SECONDS = 2.0
_recent_ts = 0.0
_recent_cache: tuple[dict[str, dict], set[str]] | None = None


def _recent_db_runs_cached() -> tuple[dict[str, dict], set[str]]:
    global _recent_ts, _recent_cache
    if _recent_cache is not None and time.monotonic() - _recent_ts < _RECENT_CACHE_SECONDS:
        return _recent_cache
    result = _recent_db_runs()
    _recent_ts = time.monotonic()
    _recent_cache = result
    return result


def _live_runs() -> list[dict]:
    """Genuinely executing runs, newest first — the resumable set.

    A run is live when the event buffer is still streaming it (fresh
    RUNNING status) AND the DB has no terminal record for it (buffer
    status never flips for team runs — see module docstring).

    Fast path: with no fresh buffer candidates no DB scan happens at all
    (the common case; the scan costs ~200ms on a large DB).
    """
    now = time.time()
    candidates = []
    for run_id, meta in event_buffer.run_metadata.items():
        status = meta.get("status")
        status_val = status.value if hasattr(status, "value") else status
        if status_val != RunStatus.running.value:
            continue
        last_updated = meta.get("last_updated")
        if last_updated and now - float(last_updated) > BUFFER_STALE_SECONDS:
            continue
        candidates.append((run_id, meta))
    if not candidates:
        return []

    db_running, db_terminal = _recent_db_runs_cached()
    out: list[dict] = []
    for run_id, meta in candidates:
        if run_id in db_terminal:
            continue  # ghost — DB says it finished
        db_info = db_running.get(run_id, {})
        events = event_buffer.events.get(run_id) or []
        session_id = db_info.get("session_id") or next(
            (getattr(ev, "session_id", None) for ev in events if getattr(ev, "session_id", None)), None
        )
        out.append(
            {
                "run_id": run_id,
                "session_id": session_id,
                "status": "RUNNING",
                "created_at": _iso(db_info.get("created_at") or meta.get("created_at")),
                "last_updated": _iso(meta.get("last_updated")),
                "event_count": len(events),
                "last_event_index": event_buffer.get_last_index(run_id),
                "input_preview": db_info.get("input_preview"),
                "live": True,
            }
        )
    out.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return out


@router.get("/runs/active")
def list_active_runs():
    """All runs currently executing (or that look like it), newest first."""
    active: list[dict] = _live_runs()
    now = time.time()
    db_running, db_terminal = _recent_db_runs_cached()

    seen = {r["run_id"] for r in active}

    # ── 2. Orphans: persisted RUNNING but never seen by this process ──
    # They died with a backend restart; their partial output (if any) is in
    # session history already. Reported for visibility, not reconnectable.
    for run_id, info in db_running.items():
        if run_id in seen or run_id in db_terminal:
            # `in seen`: covered by the live path above.
            # `in db_terminal`: the DB holds BOTH a RUNNING record (written at
            # run start) and a terminal one (written at completion) — the run
            # actually finished; the RUNNING row is stale. Without this check
            # a completed run whose stream was dropped (e.g. Stop button)
            # shows as an orphan forever.
            continue
        created = info.get("created_at")
        if created and now - float(created) > ORPHAN_MAX_AGE_SECONDS:
            continue  # ancient ghost — died mid-run long ago, status never flipped
        active.append(
            {
                "run_id": run_id,
                "session_id": info.get("session_id"),
                "status": "RUNNING",
                "created_at": _iso(info.get("created_at")),
                "last_updated": None,
                "event_count": None,
                "last_event_index": None,
                "input_preview": info.get("input_preview"),
                "live": False,
            }
        )

    active.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return JSONResponse({"data": active})


# ── One-run-per-agent guard ─────────────────────────────────────────
#
# Enforced by middleware in main.py on POST /teams/{id}/runs: if a run
# is genuinely executing, new runs are refused with 409 + the active
# run's info so the UI can attach to it instead. Only LIVE runs block
# (orphans died with a restart; stopping runs clear within seconds as
# the DB flips to terminal).

async def get_blocking_run() -> dict | None:
    """The newest live run, or None. Called before creating any new run."""
    runs = await asyncio.to_thread(_live_runs)
    return runs[0] if runs else None


# ── Clone run counts ────────────────────────────────────────────────
#
# The instance switcher shows every running clone; users want each
# clone's active-run count next to its name ("nami 2” = nami is working
# on 2 background runs). Clones are frozen code copies without this
# router, so we count by exec'ing a tiny read-only SQLite query inside
# each clone container. Counts are cached (queries take ~200-300ms per
# clone; the UI polls every 5s) and dispatched in parallel.

CLONE_CACHE_SECONDS = 3.0
_clone_count_ts: dict[str, float] = {}
_clone_cache: dict[str, int] = {}
_clone_lock = asyncio.Lock()

# The script piped into each clone container. Mirrors the orphan logic
# above: RUNNING records in recently-touched sessions, minus runs that
# also hold a terminal record (stale RUNNING rows written at run start).
_CLONE_COUNT_SCRIPT = r'''
import json, sqlite3, time
conn = sqlite3.connect("file:/workspace/backend/agno.db?mode=ro", uri=True)
rows = conn.execute(
    "SELECT j.value AS rj FROM agno_sessions s, json_each(json_extract(s.runs, '$')) j "
    "WHERE s.updated_at > ? AND j.value IS NOT NULL",
    (int(time.time()) - 600,),
).fetchall()
conn.close()
running, terminal = set(), set()
for (rj,) in rows:
    try:
        run = json.loads(rj) if isinstance(rj, str) else rj
    except Exception:
        continue
    if not isinstance(run, dict):
        continue
    rid = run.get("run_id")
    if not rid:
        continue
    (running if run.get("status") == "RUNNING" else terminal).add(rid)
print(len(running - terminal))
'''


def _clone_container(name: str) -> str:
    return f"{name}-backend-1"


def _count_clone_runs(name: str) -> int:
    """Blocking helper (threadpool) — exec the count script in `name`'s container."""
    try:
        r = subprocess.run(
            ["docker", "exec", "-i", _clone_container(name), "python3", "-"],
            input=_CLONE_COUNT_SCRIPT.encode(),
            capture_output=True,
            timeout=8,
        )
        return int(r.stdout.strip().splitlines()[-1])
    except Exception:
        return 0


async def _clone_counts(names: list[str]) -> dict[str, int]:
    """Fresh counts for `names` not covered by the cache (parallel, threadpool)."""
    now = time.monotonic()
    stale = [n for n in names if now - _clone_count_ts.get(n, -1e9) > CLONE_CACHE_SECONDS]
    if not stale:
        return {}
    loop = asyncio.get_running_loop()
    results = await asyncio.gather(
        *(loop.run_in_executor(None, _count_clone_runs, n) for n in stale)
    )
    fresh = dict(zip(stale, results))
    async with _clone_lock:
        for n, c in fresh.items():
            _clone_count_ts[n] = now
            _clone_cache[n] = c
    return fresh


@router.get("/runs/clone-counts")
async def clone_run_counts():
    """Active-run count per running clone, from this instance's registry."""
    names: list[str] = []
    try:
        reg = json.loads(Path("/workspace/.clones/registry.json").read_text())
        names = [c["name"] for c in reg if c.get("status") == "running"]
    except Exception:
        names = []
    fresh = await _clone_counts(names)
    return JSONResponse({"data": {n: _clone_cache.get(n, 0) for n in names}})
