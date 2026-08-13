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

Allowed --models ids (exact match; anything else is rejected with this list):
  hf:moonshotai/Kimi-K3    Kimi K3            (Synthetic)
  x-ai/grok-4.6            Grok 4.6           (OpenRouter)
  google/gemini-3.7-flash  Gemini 3.7 Flash   (OpenRouter)
  (default: all of the above)

Pass one or more --file PATH args to inline file contents into the prompt.
Text files are inlined; binary files are noted but not inlined. Files larger
than MAX_FILE_BYTES are truncated with a warning.

DESIGNATED HERETIC MODE (always on):
  Crew models tend to converge — overlapping training data means they can
  all be wrong the same way. To counter this, Kimi K3 (when included in the
  run) makes a SECOND call as the "designated heretic": forced to assume the
  consensus is wrong and argue the strongest objection. Kimi's regular
  crew response is shown as usual; the heretic verdict appears last, after
  all regular responses, clearly marked. If Kimi is not in --models, the
  heretic is skipped silently.

LEDGER:
  Every run is persisted to a SQLite ledger (default: ~/.local/share/crew/
  crew_ledger.db, or $CREW_LEDGER_DB). The ledger stores run metadata,
  per-model responses, and the heretic verdict. Disable with CREW_LEDGER=off.

