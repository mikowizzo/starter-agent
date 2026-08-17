#!/usr/bin/env python3
"""Ask the model crew — fire one query at multiple models in parallel.

Each model uses a bespoke route (Synthetic / ZAI / OpenRouter) — see CREW
below.

Usage:
  python ask_crew.py "your question here"
  python ask_crew.py --models x-ai/grok-4.6,google/gemini-3.7-flash "your question"
  python ask_crew.py --file path/to/code.py "review this"
  python ask_crew.py --file a.py --file b.py "compare these"
  python ask_crew.py --file README.md         # default: "please review"
  python ask_crew.py --history                # show recent runs from the ledger
  python ask_crew.py --history 20             # show last 20 runs
  python ask_crew.py --no-claims "quick question"  # skip claim cartography
  python ask_crew.py --heretic "..."          # + designated-heretic pass (extra Kimi call)
  python ask_crew.py --judge "..."            # + blind arena judging & Elo (extra calls)

Allowed --models ids (exact match; anything else is rejected with this list):
  hf:moonshotai/Kimi-K3    Kimi K3            (Synthetic)
  x-ai/grok-4.6            Grok 4.6           (OpenRouter)
  google/gemini-3.7-flash  Gemini 3.7 Flash   (OpenRouter)
  (default: all of the above)

FILES (--file PATH, repeatable):
  File contents are inlined into the prompt for every model. All inlined
  files together must fit the per-RUN budget MAX_RUN_BYTES (300 KB ≈ 75k
  tokens). Over budget → the run FAILS CLOSED with a per-file size report
  (never a silent head-only review). Raise it with --max-bytes N — a single
  large file then goes through whole — or opt into head-sampling every file
  with --force-truncate (loud warnings; models will NOT see full files).

DESIGNATED HERETIC (opt-in: --heretic):
  Crew models tend to converge — overlapping training data means they can
  all be wrong the same way. With --heretic, Kimi K3 (when in --models)
  makes a SECOND call as the "designated heretic": forced to assume the
  consensus is wrong and argue the strongest objection. The verdict prints
  last, clearly marked. Extra call = extra cost, hence opt-in.

BLIND ARENA + ELO (opt-in: --judge):
  After the crew answers, pairs are judged blind by a recused judge and Elo
  ratings update in the ledger. --judge-swap double-judges each pair to
  catch position bias. --elo [cat] shows the leaderboard, --elo-rebuild
  replays history, --audit-bias prints the judge bias audit. Extra calls
  cost money, hence opt-in.

LEDGER:
  Every run is persisted to a SQLite ledger (default: <repo>/data/crew/
  crew_ledger.db, or $CREW_LEDGER_DB). Stores run metadata, per-model
  responses, claims, and match/Elo history. Disable with CREW_LEDGER=off.

Reads SYNTHETIC_API_KEY (Kimi K3) and OPENROUTER_API_KEY
(Grok 4.6, Gemini 3.7 Flash) from the env.
"""
import concurrent.futures
import hashlib
import socket
import itertools
import json
import os
import re
import secrets
import sqlite3
import string
import sys
import textwrap
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

BASE_URL = "https://opencode.ai/zen/go/v1/chat/completions"
KEY_ENV = "OPENCODE_API_KEY"

# The model crew: (model id, display label, [base_url, api_key_env],
#   input_price_per_1m, output_price_per_1m).
# Two-tuples (id, label) route via OpenCode (BASE_URL + OPENCODE_API_KEY) at
# $0 pricing; six-tuples override the route and pricing — e.g. Kimi K3 goes
# through the Synthetic API instead.
CREW = [
    ("hf:moonshotai/Kimi-K3", "Kimi K3 (Synthetic)",
     "https://api.synthetic.new/v1", "SYNTHETIC_API_KEY",
     2.80, 14.00),
    ("x-ai/grok-4.6", "Grok 4.6 (OpenRouter)",
     "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY",
     2.00, 6.00),
    ("google/gemini-3.7-flash", "Gemini 3.7 Flash (OpenRouter)",
     "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY",
     0.75, 3.75),
]

# Checkpoint directory for streaming partial results
CHECKPOINT_ROOT = None  # resolved lazily via _checkpoint_root()


def _checkpoint_root() -> Path:
    """data/crew/runs/ — sibling of the ledger DB."""
    global CHECKPOINT_ROOT
    if CHECKPOINT_ROOT is not None:
        return CHECKPOINT_ROOT
    repo_root = Path(__file__).resolve().parents[5]
    CHECKPOINT_ROOT = repo_root / "data" / "crew" / "runs"
    CHECKPOINT_ROOT.mkdir(parents=True, exist_ok=True)
    return CHECKPOINT_ROOT


def _model_slug(model_id: str) -> str:
    """Filesystem-safe slug for a model id."""
    import re as _re
    return _re.sub(r"[^A-Za-z0-9_.-]+", "_", model_id).strip("_") or "model"


# Designated heretic — hardcoded, always Kimi K3. Inspired by intelligence
# analysis's "tenth man rule": when everyone agrees, someone is assigned to
# assume the agreement is wrong. Kimi still answers normally as part of the
# crew; this is an ADDITIONAL adversarial call on the same prompt.
HERETIC_MODEL_ID = "hf:moonshotai/Kimi-K3"

# Main system prompt for crew models. Asks for structured CLAIMS: section at
# the end so the local cartography engine can extract and compare them.
MAIN_SYSTEM_PROMPT = (
    "You are one member of a small panel of expert models answering a user's "
    "question independently. Give your honest, complete, direct answer.\n\n"
    "FORMATTING CONTRACT (mandatory):\n"
    "At the very end of your response, include a CLAIMS: section with one "
    "atomic claim per line, each prefixed exactly with \"CLAIM: \".\n"
    "Rules for claims:\n"
    "  - One single, self-contained assertion per line. No compound sentences.\n"
    "  - Each claim must stand alone without the rest of your answer.\n"
    "  - Capture your key factual statements, recommendations, and conclusions.\n"
    "  - Use plain wording so the same claim from another model reads nearly "
    "identically — shared nouns and verbs.\n"
    "  - Write between 3 and 10 claims. Quality over quantity.\n\n"
    "Example tail of your response:\n\n"
    "CLAIMS:\n"
    "CLAIM: SQLite WAL mode is sufficient for this write volume.\n"
    "CLAIM: The bottleneck is disk I/O, not CPU.\n"
)

HERETIC_SYSTEM_PROMPT = (
    "You are the DESIGNATED HERETIC on a panel of AI models answering the "
    "same question. The other models will likely converge on a consensus "
    "answer — and that consensus may be wrong.\n"
    "Your job:\n"
    "- Assume the obvious/consensus answer is WRONG or incomplete.\n"
    "- Construct the strongest possible objection to it (steelman the "
    "opposing view).\n"
    "- Hunt for what the others will miss: edge cases, security issues, "
    "false assumptions, logical fallacies, hidden costs, and failure modes.\n"
    "- Be adversarial but constructive: every objection should point toward "
    "a better answer, not just tear things down.\n"
    "Do NOT hedge or summarize both sides. Commit to the strongest "
    "counter-argument.\n\n"
    "FORMATTING CONTRACT (mandatory):\n"
    "At the very end of your response, include a DISSENT: section with one "
    "atomic objection per line, each prefixed exactly with \"DISSENT: \".\n"
    "Rules for dissent lines:\n"
    "  - One single, self-contained objection per line.\n"
    "  - Each line must target the specific claim or assumption it attacks, "
    "reusing the claim's key nouns and verbs so the disagreement can be "
    "mechanically matched.\n"
    "  - Write between 2 and 8 dissent lines.\n\n"
    "Example tail of your response:\n\n"
    "DISSENT:\n"
    "DISSENT: WAL mode degrades badly on network filesystems.\n"
    "DISSENT: In-place migration fails at table-lock time beyond 10M rows.\n"
)


# ---------------------------------------------------------------------------
# SQLite Ledger
#
# Lean schema, versioned via PRAGMA user_version for clean migrations. Money
# stored as integer micro-USD (avoids float-rounding bugs). Heretic is a
# response with role='heretic', not special columns. --history is a plain
# ORDER BY ts DESC — no FTS5 (nothing ever queried it; dropped 2026-08-16,
# existing DBs keep their harmless runs_fts table untouched).
# ---------------------------------------------------------------------------

