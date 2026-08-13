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
import json
import os
import re
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

SCHEMA_VERSION = 2

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

SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS claims (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    response_id       INTEGER REFERENCES responses(response_id),
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

CLAIM_SIMILARITY_THRESHOLD = 0.5


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
    """Lowercase, strip punctuation, collapse whitespace — comparison only."""
    return " ".join(text.lower().translate(_PUNCT_TABLE).split())


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


def parse_args(argv: list[str]) -> tuple[list[str], list[str], str, bool, int, bool]:
    """Parse CLI into (models, files, query, history_flag, history_limit, no_claims).

    --history [N]  shows recent runs from the ledger and exits.
    --no-claims    skip claim cartography.
    """
    models: list[str] = [e[0] for e in CREW]
    files: list[str] = []
    rest: list[str] = []
    history = False
    history_limit = 10
    no_claims = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--models":
            if i + 1 >= len(argv):
                print("ERROR: --models needs a comma-separated list", file=sys.stderr)
                sys.exit(2)
            models = [m.strip() for m in argv[i + 1].split(",") if m.strip()]
            i += 2
        elif a == "--file":
            if i + 1 >= len(argv):
                print("ERROR: --file needs a PATH", file=sys.stderr)
                sys.exit(2)
            files.append(argv[i + 1])
            i += 2
        elif a == "--history":
            history = True
            # Optional numeric arg: --history 20
            if i + 1 < len(argv) and argv[i + 1].isdigit():
                history_limit = int(argv[i + 1])
                i += 2
            else:
                i += 1
        elif a == "--no-claims":
            no_claims = True
            i += 1
        else:
            rest.append(a)
            i += 1
    query = " ".join(rest).strip()
    return models, files, query, history, history_limit, no_claims


def _cost(entry: tuple | None, usage: dict) -> float:
    """Dollar cost of one call from token counts + per-model pricing (per 1M)."""
    in_price = entry[4] if entry and len(entry) >= 6 else 0.0
    out_price = entry[5] if entry and len(entry) >= 6 else 0.0
    return (
        usage["prompt_tokens"] * in_price / 1_000_000
        + usage["completion_tokens"] * out_price / 1_000_000
    )


def main() -> int:
    models, files, query, history, history_limit, no_claims = parse_args(sys.argv[1:])

    # --history: read-only ledger view, then exit
    if history:
        return cmd_history(history_limit)

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
    if not no_claims and run_id:
        # Enrich results with response_ids from the ledger for claim storage
        try:
            conn = _open_ledger()
            resp_rows = conn.execute(
                "SELECT response_id, model_id, role FROM responses WHERE run_id = ? ORDER BY id",
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

    return 0


if __name__ == "__main__":
    sys.exit(main())