Reads SYNTHETIC_API_KEY (Kimi K3) and OPENROUTER_API_KEY
(Grok 4.6, Gemini 3.7 Flash) from the env.
"""
import concurrent.futures
import hashlib
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
# response with role='heretic', not special columns. FTS5 virtual tables
# power search (future: Claim Cartography). No speculative columns for
# Elo/claims — those land as migrations when the features are built.
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

-- Full-text search on queries and responses (future: topic search, Claim Cartography)
CREATE VIRTUAL TABLE IF NOT EXISTS runs_fts USING fts5(
    query, content='runs', content_rowid='rowid'
);
CREATE TRIGGER IF NOT EXISTS runs_ai AFTER INSERT ON runs BEGIN
    INSERT INTO runs_fts(rowid, query) VALUES (new.rowid, new.query);
END;
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

TIMEOUT = 600  # 10 min per model — long enough for complex queries
MAX_FILE_BYTES = 100_000
CHARS_PER_TOKEN = 4
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


def _stable_path_key(path: str) -> str:
    """Deterministic sort key for a file path.

    Uses sha256 of the path string — NOT built-in hash(), which is salted by
    PYTHONHASHSEED and changes across processes. A stable ordering of file
    blocks keeps the prompt prefix byte-for-byte identical between runs, so
    provider-side prompt caching (KV-cache reuse) actually engages.
    """
    return hashlib.sha256(path.encode("utf-8")).hexdigest()


def read_file_block(path: str) -> str | None:
    """Read a file and return a prompt-ready block, or None if unusable."""
    p = Path(path)
    if not p.is_file():
        alt = Path("/workspace") / path
        if alt.is_file():
            p = alt
        else:
            return None
    try:
        size = p.stat().st_size
        with p.open("rb") as f:
            raw = f.read(MAX_FILE_BYTES + 1)
    except OSError as e:
        print(f"WARNING: cannot read {path}: {e}", file=sys.stderr)
        return None
    truncated = len(raw) > MAX_FILE_BYTES
    if truncated:
        raw = raw[:MAX_FILE_BYTES]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return f"--- file: {path} ({size} bytes; binary, not inlined) ---\n--- end {path} ---"
    suffix = (
        f"\n... [truncated: kept first {MAX_FILE_BYTES} of {size} bytes]"
        if truncated
        else ""
    )
    return f"--- file: {path} ({size} bytes) ---\n{text}{suffix}\n--- end {path} ---"


def build_prompt(query: str, files: list[str]) -> str:
    """Combine inlined file blocks with the user's question into one prompt.

    File blocks are emitted in deterministic order (sorted by stable sha256
    of the path) so provider KV-cache reuse engages on repeat runs.
    """
    blocks: list[str] = []
    for path in sorted(files, key=_stable_path_key):
        block = read_file_block(path)
        if block is None:
            print(f"WARNING: file not found or not readable: {path}", file=sys.stderr)
            continue
        blocks.append(block)
    if not query and blocks:
        query = "Please review the file(s) above."
    if blocks:
        prefix_chars = sum(len(b) for b in blocks)
        est_tokens = prefix_chars // CHARS_PER_TOKEN
        print(
            f"📎 Files sorted for cache reuse "
            f"({len(blocks)} blocks, ~{est_tokens:,} tokens prefix)"
        )
        blocks.append(f"--- question ---\n{query}")
        return "\n\n".join(blocks)
    return query


def ask(
    model_id: str,
    query: str,
    base_url: str,
    key: str,
    system: str | None = None,
) -> tuple[str, str, float, dict]:
    """Ask one model, return (model_id, content_or_error, elapsed_seconds, usage)."""
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": query})
    body = json.dumps({"model": model_id, "messages": messages}).encode()
    req = urllib.request.Request(
        base_url,
        data=body,
        method="POST",
        headers={k: v.format(key=key) for k, v in HEADERS.items()},
    )
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            data = json.loads(r.read())
        content = (
            (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        )
        usage_raw = data.get("usage") or {}
        usage = {
            "prompt_tokens": usage_raw.get("prompt_tokens", 0),
            "completion_tokens": usage_raw.get("completion_tokens", 0),
        }
        return model_id, content.strip() or "(empty reply)", time.monotonic() - t0, usage
    except Exception as e:
        return model_id, f"ERROR: {type(e).__name__}: {e}", time.monotonic() - t0, {"prompt_tokens": 0, "completion_tokens": 0}


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


def print_blind(responses):
    """Anonymized crew output shown before judging."""
    labels_list = list(string.ascii_uppercase)
    mapping = {}
    for i, r in enumerate(responses):
        seat = labels_list[i]
        mapping[r['model_id']] = seat
        print(f'\n{"=" * 70}')
        print(f'BLIND ARENA — Model {seat} (identity hidden)')
        print('=' * 70)
        print(r.get('answer_text') or f'[error: {r.get("error")}]')
        print()
    return mapping


def print_reveal(mapping):
    print(f'\n{"=" * 70}')
    print('REVEAL')
    print('=' * 70)
    for model_id, seat in sorted(mapping.items(), key=lambda x: x[1]):
        labels = {e[0]: e[1] for e in CREW}
        lbl = labels.get(model_id, model_id)
        print(f'  Model {seat} = {lbl}')
    print()



def parse_args(argv: list[str]) -> dict:
    """Parse CLI into a dict of options.

    Flags:
      --models ID,ID      restrict to specific crew models
      --file PATH         inline a file into the prompt (repeatable)
      --history [N]       show recent runs and exit
      --no-claims         skip claim cartography
      --judge             run blind arena after crew answers (adds judge calls)
      --judge-swap        double-judge each pair to catch position bias
      --elo [CAT]         print Elo leaderboard and exit
      --elo-rebuild       replay match history to rebuild ratings, then exit
      --audit-bias        print judge bias audit and exit
    """
    opts = {
        "models": [e[0] for e in CREW],
        "files": [],
        "query": "",
        "history": False,
        "history_limit": 10,
        "no_claims": False,
        "judge": False,
        "judge_swap": False,
        "elo": False,
        "elo_category": "*",
        "elo_rebuild": False,
        "audit_bias": False,
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

    prompt = build_prompt(query, files)
    summary = query or "(no question — file review only)"
    if files:
        summary += f"  [files: {', '.join(files)}]"

    # Heretic job: only fires if Kimi K3 is in this run's model list
    heretic_job = next(
        (j for j in jobs if j[0][0] == HERETIC_MODEL_ID), None
    )
    n_calls = len(jobs) + (1 if heretic_job else 0)
    heretic_note = " + 🎭 heretic" if heretic_job else ""
    print(f"🤖 Asking the crew ({len(jobs)} models{heretic_note}): {summary}\n")

    t0 = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=n_calls) as pool:
        futures = [pool.submit(ask, e[0], prompt, base_url, key, MAIN_SYSTEM_PROMPT) for e, base_url, key in jobs]
        heretic_future = None
        if heretic_job:
            entry, base_url, key = heretic_job
            heretic_future = pool.submit(
                ask, entry[0], prompt, base_url, key, HERETIC_SYSTEM_PROMPT
            )
        results = [f.result() for f in futures]
        heretic_result = heretic_future.result() if heretic_future else None

    wall_seconds = time.monotonic() - t0

    order = {e[0][0]: i for i, e in enumerate(jobs)}
    total_cost = 0.0
    ledger_responses = []
    has_error = False

    for mid, content, dt, usage in sorted(results, key=lambda r: order[r[0]]):
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

    # Blind Arena + Elo: optional, flag-gated. Fires after all other features.
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