SCHEMA_VERSION = 3

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS runs (
    run_id            TEXT PRIMARY KEY,
    ts                INTEGER NOT NULL,             -- unix epoch seconds (UTC)
    cwd               TEXT,
    query             TEXT NOT NULL,
    files_json        TEXT NOT NULL DEFAULT '[]',   -- JSON array of paths
    config_json       TEXT NOT NULL DEFAULT '{}',   -- model list, params (NO credentials)
    total_cost_micros INTEGER,                      -- NULL if unknown
    wall_seconds      REAL,
    status            TEXT NOT NULL DEFAULT 'ok'
                      CHECK (status IN ('ok','partial','error'))
);

CREATE TABLE IF NOT EXISTS responses (
    response_id       INTEGER PRIMARY KEY AUTOINCREMENT,  -- stable FK for Elo/votes/claims
    run_id            TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    model_id          TEXT NOT NULL,
    role              TEXT NOT NULL DEFAULT 'council'
                      CHECK (role IN ('council','heretic')),
    response_text     TEXT,                           -- NULL on provider error
    prompt_tokens     INTEGER,                        -- nullable: not every provider reports
    completion_tokens INTEGER,
    cost_micros       INTEGER,                        -- snapshot at write time
    elapsed_seconds   REAL,
    error             TEXT                            -- non-NULL if this model failed
);
CREATE INDEX IF NOT EXISTS idx_responses_run   ON responses(run_id);
CREATE INDEX IF NOT EXISTS idx_responses_model ON responses(model_id, run_id);
CREATE INDEX IF NOT EXISTS idx_runs_ts         ON runs(ts);
"""

SCHEMA_V3 = """
CREATE TABLE IF NOT EXISTS matches (
    match_id        TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    category        TEXT NOT NULL DEFAULT 'general',
    response_id_a   INTEGER REFERENCES responses(response_id),
    response_id_b   INTEGER REFERENCES responses(response_id),
    model_a         TEXT NOT NULL,
    model_b         TEXT NOT NULL,
    judge_model     TEXT NOT NULL,
    judge_recused   INTEGER NOT NULL DEFAULT 0,
    presented_a_is  TEXT NOT NULL CHECK (presented_a_is IN ('model_a','model_b')),
    verdict         TEXT NOT NULL CHECK (verdict IN ('A','B','tie')),
    winner_model    TEXT,
    score_model_a   REAL NOT NULL CHECK (score_model_a IN (0.0, 0.5, 1.0)),
    confidence      INTEGER NOT NULL DEFAULT 3 CHECK (confidence BETWEEN 1 AND 5),
    swap_consistent INTEGER,
    judge_cost_usd  REAL NOT NULL DEFAULT 0,
    judge_reason    TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_matches_run    ON matches(run_id);
CREATE INDEX IF NOT EXISTS idx_matches_models ON matches(model_a, model_b);
CREATE INDEX IF NOT EXISTS idx_matches_cat    ON matches(category);

CREATE TABLE IF NOT EXISTS ratings (
    model_id   TEXT NOT NULL,
    category   TEXT NOT NULL DEFAULT '*',
    rating     REAL NOT NULL DEFAULT 1500.0,
    games      INTEGER NOT NULL DEFAULT 0,
    wins       INTEGER NOT NULL DEFAULT 0,
    losses     INTEGER NOT NULL DEFAULT 0,
    ties       INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (model_id, category)
);
"""

SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS claims (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    response_id       INTEGER,                          -- nullable: may not be in ledger
    run_id            TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    model_id          TEXT NOT NULL,
    role              TEXT NOT NULL DEFAULT 'council',
    kind              TEXT NOT NULL DEFAULT 'claim',   -- 'claim' | 'dissent'
    claim_text        TEXT NOT NULL,                    -- verbatim, never paraphrased
    normalized        TEXT NOT NULL,                    -- for clustering comparison
    cluster_id        INTEGER,
    status            TEXT NOT NULL DEFAULT 'unique'   -- consensus|majority|disputed|dissent|unique
);
CREATE INDEX IF NOT EXISTS idx_claims_run      ON claims(run_id);
CREATE INDEX IF NOT EXISTS idx_claims_response ON claims(response_id);
CREATE INDEX IF NOT EXISTS idx_claims_cluster  ON claims(cluster_id);
"""

_LEDGER_DISABLED = False  # sticky: one write failure disables writes for the process


def _default_db_path() -> str:
    """Resolve the ledger path.

    Priority: CREW_LEDGER_DB env var > repo-adjacent data/crew/crew_ledger.db.

    Repo-adjacent (not XDG) so the ledger survives container restarts, stays
    visible to backup tooling, and is never inside the skill folder that gets
    docker-cp'd to clones (which would clobber or fork the DB).
    """
    if p := os.environ.get("CREW_LEDGER_DB"):
        return p
    # This file: <repo>/backend/app/skills/ask-crew/scripts/ask_crew.py
    repo_root = Path(__file__).resolve().parents[5]
    return str(repo_root / "data" / "crew" / "crew_ledger.db")


def _ledger_enabled() -> bool:
    return os.environ.get("CREW_LEDGER", "on").lower() not in ("off", "0", "false")


def _open_ledger(path: str | None = None) -> sqlite3.Connection:
    """Open (and migrate if needed) the ledger DB. Can raise — caller handles."""
    path = path or _default_db_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < 1:
        conn.executescript(SCHEMA_SQL)
    if version < 2:
        conn.executescript(SCHEMA_V2)
    if version < 3:
        conn.executescript(SCHEMA_V3)
    if version < SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return conn


def _usd_to_micros(usd: float | None) -> int | None:
    return None if usd is None else int(round(usd * 1_000_000))


def record_run(
    query: str,
    files: list[str],
    models_asked: list[str],
    responses: list[dict],
    wall_seconds: float,
    status: str = "ok",
) -> str | None:
    """Persist one invocation to the ledger. Returns run_id, or None on failure.

    Never raises — a ledger failure must not kill the main ask flow. On first
    failure, writes are sticky-disabled for the rest of the process and a
    WARNING is printed to stderr (loud once, then silent).
    """
    global _LEDGER_DISABLED
    if _LEDGER_DISABLED or not _ledger_enabled():
        return None
    try:
        conn = _open_ledger()
        run_id = uuid.uuid4().hex
        total = sum(r.get("cost", 0) for r in responses if r.get("cost") is not None)
        config = {"models": models_asked}  # no credentials, ever
        with conn:  # single transaction: run row + all response rows
            conn.execute(
                "INSERT INTO runs(run_id, ts, cwd, query, files_json, "
                "config_json, total_cost_micros, wall_seconds, status) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (run_id, int(time.time()), os.getcwd(), query,
                 json.dumps(files), json.dumps(config),
                 _usd_to_micros(total), wall_seconds, status),
            )
            conn.executemany(
                "INSERT INTO responses(run_id, model_id, role, response_text, "
                "prompt_tokens, completion_tokens, cost_micros, "
                "elapsed_seconds, error) VALUES (?,?,?,?,?,?,?,?,?)",
                [(run_id, r["model_id"], r.get("role", "council"),
                  r.get("text"), r.get("prompt_tokens"),
                  r.get("completion_tokens"), _usd_to_micros(r.get("cost")),
                  r.get("elapsed_seconds"), r.get("error"))
                 for r in responses],
            )
        conn.close()
        return run_id
    except Exception as e:
        _LEDGER_DISABLED = True
        print(f"[ledger] WARNING: write failed — ledger disabled for this "
              f"process: {e}", file=sys.stderr)
        return None


def cmd_history(limit: int = 10) -> int:
    """Print recent runs from the ledger."""
    try:
        conn = _open_ledger()
    except Exception as e:
        print(f"[ledger] ERROR: cannot open ledger ({e})", file=sys.stderr)
        return 1
    rows = conn.execute(
        "SELECT run_id, ts, query, total_cost_micros, status, wall_seconds, "
        "(SELECT group_concat(model_id || "
        "  CASE role WHEN 'heretic' THEN '*' ELSE '' END, ', ') "
        "  FROM responses WHERE run_id = runs.run_id) AS models "
        "FROM runs ORDER BY ts DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    if not rows:
        note = " (ledger writes may have failed recently)" if _LEDGER_DISABLED else ""
        print(f"No runs recorded yet.{note}")
        return 0
    print(f"{'ID':<10} {'TIMESTAMP':<18} {'COST':>10}  {'MODELS':<40} QUERY")
    print("-" * 110)
    for r in rows:
        cost = "?" if r["total_cost_micros"] is None else f"${r['total_cost_micros']/1e6:.4f}"
        ts_str = time.strftime("%Y-%m-%d %H:%M", time.gmtime(r["ts"]))
        q = " ".join(r["query"].split())[:50]
        print(f"{r['run_id'][:8]:<10} {ts_str:<18} {cost:>10}  "
              f"[{r['models'] or ''}]  {q}")
    return 0


# ---------------------------------------------------------------------------
# Core ask-crew logic
# ---------------------------------------------------------------------------

def route(entry: tuple) -> tuple[str, str]:
    """(full endpoint url, api_key_env) for a crew entry; default = OpenCode."""
    if len(entry) >= 4:
        base, key_env = entry[2], entry[3]
        url = base if base.endswith("/chat/completions") else base.rstrip("/") + "/chat/completions"
        return url, key_env
    return BASE_URL, KEY_ENV

# Streaming + checkpointing timeouts
IDLE_TIMEOUT = 90        # max seconds of silence between SSE chunks (dead connection)
WALL_CLOCK_CAP = 1200    # absolute per-stream cap (20 min) — ensures threads provably die
QUORUM_DEADLINE = 510    # inner deadline (8.5 min) — returns before agent's 10-min kill
QUORUM_COUNT = 2         # return once this many council answers are in (or deadline)
ASK_MAX_ATTEMPTS = 3     # 1 try + 2 retries, only BEFORE the first token arrives

# HTTP statuses worth a retry (rate limits + transient server errors)
_RETRYABLE_HTTP = frozenset({408, 425, 429, 500, 502, 503, 504})


def _retryable(e: Exception) -> bool:
    """Transient failure worth a retry? (HTTPError is a URLError subclass,
    so it must be checked first.)"""
    if isinstance(e, urllib.error.HTTPError):
        return e.code in _RETRYABLE_HTTP
    return isinstance(
        e, (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError)
    )


MAX_RUN_BYTES = 300_000  # per-RUN budget for all inlined files (~75k tokens) — fail closed
CHARS_PER_TOKEN = 4

# Capability routing: models differ in context size. A model whose context
# is smaller than the prompt gets a structural summary (file tree + symbol
# outline) instead of the full file bodies — not a silent head-only copy,
# and no pre-splitting by the caller. Extend as crew models change.
MODEL_CONTEXT_CHARS = {
    "hf:moonshotai/Kimi-K3": 1_000_000 * CHARS_PER_TOKEN,
    "x-ai/grok-4.6": 2_000_000 * CHARS_PER_TOKEN,
    "google/gemini-3.7-flash": 1_000_000 * CHARS_PER_TOKEN,
}
OUTLINE_BUDGET = 30_000  # per-model cap for structural outlines (~7.5k tokens)

HEADERS = {
    "Authorization": "Bearer {key}",
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
}


def get_env_key(name: str) -> str:
    """Read a key from the environment, falling back to /workspace/.env."""
    key = os.environ.get(name, "")
    if key:
        return key
    env_file = Path("/workspace/.env")
    if env_file.is_file():
        try:
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if line.startswith(name + "="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
        except OSError:
            pass
    return ""


_DEF_RE = re.compile(
    r"^(?:class|def|async\s+def|function|const|let|var|type|interface|enum|struct|impl|trait|pub\s+fn|fn)\b[^\n]*",
    re.MULTILINE,
)


def file_outline(path: str, budget: int = OUTLINE_BUDGET) -> str:
    """Structural summary of a source file: every def/class/declaration
    line + key metadata, no bodies. Deterministic, local, zero API calls.
    Used for capability routing when the full pack exceeds a model's
    context."""
    try:
        raw = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return f"--- file: {path} (unreadable: {e}) ---"
    lines = raw.splitlines()
    hits = [
        (i + 1, ln.rstrip())
        for i, ln in enumerate(lines)
        if _DEF_RE.match(ln)
    ]
    body = "\n".join(f"  L{i}: {ln}" for i, ln in hits)
    if len(body) > budget:
        body = body[:budget] + f"\n... ({len(hits)} symbols total, outline capped)"
    return (
        f"--- outline: {path} ({len(lines)} lines, {len(raw):,} bytes; "
        f"structural summary — full file not inlined for this model) ---\n"
        f"{body}\n--- end outline {path} ---"
    )


def _stable_path_key(path: str) -> str:
    """Deterministic sort key for a file path.

    Uses sha256 of the path string — NOT built-in hash(), which is salted by
    PYTHONHASHSEED and changes across processes. A stable ordering of file
    blocks keeps the prompt prefix byte-for-byte identical between runs, so
    provider-side prompt caching (KV-cache reuse) actually engages.
    """
    return hashlib.sha256(path.encode("utf-8")).hexdigest()


def read_file_block(path: str) -> str | None:
    """Read a file and return a prompt-ready block, or None if unusable.

    Reads the WHOLE file — no per-file cap. Oversize is enforced per-RUN in
    build_prompt (fail closed), so a single big file goes through whole
    when it's the only one.
    """
    p = Path(path)
    if not p.is_file():
        alt = Path("/workspace") / path
        if alt.is_file():
            p = alt
        else:
            return None
    try:
        size = p.stat().st_size
        raw = p.read_bytes()
    except OSError as e:
        print(f"WARNING: cannot read {path}: {e}", file=sys.stderr)
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return f"--- file: {path} ({size} bytes; binary, not inlined) ---\n--- end {path} ---"
    return f"--- file: {path} ({size} bytes) ---\n{text}\n--- end {path} ---"


def build_prompt(query: str, files: list[str], max_bytes: int = MAX_RUN_BYTES,
                 force_truncate: bool = False) -> str:
    """Combine inlined file blocks with the user's question into one prompt.

    File blocks are emitted in deterministic order (sorted by stable sha256
    of the path) so provider KV-cache reuse engages on repeat runs.

    Fail-closed per-RUN budget: if all inlined files together exceed
    max_bytes, REFUSE (exit 2) with a per-file size report — never a
    silent head-only review. --force-truncate opts into head-sampling
    every file to fit the budget (loud warnings).
    """
    blocks: list[tuple[str, str, int]] = []  # (path, block, size_bytes)
    for path in sorted(files, key=_stable_path_key):
        block = read_file_block(path)
        if block is None:
            print(f"WARNING: file not found or not readable: {path}", file=sys.stderr)
            continue
        blocks.append((path, block, len(block.encode("utf-8"))))
    if not query and blocks:
        query = "Please review the file(s) above."
    if not blocks:
        return query

    total = sum(sz for _, _, sz in blocks)
    if total > max_bytes:
        if not force_truncate:
            report = "\n".join(
                f"  {sz:>9,}  {p}" for p, _, sz in sorted(
                    blocks, key=lambda t: -t[2])
            )
            print(
                f"ERROR: inlined files total {total:,} bytes but the per-run "
                f"budget is {max_bytes:,} bytes (~{total // CHARS_PER_TOKEN:,} "
                f"tokens would hit every model).\n"
                f"Refusing to run a silent partial review. Options:\n"
                f"  --max-bytes N     raise the budget (whole files, more cost)\n"
                f"  --force-truncate  head-sample every file to fit (models "
                f"will NOT see full files)\n"
                f"Files by size:\n{report}",
                file=sys.stderr,
            )
            sys.exit(2)
        print(
            f"WARNING: --force-truncate — inlined files total {total:,} bytes, "
            f"head-sampling every file to fit {max_bytes:,} bytes. "
            f"Models will NOT see full files.",
            file=sys.stderr,
        )
        # ponytail: equal share per file; a proportional allocator is
        # overkill — tiny files keep everything, big ones get equal shares.
        share = max_bytes // len(blocks)
        kept: list[str] = []
        for path, block, sz in blocks:
            if sz <= share:
                kept.append(block)
                continue
            b = block.encode("utf-8")[:share]
            nl = b.rfind(b"\n")  # cut on a line boundary when cheap
            if nl > share // 2:
                b = b[:nl]
            text = b.decode("utf-8", "replace")
            kept.append(
                f"--- file: {path} (TRUNCATED: kept first {len(b):,} of "
                f"{sz:,} bytes) ---\n{text}\n--- end {path} ---"
            )
            print(
                f"WARNING: truncated {path}: kept {len(b):,} of "
                f"{sz:,} bytes",
                file=sys.stderr,
            )
        rendered = kept
    else:
        rendered = [block for _, block, _ in blocks]

    est_tokens = sum(len(b) for b in rendered) // CHARS_PER_TOKEN
    print(
        f"📎 Files sorted for cache reuse "
        f"({len(rendered)} blocks, ~{est_tokens:,} tokens prefix)"
    )
    rendered.append(f"--- question ---\n{query}")
    return "\n\n".join(rendered)


def ask(
    model_id: str,
    query: str,
    base_url: str,
    key: str,
    system: str | None = None,
    role_tag: str = "council",
    run_id: str | None = None,
    attempt: int = 1,
) -> tuple[str, str, float, dict]:
    """Ask one model via SSE streaming with disk checkpointing.

    Every content delta is immediately appended (line-buffered) to
    data/crew/runs/<run_id>/<role>_<model_slug>.jsonl so partial work
    survives process kills. A .final marker sibling is written on success;
    any .jsonl without a .final is recoverable partial work.

    Returns (model_id, content_or_error, elapsed_seconds, usage).
    """
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": query})
    body = json.dumps({
        "model": model_id,
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},  # heretic catch: usage not sent without this
    }).encode()
    req = urllib.request.Request(
        base_url,
        data=body,
        method="POST",
        headers={k: v.format(key=key) for k, v in HEADERS.items()},
    )

    # Checkpoint paths
    ckpt_dir = _checkpoint_root()
    if run_id is None:
        run_id = uuid.uuid4().hex[:12]
    ckpt_subdir = ckpt_dir / run_id
    ckpt_subdir.mkdir(parents=True, exist_ok=True)
    slug = f"{role_tag}_{_model_slug(model_id)}"
    jsonl_path = ckpt_subdir / f"{slug}.jsonl"
    final_path = ckpt_subdir / f"{slug}.final"

    t0 = time.monotonic()
    parts: list[str] = []
    usage: dict = {"prompt_tokens": 0, "completion_tokens": 0}

    try:
        # urlopen timeout = per-read (idle) timeout, not wall-clock.
        # 90s silence between chunks = dead connection.
        resp = urllib.request.urlopen(req, timeout=IDLE_TIMEOUT)
        with resp, open(jsonl_path, "w", buffering=1, encoding="utf-8") as ckpt:
            first_token = True
            for raw_line in resp:
                # Heretic catch: check wall-clock cap so threads provably die
                elapsed = time.monotonic() - t0
                if elapsed > WALL_CLOCK_CAP:
                    raise TimeoutError(
                        f"wall-clock cap {WALL_CLOCK_CAP}s exceeded"
                    )
                line = raw_line.decode("utf-8", "replace").strip()
                if not line or line.startswith(":"):
                    continue
                if not line.startswith("data:"):
                    continue
                payload = line[len("data:"):].strip()
                if payload == "[DONE]":
                    break
                try:
                    evt = json.loads(payload)
                except json.JSONDecodeError:
                    continue

                # Usage may appear in any event, but typically the final one
                if isinstance(evt.get("usage"), dict):
                    u = evt["usage"]
                    usage = {
                        "prompt_tokens": u.get("prompt_tokens", 0),
                        "completion_tokens": u.get("completion_tokens", 0),
                    }

                # Heretic catch: choices can be EMPTY on trailing usage-only chunk
                choices = evt.get("choices") or []
                if not choices:
                    continue
                delta = (choices[0].get("delta") or {}).get("content") or ""
                if delta:
                    # Heretic catch: generous pre-first-token tolerance
                    if first_token:
                        first_token = False
                    parts.append(delta)
                    ckpt.write(json.dumps(
                        {"ts": time.time(), "chunk": delta},
                        ensure_ascii=False,
                    ) + "\n")  # line-buffered: flushed immediately

    except (socket.timeout, TimeoutError) as e:
        elapsed = time.monotonic() - t0
        kept = sum(len(p) for p in parts)
        # Retry transient failures, but ONLY before any content arrived —
        # past first token a retry would duplicate the checkpoint stream.
        if not parts and attempt < ASK_MAX_ATTEMPTS and _retryable(e):
            time.sleep(min(2 ** attempt, 8))  # 2s, 4s — capped, with backoff
            return ask(model_id, query, base_url, key, system, role_tag,
                       run_id, attempt + 1)
        return (
            model_id,
            f"ERROR: {type(e).__name__}: {e} "
            f"({kept} chars checkpointed to {jsonl_path})",
            elapsed,
            usage,
        )
    except Exception as e:
        elapsed = time.monotonic() - t0
        kept = sum(len(p) for p in parts)
        if not parts and attempt < ASK_MAX_ATTEMPTS and _retryable(e):
            time.sleep(min(2 ** attempt, 8))
            return ask(model_id, query, base_url, key, system, role_tag,
                       run_id, attempt + 1)
        return (
            model_id,
            f"ERROR: {type(e).__name__}: {e} "
            f"({kept} chars checkpointed to {jsonl_path})",
            elapsed,
            usage,
        )

    # Success: write .final marker atomically (heretic: marker as SIBLING, not rename)
    content = "".join(parts)
    elapsed = time.monotonic() - t0
    final_doc = {
        "model": model_id,
        "elapsed_s": round(elapsed, 3),
        "usage": usage,
        "chars": len(content),
    }
    tmp = str(final_path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(final_doc, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, final_path)

    return model_id, content.strip() or "(empty reply)", elapsed, usage


# ---------------------------------------------------------------------------
# Claim Cartography — local, deterministic, zero extra model calls
# (Option A: structured claims at source, verbatim display)
# ---------------------------------------------------------------------------

# Regex tolerates optional bullet markers and case variance before the prefix.
_CLAIM_LINE_RE = re.compile(
    r"^\s*(?:[-*~•]\s*|\d+[.)]\s*)?(CLAIM|DISSENT)\s*:\s*(.+?)\s*$",
    re.IGNORECASE,
)
_PUNCT_TABLE = str.maketrans("", "", string.punctuation)
_CLAIM_STOPWORDS = frozenset(
    "the a an and or but if then that this is are was were be been it its of to "
    "in for on with as at by from can could should would may might will per "
    "not no very more much when while does do did has have had than also into"
    .split()
)

CLAIM_SIMILARITY_THRESHOLD = 0.35


def extract_claims_tagged(text: str, role: str) -> list[tuple[str, str]]:
    """Pull CLAIM:/DISSENT: lines from a response.

    Returns list of (kind, verbatim_text). kind is 'dissent' for DISSENT:
    lines (or any prefixed line from the heretic), else 'claim'.
    """
    if not text:
        return []
    preferred = "DISSENT" if role == "heretic" else "CLAIM"
    found = []
    for line in text.splitlines():
        m = _CLAIM_LINE_RE.match(line)
        if not m:
            continue
        tag = m.group(1).upper()
        body = m.group(2).strip().strip("*`")
        if not body:
            continue
        kind = "dissent" if (tag == "DISSENT" or role == "heretic") else "claim"
        found.append((0 if tag == preferred else 1, kind, body))
    # Stable sort: preferred-prefix lines first, preserving line order.
    found.sort(key=lambda t: t[0])
    return [(kind, body) for _, kind, body in found]


def normalize_claim(text: str) -> str:
    """Lowercase, strip punctuation, remove stopwords, collapse whitespace.

    Used only for clustering comparison — the original verbatim text is
    always preserved for display.
    """
    tokens = text.lower().translate(_PUNCT_TABLE).split()
    return " ".join(t for t in tokens if t not in _CLAIM_STOPWORDS)


def _similarity(tokens_a: set, tokens_b: set) -> float:
    """max(Jaccard, containment*0.9). Containment rescues pairs where one
    claim is a superset of another, which Jaccard punishes."""
    if not tokens_a or not tokens_b:
        return 0.0
    inter = len(tokens_a & tokens_b)
    if inter == 0:
        return 0.0
    jaccard = inter / len(tokens_a | tokens_b)
    containment = inter / min(len(tokens_a), len(tokens_b))
    return max(jaccard, containment * 0.9)


def cluster_claims(all_claims: list[dict]) -> list[dict]:
    """Greedy single-linkage clustering by token-overlap similarity.

    Deterministic: input order is fixed (crew order, then heretic, then
    line order). Each claim joins the best-matching existing cluster if
    similarity >= threshold, else seeds a new one.

    Returns list of {"cluster_id": int, "members": [claim_dict, ...]}.
    """
    for c in all_claims:
        c["_tokens"] = set(c["normalized"].split())

    clusters: list[dict] = []
    for c in all_claims:
        best_idx, best_score = -1, 0.0
        for i, cl in enumerate(clusters):
            score = max(_similarity(c["_tokens"], m["_tokens"]) for m in cl["members"])
            if score > best_score:
                best_idx, best_score = i, score
        if best_score >= CLAIM_SIMILARITY_THRESHOLD:
            clusters[best_idx]["members"].append(c)
        else:
            clusters.append({"members": [c]})

    for i, cl in enumerate(clusters):
        cl["cluster_id"] = i + 1
        for m in cl["members"]:
            m.pop("_tokens", None)
    return clusters


def classify_cluster(cluster: dict, crew_count: int) -> str:
    """consensus / majority / disputed / dissent / unique.

    A cluster mixing crew CLAIMs with heretic DISSENTs is a dispute.
    Heretic-only clusters are 'dissent' (unanswered attack surface).
    """
    members = cluster["members"]
    supporters = {m["model_id"] for m in members if m["kind"] == "claim"}
    dissenters = {m["model_id"] for m in members if m["kind"] == "dissent"}

    if supporters and dissenters:
        return "disputed"
    if dissenters:
        return "dissent"
    if crew_count > 1 and len(supporters) >= crew_count:
        return "consensus"
    if len(supporters) >= 2:
        return "majority"
    return "unique"


_STATUS_ORDER = ["consensus", "majority", "disputed", "dissent", "unique"]
_STATUS_TITLES = {
    "consensus": "CONSENSUS — all crew models support",
    "majority":  "MAJORITY — 2+ crew models support",
    "disputed":  "DISPUTED — crew claim vs heretic dissent",
    "dissent":   "HERETIC DISSENT — unanswered attack surface",
    "unique":    "UNIQUE — singleton insights",
}
_STATUS_ICONS = {"consensus": "✅", "majority": "➖", "disputed": "⚔️", "dissent": "🎭", "unique": "💡"}


def display_claim_map(clusters: list[dict], total_claims: int, responder_count: int) -> None:
    """Render the claim cartography matrix to stdout.

    VERBATIM quotes only, with response attribution. A bad cluster is
    visibly a bad cluster of literal quotes — never a paraphrase.
    """
    w = 78
    print()
    print("=" * w)
    print(f"📋 CLAIM CARTOGRAPHY — {total_claims} claims from {responder_count} responses "
          f"→ {len(clusters)} clusters")
    print(f"   (deterministic local clustering, verbatim quotes; threshold {CLAIM_SIMILARITY_THRESHOLD})")
    print("=" * w)

    for status in _STATUS_ORDER:
        group = [c for c in clusters if c["status"] == status]
        if not group:
            continue
        print()
        print(f"{_STATUS_ICONS[status]} {_STATUS_TITLES[status]} — {len(group)} cluster(s)")
        print("-" * w)
        for cl in group:
            print(f"  Cluster #{cl['cluster_id']}:")
            supporters = [m for m in cl["members"] if m["kind"] == "claim"]
            dissenters = [m for m in cl["members"] if m["kind"] == "dissent"]
            for m in supporters:
                print(textwrap.fill(
                    f"  + [{m['label']}] \"{m['text']}\"",
                    width=w, initial_indent="    ", subsequent_indent="      "))
            for m in dissenters:
                print(textwrap.fill(
                    f"  - [{m['label']}] \"{m['text']}\"",
                    width=w, initial_indent="    ", subsequent_indent="      "))
    print()
    print("=" * w)


def _store_claims(conn, run_id: str, clusters: list[dict]) -> None:
    """Persist extracted claims to the ledger (claims table).

    response_id may be None if the lookup didn't find a match (e.g. heretic
    reuses the same model_id as a crew member); claims are still useful for
    display and re-rendering even without the FK.
    """
    for cl in clusters:
        for m in cl["members"]:
            conn.execute(
                "INSERT INTO claims (response_id, run_id, model_id, role, "
                "kind, claim_text, normalized, cluster_id, status) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (m.get("response_id"), run_id, m["model_id"], m["role"],
                 m["kind"], m["text"], m["normalized"],
                 cl["cluster_id"], cl["status"]),
            )
    conn.commit()


def run_claim_cartography(
    ok_results: list[dict],
    run_id: str,
    crew_count: int,
) -> None:
    """Extract, cluster, classify, persist, and display the claim map.

    Silently no-ops if models didn't follow the CLAIMS/DISSENT format.
    """
    all_claims = []
    for r in ok_results:
        for kind, body in extract_claims_tagged(r["text"], r.get("role", "council")):
            all_claims.append({
                "model_id": r["model_id"],
                "label": r.get("label", r["model_id"]),
                "role": r.get("role", "council"),
                "kind": kind,
                "text": body,
                "normalized": normalize_claim(body),
                "response_id": r.get("response_id"),
            })

    if not all_claims:
        return  # models didn't follow the format — silently skip

    clusters = cluster_claims(all_claims)
    for cl in clusters:
        cl["status"] = classify_cluster(cl, crew_count)

    # Persist to ledger
    try:
        conn = _open_ledger()
        _store_claims(conn, run_id, clusters)
        conn.close()
    except Exception as e:
        print(f"[ledger] WARNING: claims write failed: {e}", file=sys.stderr)

    display_claim_map(clusters, len(all_claims), len(ok_results))


JUDGE_MODEL_ID = HERETIC_MODEL_ID  # Kimi K3: hardcoded default judge
ELO_START = 1500.0
ELO_SCALE = 400.0

ARENA_CATEGORIES = ('coding', 'reasoning', 'creative', 'math', 'general')

_CATEGORY_HINTS = {
    'coding':    r'\b(code|function|python|javascript|sql|regex|debug|refactor|compile|api|bug|class|import)\b',
    'math':      r'\b(calculate|integral|derivative|probability|equation|solve|algebra|proof|theorem|\d+\s*[+\-*/]\s*\d+)\b',
    'reasoning': r'\b(why|reason|logic|argue|infer|implies|paradox|assume|therefore|fallacy|deduce)\b',
    'creative':  r'\b(story|poem|write a|imagine|fiction|haiku|screenplay|metaphor|brainstorm)\b',
}


def classify_category(question: str) -> str:
    q = question.lower()
    for cat, pat in _CATEGORY_HINTS.items():
        if re.search(pat, q):
            return cat
    return 'general'


def elo_k(games: int) -> float:
    if games < 30:
        return 32.0
    if games < 100:
        return 24.0
    return 16.0


def elo_expected(ra: float, rb: float) -> float:
    return 1.0 / (1.0 + 10.0 ** ((rb - ra) / ELO_SCALE))


def elo_update(ra, rb, score_a, games_a, games_b):
    ea = elo_expected(ra, rb)
    return (ra + elo_k(games_a) * (score_a - ea),
            rb + elo_k(games_b) * ((1.0 - score_a) - (1.0 - ea)))


def pick_judge(model_id_a: str, model_id_b: str):
    """Kimi is the hardcoded judge. If Kimi is a defendant, the third crew
    member — not in this pair — takes the bench. Returns (judge_cfg, recused)."""
    for cfg in CREW:
        if cfg[0] == JUDGE_MODEL_ID and cfg[0] not in (model_id_a, model_id_b):
            return cfg, False
    for cfg in CREW:
        if cfg[0] not in (model_id_a, model_id_b):
            return cfg, True
    raise RuntimeError('No conflict-free judge available (crew < 3 models?)')


_JUDGE_SYSTEM = (
    "You are an impartial grading judge. You will see one question and two "
    "anonymous answers (A and B). Rules:\n"
    "- Score ONLY correctness, completeness, and clarity.\n"
    "- Do NOT treat answer length, formatting, or confident tone as quality signals.\n"
    "- Do NOT speculate about which system wrote which answer; if you suspect you "
    "recognize an answer's style or authorship, disregard it entirely.\n"
    "- If neither answer is meaningfully better, call a tie.\n"
    "Return STRICT JSON, nothing else:\n"
    '{"winner": "A"|"B"|"tie", "confidence": 1-5, "reason": "<=30 words"}'
)


def build_judge_prompt(question, text_a, text_b):
    return (
        f"QUESTION:\n{question}\n\n"
        f"=== RESPONSE A ===\n{text_a}\n\n"
        f"=== RESPONSE B ===\n{text_b}\n\n"
        "Return the JSON verdict now."
    )


def parse_verdict(raw: str):
    """Tolerant JSON extraction: strips code fences, finds first {...} block."""
    m = re.search(r'\{[^{}]*\}', raw, re.S)
    if not m:
        return None
    try:
        v = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if v.get('winner') not in ('A', 'B', 'tie'):
        return None
    try:
        conf = int(v.get('confidence', 3))
    except (TypeError, ValueError):
        conf = 3
    return {
        'winner': v['winner'],
        'confidence': max(1, min(5, conf)),
        'reason': str(v.get('reason', ''))[:200],
    }


def _judge_http_call(judge_cfg, prompt_text):
    """Single chat call for judging. Returns (raw_text, cost_usd)."""
    model_id, _label, base_url, key_env, p_in, p_out = judge_cfg
    key = get_env_key(key_env)
    if not key:
        raise RuntimeError(f'{key_env} not set (needed for judge {model_id})')
    body = json.dumps({
        'model': model_id,
        'messages': [
            {'role': 'system', 'content': _JUDGE_SYSTEM},
            {'role': 'user', 'content': prompt_text},
        ],
        'temperature': 0.0,
        'max_tokens': 300,
    }).encode()
    last_err = None
    for _ in range(3):
        try:
            url = base_url.rstrip('/') + '/chat/completions'
            req = urllib.request.Request(
                url,
                data=body,
                method='POST',
                headers={
                    'Authorization': f'Bearer {key}',
                    'Content-Type': 'application/json',
                    'User-Agent': 'ask-crew-judge/1.0',
                },
            )
            with urllib.request.urlopen(req, timeout=90) as r:
                data = json.loads(r.read())
            usage = data.get('usage') or {}
            cost = (
                usage.get('prompt_tokens', 0) / 1e6 * p_in
                + usage.get('completion_tokens', 0) / 1e6 * p_out
            )
            content = (data.get('choices') or [{}])[0].get('message', {}).get('content', '')
            return content, cost
        except Exception as e:
            last_err = e
            time.sleep(2)
    raise RuntimeError(f'judge call failed: {last_err}')


def judge_pair(question, resp_a, resp_b, do_swap):
    """Judge one pair. resp_a/resp_b: dicts with model_id, answer_text,
    response_id. Returns match dict or None if unjudgeable."""
    judge_cfg, recused = pick_judge(resp_a['model_id'], resp_b['model_id'])
    total_cost = 0.0

    # Cryptographic coin-flip for seat assignment
    presented_a_is = 'model_a' if secrets.randbelow(2) == 0 else 'model_b'
    shown_a, shown_b = (
        (resp_a, resp_b) if presented_a_is == 'model_a'
        else (resp_b, resp_a)
    )

    def one_pass(ta, tb):
        raw, cost = _judge_http_call(
            judge_cfg, build_judge_prompt(question, ta, tb)
        )
        return raw, parse_verdict(raw), cost

    raw1, v1, c1 = one_pass(shown_a['answer_text'], shown_b['answer_text'])
    total_cost += c1
    swap_consistent = None
    if v1 is None:
        return None  # judge gave garbage; skip pair

    if do_swap:
        raw2, v2, c2 = one_pass(shown_b['answer_text'], shown_a['answer_text'])
        total_cost += c2
        remap = {'A': 'B', 'B': 'A', 'tie': 'tie'}
        if v2 is not None:
            swap_consistent = 1 if remap[v2['winner']] == v1['winner'] else 0
            if not swap_consistent:
                # Position bias detected — force tie
                v1 = {'winner': 'tie', 'confidence': v1['confidence'],
                      'reason': f'position-inconsistent: {v1["reason"]}'}

    # Low-confidence verdicts don't move ratings
    effective = v1 if v1['confidence'] > 1 else {**v1, 'winner': 'tie'}

    # Resolve judge seats back to real models
    winner_model = None
    if effective['winner'] == 'A':
        winner_model = shown_a['model_id']
    elif effective['winner'] == 'B':
        winner_model = shown_b['model_id']

    if winner_model == resp_a['model_id']:
        score_a = 1.0
    elif winner_model == resp_b['model_id']:
        score_a = 0.0
    else:
        score_a = 0.5

    return {
        'match_id': str(uuid.uuid4()),
        'response_id_a': resp_a.get('response_id'),
        'response_id_b': resp_b.get('response_id'),
        'model_a': resp_a['model_id'],
        'model_b': resp_b['model_id'],
        'judge_model': judge_cfg[0],
        'judge_recused': int(recused),
        'presented_a_is': presented_a_is,
        'verdict': effective['winner'],
        'winner_model': winner_model,
        'score_model_a': score_a,
        'confidence': v1['confidence'],
        'swap_consistent': swap_consistent,
        'judge_cost_usd': round(total_cost, 6),
        'judge_reason': effective['reason'],
    }


def _rating_row(conn, model_id, category):
    row = conn.execute(
        'SELECT rating, games FROM ratings WHERE model_id=? AND category=?',
        (model_id, category),
    ).fetchone()
    return (row[0], row[1]) if row else (ELO_START, 0)


def _bump_rating(conn, model_id, category, new_rating, score):
    w = 1 if score == 1.0 else 0
    l = 1 if score == 0.0 else 0
    t = 1 if score == 0.5 else 0
    conn.execute(
        "INSERT INTO ratings (model_id, category, rating, games, wins, "
        "losses, ties, updated_at) "
        "VALUES (?,?,?,1,?,?,?,datetime('now')) "
        "ON CONFLICT(model_id, category) DO UPDATE SET "
        "rating=excluded.rating, games=games+1, "
        "wins=wins+excluded.wins, losses=losses+excluded.losses, "
        "ties=ties+excluded.ties, updated_at=excluded.updated_at",
        (model_id, category, new_rating, w, l, t),
    )


def apply_match_to_ratings(conn, m):
    """Update global '*' and per-category rows for both players."""
    for cat in ('*', m['category']):
        ra, ga = _rating_row(conn, m['model_a'], cat)
        rb, gb = _rating_row(conn, m['model_b'], cat)
        nra, nrb = elo_update(ra, rb, m['score_model_a'], ga, gb)
        _bump_rating(conn, m['model_a'], cat, nra, m['score_model_a'])
        _bump_rating(conn, m['model_b'], cat, nrb, 1.0 - m['score_model_a'])


def recompute_ratings(conn):
    """Full deterministic replay from match history."""
    conn.execute('DELETE FROM ratings')
    rows = conn.execute(
        "SELECT match_id, category, model_a, model_b, score_model_a "
        "FROM matches ORDER BY created_at, match_id"
    ).fetchall()
    for r in rows:
        m = {
            'category': r[0 + 1],  # category
            'model_a': r[0 + 2],
            'model_b': r[0 + 3],
            'score_model_a': r[0 + 4],
        }
        apply_match_to_ratings(conn, m)
    conn.commit()


def run_arena(conn, run_id, question, responses, do_swap):
    """Full round-robin over crew responses. responses: list of dicts with
    response_id, model_id, answer_text, error. Heretic excluded by caller."""
    eligible = [r for r in responses
                if r.get('answer_text') and not r.get('error')]
    if len(eligible) < 2:
        print('Arena: fewer than 2 successful answers — skipped.')
        return []
    category = classify_category(question)
    pairs = [(a, b) for a, b in itertools.combinations(eligible, 2)]
    matches, total_cost = [], 0.0

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
        futs = {ex.submit(judge_pair, question, a, b, do_swap): (a, b)
                for a, b in pairs}
        for fut in concurrent.futures.as_completed(futs):
            try:
                m = fut.result()
            except Exception as e:
                a, b = futs[fut]
                print(f'Arena: judge failed for {a["model_id"]} vs '
                      f'{b["model_id"]}: {e} — pair skipped.')
                continue
            if m:
                m['run_id'] = run_id
                m['category'] = category
                matches.append(m)
                total_cost += m['judge_cost_usd']

    # Deterministic application order so replay == live
    matches.sort(key=lambda m: (m['model_a'], m['model_b']))
    for m in matches:
        conn.execute(
            "INSERT INTO matches (match_id, run_id, category, response_id_a, "
            "response_id_b, model_a, model_b, judge_model, judge_recused, "
            "presented_a_is, verdict, winner_model, score_model_a, "
            "confidence, swap_consistent, judge_cost_usd, judge_reason) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (m['match_id'], m['run_id'], m['category'],
             m['response_id_a'], m['response_id_b'],
             m['model_a'], m['model_b'], m['judge_model'],
             m['judge_recused'], m['presented_a_is'], m['verdict'],
             m['winner_model'], m['score_model_a'], m['confidence'],
             m['swap_consistent'], m['judge_cost_usd'], m['judge_reason']),
        )
        apply_match_to_ratings(conn, m)
    conn.commit()

    print(f'\n{"=" * 70}')
    print(f'Arena [{category}] — {len(matches)} matches judged')
    print('=' * 70)
    labels = {e[0]: e[1] for e in CREW}
    for m in matches:
        res = ('tie' if m['winner_model'] is None
               else f'{labels.get(m["winner_model"], m["winner_model"])} wins')
        rec = ' (recused)' if m['judge_recused'] else ''
        sw = {None: '', 1: ' +swap-ok', 0: ' +swap-bias->tie'}[m['swap_consistent']]
        judge_lbl = labels.get(m['judge_model'], m['judge_model'])
        print(f'  {labels.get(m["model_a"], m["model_a"])[:15]} vs '
              f'{labels.get(m["model_b"], m["model_b"])[:15]}: {res}'
              f' [judge: {judge_lbl[:15]}{rec}, conf {m["confidence"]}{sw}]')
        if m['judge_reason']:
            print(f'    {m["judge_reason"]}')
    print(f'  judge cost: ${total_cost:.4f}')
    return matches


def show_leaderboard(conn, category='*'):
    rows = conn.execute(
        "SELECT model_id, rating, games, wins, losses, ties FROM ratings "
        "WHERE category=? ORDER BY rating DESC",
        (category,),
    ).fetchall()
    tag = 'GLOBAL' if category == '*' else category.upper()
    print(f'\n{"=" * 70}')
    print(f'🏆 ELO LEADERBOARD [{tag}]')
    print('=' * 70)
    if not rows:
        print('  No rated matches yet. Run with --judge first.')
        print()
        return
    print(f'  {"#":<3}{"model":<36}{"rating":>8}{"games":>7}{"W-L-T":>12}')
    print(f'  {"-" * 66}')
    labels = {e[0]: e[1] for e in CREW}
    for i, r in enumerate(rows, 1):
        mid, rating, games, w, l, t = r
        lbl = labels.get(mid, mid)
        prov = ' (prov)' if games < 30 else ''
        print(f'  {i:<3}{lbl:<36}{rating:>8.1f}{games:>7}'
              f'{f"{w}-{l}-{t}":>12}{prov}')

    # Judge accountability footnote
    stats = conn.execute(
        "SELECT judge_model, COUNT(*), "
        "AVG(CASE WHEN swap_consistent=0 THEN 1.0 ELSE 0.0 END) "
        "FROM matches GROUP BY judge_model"
    ).fetchall()
    if stats:
        print(f'\n  {"Judge accountability":<36}{"matches":>8}{"flip-rate":>12}')
        for jm, n, flip in stats:
            jlbl = labels.get(jm, jm)
            fr = f'{flip:.0%}' if flip is not None else 'n/a'
            print(f'  {jlbl:<36}{n:>8}{fr:>12}')
    print()


def audit_judge_bias(conn):
    """Empirical self-preference check using stored verdicts."""
    rows = conn.execute(
        "SELECT judge_model, judge_recused, verdict, "
        "COUNT(*) as cnt FROM matches GROUP BY judge_model, judge_recused, verdict"
    ).fetchall()
    if not rows:
        print('\n  No matches yet — nothing to audit.\n')
        return
    labels = {e[0]: e[1] for e in CREW}
    print(f'\n{"=" * 70}')
    print('🔍 JUDGE BIAS AUDIT')
    print('=' * 70)
    for jm, recused, verdict, cnt in rows:
        jlbl = labels.get(jm, jm)
        tag = ' (recused/substitute)' if recused else ''
        print(f'  {jlbl}{tag}: {verdict} x{cnt}')
    print()


def parse_args(argv: list[str]) -> dict:
    """Parse CLI into a dict of options.

    Flags:
      --models ID,ID      restrict to specific crew models
      --file PATH         inline a file into the prompt (repeatable)
      --history [N]       show recent runs and exit
      --no-claims         skip claim cartography
      --heretic           add the designated-heretic pass (extra Kimi call)
      --no-judge / --judge  blind arena judging is OFF unless --judge
      --judge-swap        double-judge each pair to catch position bias
      --elo [CAT]         print Elo leaderboard and exit
      --elo-rebuild       replay match history to rebuild ratings, then exit
      --audit-bias        print judge bias audit and exit
      --max-bytes N       per-run budget for inlined files (default 300000)
      --force-truncate    over budget? head-sample every file to fit (loud)
    """
    opts = {
        "models": [e[0] for e in CREW],
        "files": [],
        "query": "",
        "history": False,
        "history_limit": 10,
        "no_claims": False,
        "heretic": False,
        "judge": False,       # opt-in — extra calls cost money
        "judge_swap": False,
        "elo": False,
        "elo_category": "*",
        "elo_rebuild": False,
        "audit_bias": False,
        "max_bytes": MAX_RUN_BYTES,
        "force_truncate": False,
    }
    rest: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--models":
            if i + 1 >= len(argv):
                print("ERROR: --models needs a comma-separated list", file=sys.stderr)
                sys.exit(2)
            opts["models"] = [m.strip() for m in argv[i + 1].split(",") if m.strip()]
            i += 2
        elif a == "--file":
            if i + 1 >= len(argv):
                print("ERROR: --file needs a PATH", file=sys.stderr)
                sys.exit(2)
            opts["files"].append(argv[i + 1])
            i += 2
        elif a == "--history":
            opts["history"] = True
            if i + 1 < len(argv) and argv[i + 1].isdigit():
                opts["history_limit"] = int(argv[i + 1])
                i += 2
            else:
                i += 1
        elif a == "--no-claims":
            opts["no_claims"] = True
            i += 1
        elif a == "--heretic":
            opts["heretic"] = True
            i += 1
        elif a == "--no-judge":
            opts["judge"] = False
            i += 1
        elif a == "--judge":
            opts["judge"] = True
            i += 1
        elif a == "--judge-swap":
            opts["judge_swap"] = True
            opts["judge"] = True  # swap implies judge
            i += 1
        elif a == "--elo":
            opts["elo"] = True
            if i + 1 < len(argv) and not argv[i + 1].startswith("-"):
                opts["elo_category"] = argv[i + 1]
                i += 2
            else:
                i += 1
        elif a == "--elo-rebuild":
            opts["elo_rebuild"] = True
            i += 1
        elif a == "--audit-bias":
            opts["audit_bias"] = True
            i += 1
        elif a == "--max-bytes":
            if i + 1 >= len(argv) or not argv[i + 1].isdigit():
                print("ERROR: --max-bytes needs a positive integer N", file=sys.stderr)
                sys.exit(2)
            opts["max_bytes"] = int(argv[i + 1])
            i += 2
        elif a == "--force-truncate":
            opts["force_truncate"] = True
            i += 1
        else:
            rest.append(a)
            i += 1
    opts["query"] = " ".join(rest).strip()
    return opts


def _cost(entry: tuple | None, usage: dict) -> float:
    """Dollar cost of one call from token counts + per-model pricing (per 1M)."""
    in_price = entry[4] if entry and len(entry) >= 6 else 0.0
    out_price = entry[5] if entry and len(entry) >= 6 else 0.0
    return (
        usage["prompt_tokens"] * in_price / 1_000_000
        + usage["completion_tokens"] * out_price / 1_000_000
    )


def main() -> int:
    opts = parse_args(sys.argv[1:])

    # --history: read-only ledger view, then exit
    if opts["history"]:
        return cmd_history(opts["history_limit"])

    # --elo / --elo-rebuild / --audit-bias: read-only ledger views, then exit
    if opts["elo_rebuild"] or opts["elo"] or opts["audit_bias"]:
        try:
            conn = _open_ledger()
        except Exception as e:
            print(f"[ledger] ERROR: cannot open ledger ({e})", file=sys.stderr)
            return 1
        if opts["elo_rebuild"]:
            recompute_ratings(conn)
            show_leaderboard(conn, opts["elo_category"])
        elif opts["audit_bias"]:
            audit_judge_bias(conn)
        else:
            show_leaderboard(conn, opts["elo_category"])
        conn.close()
        return 0

    query = opts["query"]
    files = opts["files"]
    models = opts["models"]

    if not query and not files:
        print(__doc__)
        return 2

    jobs = []
    for mid in models:
        entry = next((e for e in CREW if e[0] == mid), None)
        if entry is None:
            print(
                f"ERROR: unknown model {mid!r}. Allowed --models ids "
                f"(exact match, no aliases):\n"
                + "\n".join(f"  {e[0]}" for e in CREW),
                file=sys.stderr,
            )
            return 2
        base_url, key_env = route(entry)
        key = get_env_key(key_env)
        if not key:
            print(f"ERROR: {key_env} not set (needed for {entry[1]})", file=sys.stderr)
            return 1
        jobs.append((entry, base_url, key))

    prompt = build_prompt(query, files, opts["max_bytes"], opts["force_truncate"])
    summary = query or "(no question — file review only)"
    if files:
        summary += f"  [files: {', '.join(files)}]"

    # Capability routing: if the full pack exceeds a model's context, that
    # model gets structural outlines instead of full file bodies. The
    # prompt layout is identical otherwise — file blocks then question — so
    # the prefix cache still engages where blocks are shared.
    prompt_len = len(prompt.encode("utf-8"))
    prompt_map: dict[str, str] = {}
    for entry, _base_url, _key in jobs:
        mid = entry[0]
        cap = MODEL_CONTEXT_CHARS.get(mid)
        if cap is not None and prompt_len > cap and files:
            outline_blocks = [
                file_outline(p) for p in sorted(files, key=_stable_path_key)
            ]
            q = query or "Please review the file(s) above."
            routed_prompt = "\n\n".join(
                [b for b in outline_blocks if b] + [f"--- question ---\n{q}"]
            )
            print(
                f"⚡ routing: {entry[1]} — full pack {prompt_len:,} bytes "
                f"exceeds its context; sending structural outlines instead "
                f"({len(routed_prompt.encode('utf-8')):,} bytes)",
                file=sys.stderr,
            )
            prompt_map[mid] = routed_prompt
        else:
            prompt_map[mid] = prompt

    # Heretic job: opt-in via --heretic, and only if Kimi K3 is in the run
    heretic_job = (
        next((j for j in jobs if j[0][0] == HERETIC_MODEL_ID), None)
        if opts["heretic"] else None
    )
    n_calls = len(jobs) + (1 if heretic_job else 0)
    heretic_note = " + 🎭 heretic" if heretic_job else ""
    print(f"🤖 Asking the crew ({len(jobs)} models{heretic_note}): {summary}\n")

    t0 = time.monotonic()
    stream_run_id = uuid.uuid4().hex[:12]
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=n_calls)

    # Map future -> (model_id, role) so we can separate council from heretic
    # even when they share the same model_id (Kimi K3 plays both roles).
    future_roles: dict = {}
    for e, base_url, key in jobs:
        f = pool.submit(
            ask, e[0], prompt_map[e[0]], base_url, key, MAIN_SYSTEM_PROMPT,
            "council", stream_run_id,
        )
        future_roles[f] = (e[0], "council")
    heretic_future = None
    if heretic_job:
        entry, base_url, key = heretic_job
        heretic_future = pool.submit(
            ask, entry[0], prompt_map[entry[0]], base_url, key, HERETIC_SYSTEM_PROMPT,
            "heretic", stream_run_id,
        )
        future_roles[heretic_future] = (entry[0], "heretic")

    # Quorum-based return: once QUORUM_COUNT council answers are in (or
    # the deadline hits), return. Stragglers keep streaming to disk; their
    # checkpoints are NOT lost.
    deadline = t0 + QUORUM_DEADLINE
    council_results: list = []
    heretic_result = None
    timed_out_models: list = []
    pending = set(future_roles.keys())
    quorum = min(QUORUM_COUNT, len(jobs))

    while pending:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        finished, pending = concurrent.futures.wait(
            pending,
            timeout=min(remaining, 30),
            return_when=concurrent.futures.FIRST_COMPLETED,
        )
        for fut in finished:
            mid, role = future_roles[fut]
            try:
                result = fut.result()
            except Exception as exc:
                result = (mid, f"ERROR: future failure: {exc}", 0.0, {})
            if role == "heretic":
                heretic_result = result
            else:
                council_results.append(result)
        # Count-based quorum: enough good answers → stop waiting. Errors
        # don't count toward quorum — a 429-then-retry shouldn't count as
        # "answered". The opt-in heretic is never abandoned: if the user
        # paid for it, we wait for its verdict (up to the deadline).
        good = sum(
            1 for _, c, _, _ in council_results if not c.startswith("ERROR:")
        )
        heretic_pending = (
            heretic_future is not None and heretic_future in pending
        )
        if quorum > 0 and good >= quorum and not heretic_pending:
            break

    # Collect timed-out models (still pending when we stopped waiting)
    for fut in pending:
        mid, role = future_roles[fut]
        if role == "council":
            timed_out_models.append(mid)

    # Try to cancel stragglers so they don't block process exit
    pool.shutdown(wait=False, cancel_futures=True)

    wall_seconds = time.monotonic() - t0

    order = {e[0][0]: i for i, e in enumerate(jobs)}
    total_cost = 0.0
    ledger_responses = []
    has_error = False

    for mid, content, dt, usage in sorted(council_results, key=lambda r: order[r[0]]):
        entry = next((e for e in CREW if e[0] == mid), None)
        label = entry[1] if entry else mid
        in_tok = usage["prompt_tokens"]
        out_tok = usage["completion_tokens"]
        cost = _cost(entry, usage)
        total_cost += cost
        if content.startswith("ERROR:"):
            has_error = True
        print(f"── {label} ({mid}) — {dt:.1f}s · {in_tok:,}→{out_tok:,} tok · ${cost:.4f} ──")
        print(content)
        print()

        # Collect for ledger
        ledger_responses.append({
            "model_id": mid,
            "role": "council",
            "text": content,
            "prompt_tokens": in_tok,
            "completion_tokens": out_tok,
            "cost": cost,
            "elapsed_seconds": dt,
            "error": content if content.startswith("ERROR:") else None,
        })

    # Report timed-out models
    for mid in timed_out_models:
        entry = next((e for e in CREW if e[0] == mid), None)
        label = entry[1] if entry else mid
        slug = _model_slug(mid)
        ckpt_path = _checkpoint_root() / stream_run_id / f"council_{slug}.jsonl"
        print(f"── {label} ({mid}) — TIMED OUT (partial checkpoint: {ckpt_path}) ──")
        print()
        has_error = True
        ledger_responses.append({
            "model_id": mid,
            "role": "council",
            "text": "(timed out — partial transcript on disk)",
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cost": 0,
            "elapsed_seconds": QUORUM_DEADLINE,
            "error": "timed out at quorum deadline",
        })

    # Heretic verdict comes LAST
    if heretic_result:
        mid, content, dt, usage = heretic_result
        entry = next((e for e in CREW if e[0] == mid), None)
        in_tok = usage["prompt_tokens"]
        out_tok = usage["completion_tokens"]
        cost = _cost(entry, usage)
        total_cost += cost
        print("═" * 72)
        print(
            f"🎭 HERETIC (Kimi K3) — designated devil's advocate "
            f"— {dt:.1f}s · {in_tok:,}→{out_tok:,} tok · ${cost:.4f}"
        )
        print("═" * 72)
        print(content)
        print()

        ledger_responses.append({
            "model_id": mid,
            "role": "heretic",
            "text": content,
            "prompt_tokens": in_tok,
            "completion_tokens": out_tok,
            "cost": cost,
            "elapsed_seconds": dt,
            "error": None,
        })

    print(f"💰 Total crew cost: ${total_cost:.4f}")

    # Persist to ledger (never crashes the main flow)
    status = "partial" if has_error else "ok"
    run_id = record_run(
        query=query,
        files=files,
        models_asked=[e[0][0] for e, _, _ in jobs],
        responses=ledger_responses,
        wall_seconds=wall_seconds,
        status=status,
    )

    # Claim Cartography: local, deterministic, zero extra model calls.
    # Fires after ledger write so response_ids are available. Silently
    # skips if models didn't follow the CLAIMS/DISSENT format.
    if not opts["no_claims"] and run_id:
        # Enrich results with response_ids from the ledger for claim storage
        try:
            conn = _open_ledger()
            resp_rows = conn.execute(
                "SELECT response_id, model_id, role FROM responses WHERE run_id = ? ORDER BY response_id",
                (run_id,),
            ).fetchall()
            conn.close()
            # Build a lookup: (model_id, role) -> response_id
            rid_map = {}
            for row in resp_rows:
                key = (row["model_id"], row["role"])
                rid_map[key] = row["response_id"]
            # Attach response_ids to ledger_responses for cartography
            for r in ledger_responses:
                r["response_id"] = rid_map.get((r["model_id"], r["role"]))
        except Exception as e:
            print(f"[ledger] WARNING: response_id lookup failed: {e}", file=sys.stderr)

        ok_results = [r for r in ledger_responses if not r.get("error")]
        crew_count = sum(1 for r in ok_results if r.get("role") == "council")
        if len(ok_results) >= 2:
            run_claim_cartography(ok_results, run_id, crew_count)

    # Blind Arena + Elo: opt-in via --judge (extra calls cost money).
    # Uses the same response_ids enriched above for FK integrity.
    if opts["judge"] and run_id:
        # Build arena responses (council only — heretic excluded)
        arena_responses = [
            {
                "response_id": r.get("response_id"),
                "model_id": r["model_id"],
                "answer_text": r["text"],
                "error": r.get("error"),
            }
            for r in ledger_responses
            if r.get("role") == "council"
        ]
        try:
            conn = _open_ledger()
            run_arena(conn, run_id, query, arena_responses, opts["judge_swap"])
            show_leaderboard(conn)
            conn.close()
        except Exception as e:
            print(f"[arena] WARNING: arena failed: {e}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
