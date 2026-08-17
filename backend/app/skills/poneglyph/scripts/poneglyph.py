#!/usr/bin/env python3
"""
gi2.py — GI v2 forensic investigation tool.

Single-file, stdlib-only CLI providing:
  * an append-only, hash-chained NDJSON evidence journal (journal.ndjson)
  * a content-addressed evidence store (evidence/sha256/<2hex>/<62hex>)
  * a disposable SQLite projection (case.db) rebuilt by journal replay
  * span/quote verification for claims citing stored artifacts
  * torn-tail journal detection and repair

Slice 2 (Belief Kernel):
  * subjective-logic belief computation at query time (belief.py)

Slice 3 (Identity):
  * merge/unmerge journal events with union-find canonical identity mapping

Slice 4 (Transforms):
  * jailed subprocess transforms; host fetches and gates all evidence
  * transform_run and via_run provenance events in the journal

Slice 5 (Reputation & Retraction):
  * retract-run: wholesale retraction of a transform run's claims
  * scored retractions feed Beta reputation (reputation.py)
  * belief output discounts sources with bad track records
"""

from __future__ import annotations

import argparse
import contextlib
import difflib
import fcntl
import hashlib
import ipaddress
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from belief import HYPOTHESIS_CONF_CAP, compute_edge_belief  # noqa: E402

# 1.4 (plan-review 2026-08-16): filing-time confidence defaults. Historical
# nulls stay 1.0 by treaty (belief.py never rewrites history); every NEW
# unquantified filing gets an honest default instead of categorical certainty.
CLI_DEFAULT_CONFIDENCE = 0.70    # analyst hand-filed via CLI
TRANSFORM_DEFAULT_CONFIDENCE = 0.60  # LLM-inferred via `run`
TRANSFORM_MAX_CONFIDENCE = 0.80  # LLM claims may never exceed this cap
TRANSFORM_PAYLOAD_MAX_BYTES = 8 * 1024 * 1024  # 3.3: cap artifact_text in the subprocess payload; larger artifacts stream from artifact_path instead
from reputation import discount_from_scores, score_sources  # noqa: E402
from counterfactual import whatif as _whatif, loadbearing as _loadbearing  # noqa: E402
from pivot import (degree_of, expand_rings, load_session, rank_neighbors,  # noqa: E402
                   session_path, touch_trail, visited_ids)

# --------------------------------------------------------------------------
# Constants (on-disk format — changing any of these breaks existing cases)
# --------------------------------------------------------------------------

FORMAT_VERSION = 1

# Projection schema revision (plan-review 1.0, 2026-08-16). The journal is
# frozen forever; the projection is disposable — this constant only governs
# when a stale case.db must be rebuilt. Bump on ANY change to SCHEMA,
# _install_claims_view, or an apply_event column list. History:
#   1 = pre-2026-08-16 layout (no schema_version key, no asserted_ts)
#   2 = +asserted_ts/filed_seq on claims_filed, view exposes them,
#       +idx_claims_asserted, schema_version enforced
#   3 = +artifacts table, idx_id_canon_lookup, idx_supersedes_target
#       (review-3: 2.4 shipped with version left at 2 — the bump discipline
#       bypassed on first use; bumped retroactively and now enforced by the
#       artifacts shape check in _projection_is_current)
CURRENT_SCHEMA_VERSION = 3
GENESIS_PREV_HASH = "sha256:" + hashlib.sha256(b"").hexdigest()
HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
KNOWN_OPS = (
    "case_init",
    "entity",
    "artifact",
    "claim",
    "retract",
    "supersede",
    "merge",
    "unmerge",
    "transform_run",
    "mark",
    "prospect",
    "dig_accept",
    "dig_test",
    "dig_withdraw",
    "dig_close",
)
POLARITIES = ("supports", "refutes")
EVIDENCE_KINDS = ("direct", "inferred", "hypothesis")
# Council r4 fix (honor system): which transforms may emit which evidence
# kinds at the gate. Model transforms (llm/wiki) may never mint 'direct'
# claims — readings of documents are inferred. 'direct' is reserved for
# deterministic extraction transforms and the analyst's hand-filed claims
# (cmd_claim requires --artifact for direct, r4). Unlisted transforms
# default to inferred/hypothesis only.
# 1.5 (plan-review 2026-08-16): fundamentals direct-mint hole closed — a
# transform asserting a 'direct' claim is laundering by definition; the
# open-weights lesson applies to evidence kinds too. 'direct' = analyst
# hand-filed only (plus deterministic re-quote corrections).
TRANSFORM_EVIDENCE_POLICY: dict[str, tuple[str, ...]] = {
    "llm": ("inferred",),
    "wiki": ("inferred",),
    "dig": ("hypothesis",),
    "fundamentals": ("inferred", "hypothesis"),
}
# Provenance of valid_from: how the date was derived. "null" marks a
# claim with NO temporal anchoring (fuzzy source, undated article) —
# such claims are excluded from --as-of fusion rather than stamped
# with assertion time (which would be the bug this enum exists to fix).
TIME_SOURCES = ("explicit", "relative", "fiscal", "inherited", "null")
ISO_TS_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}"
    r"(T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?$"
)


def _valid_iso_ts(v, field: str) -> None:
    if not isinstance(v, str) or not ISO_TS_RE.match(v):
        raise GiError(
            f"{field} must be an ISO-8601 date or UTC datetime string "
            f"like '2024-03-15' or '2024-03-15T14:30:00Z' (got {v!r})")

MARK_KINDS = ("suspicious", "interesting", "cleared", "dead-end", "followup")

QUOTE_SPAN_SLACK = 64           # chars of slack around a quoted span
FETCH_CHUNK = 1 << 16           # 64 KiB
FETCH_MAX_BYTES = 512 * 1024 * 1024
FETCH_TIMEOUT_S = 30
TRANSFORM_TIMEOUT_S = 60

SCHEMA = """
DROP VIEW IF EXISTS claims;
DROP TABLE IF EXISTS claims_filed;
DROP TABLE IF EXISTS entities;
DROP TABLE IF EXISTS claims;
DROP TABLE IF EXISTS aliases;
DROP TABLE IF EXISTS id_canon;
DROP TABLE IF EXISTS transform_runs;
DROP TABLE IF EXISTS retractions;
DROP TABLE IF EXISTS marks;
DROP TABLE IF EXISTS meta;

CREATE TABLE entities (
  id    TEXT PRIMARY KEY,
  kind  TEXT,
  name  TEXT,
  attrs TEXT
);

CREATE TABLE claims (
  claim_id   TEXT PRIMARY KEY,
  subj       TEXT,
  pred       TEXT,
  obj        TEXT,
  polarity   TEXT,
  evidence   TEXT,
  confidence REAL,
  artifact   TEXT,
  span_start INTEGER,
  span_end   INTEGER,
  quote      TEXT,
  via_run    TEXT,
  -- Tritemporal columns (Kimi/Grok/Gemini consensus, 2026-08-14):
  -- valid_from/valid_to = half-open [from, to) world-time interval in
  --   ISO-8601 date form; NULL means "temporally indefinite" (excluded
  --   from as-of fusion, never silently stamped with assert time).
  -- pub_ts = when the CITED ARTIFACT was published (not ingest time);
  --   grounds relative expressions and knowability queries.
  -- time_source = how valid_from was derived (provenance of the date
  --   itself): explicit | relative | fiscal | inherited | null.
  valid_from TEXT,
  valid_to   TEXT,
  pub_ts     TEXT,
  time_source TEXT,
  -- Council plan-review 1.1 (2026-08-16): claim-assert time and journal
  --   seq carried on the row so --as-learned queries need no journal scan.
  asserted_ts TEXT,
  filed_seq   INTEGER
);

CREATE INDEX idx_claims_asserted ON claims(asserted_ts);

CREATE TABLE supersedes (
  seq        INTEGER PRIMARY KEY,
  claim_id   TEXT NOT NULL,       -- the superseding claim (survivor)
  target_id  TEXT NOT NULL,       -- the claim being retracted/corrected
  kind       TEXT NOT NULL,       -- retracts | corrects | updates
  reason     TEXT,
  via_run    TEXT
);

CREATE TABLE aliases (
  absorbed_id TEXT PRIMARY KEY,
  survivor_id TEXT NOT NULL
);

CREATE TABLE id_canon (
  entity_id TEXT PRIMARY KEY,
  canon_id  TEXT NOT NULL
);

CREATE TABLE transform_runs (
  run_id        TEXT PRIMARY KEY,
  transform     TEXT NOT NULL,
  uri           TEXT NOT NULL,
  artifact_hash TEXT NOT NULL,
  accepted      INTEGER NOT NULL,
  rejected      INTEGER NOT NULL,
  args          TEXT
);

CREATE TABLE retractions (
  seq      INTEGER PRIMARY KEY,
  claim_id TEXT NOT NULL,
  via_run  TEXT,
  scored   INTEGER NOT NULL DEFAULT 0,
  reason   TEXT
);

CREATE TABLE marks (
  entity_id TEXT NOT NULL,
  mark      TEXT NOT NULL,
  reason    TEXT,
  seq       INTEGER NOT NULL,
  PRIMARY KEY (entity_id, mark)
);

-- Slice 8 (dig): the exploratory pivot. Prospect packs are journaled as
-- events; status is DERIVED at query time (never mutated). See DIG.md.
CREATE TABLE prospects (
  prospect_id    TEXT PRIMARY KEY,
  run_id         TEXT NOT NULL,
  subj           TEXT NOT NULL,
  pred           TEXT NOT NULL,
  obj            TEXT NOT NULL,
  thesis         TEXT NOT NULL,
  mechanism      TEXT,
  anchors        TEXT NOT NULL,   -- JSON: cited active claim ids
  kill_criterion TEXT NOT NULL,   -- JSON: {observation, polarity, source_class}
  fetch_targets  TEXT,            -- JSON: [str]
  novelty_against TEXT,           -- JSON: [claim ids sharing an anchor]
  seq            INTEGER NOT NULL,
  ts             TEXT NOT NULL
);

CREATE TABLE dig_accepts (
  prospect_id TEXT PRIMARY KEY,
  claim_id    TEXT NOT NULL,      -- the evidence=hypothesis claim filed
  reason      TEXT,
  seq         INTEGER NOT NULL
);

CREATE TABLE dig_tests (
  seq         INTEGER PRIMARY KEY,
  prospect_id TEXT NOT NULL,
  plan_hash   TEXT NOT NULL       -- CAS artifact: the kill-first fetch plan
);

CREATE TABLE dig_withdraws (
  prospect_id TEXT PRIMARY KEY,
  reason      TEXT NOT NULL,
  seq         INTEGER NOT NULL
);

-- Slice 8b (closure): verdicts journal the loop's end. corroborated frees
-- quota WITHOUT touching the claim (the hypothesis earned its keep);
-- killed retracts it (the kill criterion was observed). expired retires
-- neglect without prejudice. Derived at query time — never mutated.
CREATE TABLE dig_closes (
  prospect_id TEXT PRIMARY KEY,
  verdict    TEXT NOT NULL CHECK (verdict IN ('corroborated','killed','expired')),
  claim_id   TEXT,
  reason     TEXT,
  evidence   TEXT,
  seq        INTEGER NOT NULL
);

CREATE TABLE artifacts (
  hash TEXT PRIMARY KEY,
  uri TEXT,
  size INTEGER,
  content_type TEXT,
  derived_from TEXT,
  extractor TEXT,
  seq INTEGER
);

CREATE TABLE meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);

CREATE INDEX idx_claims_edge ON claims(subj, pred, obj);
CREATE INDEX idx_claims_obj  ON claims(obj);
CREATE INDEX idx_claims_valid ON claims(valid_from, valid_to);
CREATE INDEX idx_id_canon_lookup ON id_canon(entity_id, canon_id);
CREATE INDEX idx_supersedes_target ON supersedes(target_id);
"""


class GiError(Exception):
    """User-facing failure; printed without a traceback."""


def die(msg: str, code: int = 1) -> "SystemExit":
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(code)


# --------------------------------------------------------------------------
# Identity: union-find over merge events (Slice 3)
# --------------------------------------------------------------------------

class AliasMap:
    """Union-find over identity merges."""

    def __init__(self, parent: dict[str, str] | None = None):
        self.parent: dict[str, str] = dict(parent or {})

    def find(self, x: str) -> str:
        seen: set[str] = set()
        while x in self.parent:
            if x in seen:
                raise GiError(f"identity cycle detected involving {x!r}; the journal is incoherent")
            seen.add(x)
            x = self.parent[x]
        return x

    def is_absorbed(self, x: str) -> bool:
        return x in self.parent

    def would_cycle(self, absorbed: str, survivor: str) -> bool:
        x = survivor
        seen: set[str] = set()
        while True:
            if x == absorbed:
                return True
            if x not in self.parent or x in seen:
                return False
            seen.add(x)
            x = self.parent[x]

    def merge(self, absorbed: str, survivor: str) -> None:
        self.parent[absorbed] = survivor

    def unmerge(self, entity_id: str) -> bool:
        return self.parent.pop(entity_id, None) is not None


def _load_parent_map(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        r["absorbed_id"]: r["survivor_id"]
        for r in conn.execute("SELECT absorbed_id, survivor_id FROM aliases")
    }


def _resolve_id(conn: sqlite3.Connection, entity_id: str) -> str:
    row = conn.execute(
        "SELECT canon_id FROM id_canon WHERE entity_id = ?", (entity_id,)
    ).fetchone()
    return row["canon_id"] if row else entity_id


def _persist_aliases(cur: sqlite3.Cursor, aliases: AliasMap) -> None:
    cur.execute("DELETE FROM aliases")
    cur.execute("DELETE FROM id_canon")
    for absorbed, survivor in aliases.parent.items():
        cur.execute(
            "INSERT INTO aliases(absorbed_id, survivor_id) VALUES (?, ?)",
            (absorbed, survivor),
        )
    ids: set[str] = set()
    for (eid,) in cur.execute("SELECT id FROM entities"):
        ids.add(eid)
    ids.update(aliases.parent.keys())
    ids.update(aliases.parent.values())
    for subj, obj in cur.execute("SELECT subj, obj FROM claims"):
        if subj:
            ids.add(subj)
        if obj:
            ids.add(obj)
    for eid in ids:
        cur.execute(
            "INSERT INTO id_canon(entity_id, canon_id) VALUES (?, ?)",
            (eid, aliases.find(eid)),
        )


def _install_claims_view(cur: sqlite3.Cursor) -> None:
    cur.execute("ALTER TABLE claims RENAME TO claims_filed")
    cur.execute(
        """
        CREATE VIEW claims AS
        SELECT
          cf.claim_id,
          COALESCE(cs.canon_id, cf.subj) AS subj,
          cf.pred,
          COALESCE(co.canon_id, cf.obj)  AS obj,
          cf.polarity,
          cf.evidence,
          cf.confidence,
          cf.artifact,
          cf.span_start,
          cf.span_end,
          cf.quote,
          cf.via_run,
          cf.valid_from,
          cf.valid_to,
          cf.pub_ts,
          cf.time_source,
          cf.asserted_ts,
          cf.filed_seq,
          cf.subj AS filed_subj,
          cf.obj  AS filed_obj,
          CASE WHEN sup.target_id IS NOT NULL THEN 1 ELSE 0 END AS superseded
        FROM claims_filed cf
        LEFT JOIN id_canon cs ON cs.entity_id = cf.subj
        LEFT JOIN id_canon co ON co.entity_id = cf.obj
        LEFT JOIN (SELECT DISTINCT target_id FROM supersedes) sup
               ON sup.target_id = cf.claim_id
        """
    )


def _projection_is_current(case: CasePaths) -> bool:
    if not case.db.exists():
        return False
    conn = sqlite3.connect(str(case.db))
    try:
        rows = {
            r[0]: r[1]
            for r in conn.execute(
                "SELECT name, type FROM sqlite_master WHERE name IN "
                "('claims', 'claims_filed', 'aliases', 'id_canon', 'transform_runs', 'retractions', 'marks', 'supersedes', 'prospects', 'dig_accepts', 'dig_tests', 'dig_withdraws', 'dig_closes', 'artifacts')"
            )
        }
        if not (
            rows.get("claims") == "view"
            and all(rows.get(t) == "table" for t in (
                "claims_filed", "aliases", "id_canon", "transform_runs",
                "retractions", "marks", "supersedes", "prospects",
                "dig_accepts", "dig_tests", "dig_withdraws", "dig_closes",
                "artifacts"))
        ):
            return False
        # Council plan-review 1.0 (2026-08-16): schema_version guards against
        # stale pre-upgrade dbs that pass the shape check (table names only)
        # but lack new columns — once the watermark matches they would be
        # served forever. Any SCHEMA/view change bumps CURRENT_SCHEMA_VERSION.
        try:
            sv = conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
        except sqlite3.Error:
            return False
        if sv is None or int(sv[0]) != CURRENT_SCHEMA_VERSION:
            return False
        # Council r4 (stale projection): schema shape alone can serve a stale
        # graph after a crash mid-transact — the db is current only if its
        # journal watermark matches the journal's last seq.
        try:
            wm = conn.execute(
                "SELECT value FROM meta WHERE key = 'journal_seq'").fetchone()
        except sqlite3.Error:
            return False
        if wm is None:
            return False
        last = last_event(case)
        return last is not None and int(wm[0]) == int(last["seq"])
    except sqlite3.Error:
        return False
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Canonical JSON / hashing
# --------------------------------------------------------------------------

def canonical_json(obj) -> bytes:
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def event_hash(event: dict) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(event)).hexdigest()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# --------------------------------------------------------------------------
# Case layout
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class CasePaths:
    root: Path

    @property
    def journal(self) -> Path:
        return self.root / "journal.ndjson"

    @property
    def db(self) -> Path:
        return self.root / "case.db"

    @property
    def evidence(self) -> Path:
        return self.root / "evidence"

    @property
    def lockfile(self) -> Path:
        return self.root / ".gi2.lock"

    def object_path(self, hash_str: str) -> Path:
        if not HASH_RE.match(hash_str):
            raise GiError(f"malformed hash {hash_str!r}; expected 'sha256:<64 lowercase hex>'")
        algo, _, hexd = hash_str.partition(":")
        return self.evidence / algo / hexd[:2] / hexd[2:]


def resolve_case(args) -> Path:
    raw = getattr(args, "case", None) or os.environ.get("GI_CASE") or "./case"
    return Path(raw).expanduser().resolve()


def require_case(root: Path) -> CasePaths:
    case = CasePaths(root)
    if not case.journal.is_file():
        die(f"no journal at {case.journal} — run 'init' first or set --case / GI_CASE")
    return case


# --------------------------------------------------------------------------
# Journal: locking, tail read, append, iterate
# --------------------------------------------------------------------------

# Reentrancy guard: flock is per-open-file-description, so a LOCK_SH
# acquisition on a NEW fd while THIS process holds LOCK_EX on another fd
# self-deadlocks (the 2026-08-16 write-hang bug: transact -> last_event ->
# shared_lock, blocked on itself). Review-3: a boolean flag fixed shared-
# under-exclusive but left exclusive-under-exclusive deadlocked (any future
# composition like transact-inside-transact). A depth counter used by BOTH
# lock managers makes both directions reentrant. Single-threaded CLI.
_EXCLUSIVE_DEPTH = 0


@contextlib.contextmanager
def exclusive_lock(case: CasePaths):
    global _EXCLUSIVE_DEPTH
    if _EXCLUSIVE_DEPTH > 0:
        # Reentrant: this process already holds LOCK_EX on an outer fd.
        _EXCLUSIVE_DEPTH += 1
        try:
            yield
        finally:
            _EXCLUSIVE_DEPTH -= 1
        return
    case.lockfile.parent.mkdir(parents=True, exist_ok=True)
    if not case.lockfile.exists() or case.lockfile.stat().st_size == 0:
        with open(case.lockfile, "a+b") as fh:
            fh.write(b"\x00")
            fh.flush()
            os.fsync(fh.fileno())
    with open(case.lockfile, "a+b") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        _EXCLUSIVE_DEPTH = 1
        try:
            yield
        finally:
            _EXCLUSIVE_DEPTH = 0
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def shared_lock(case: CasePaths):
    """Phase 2.2: readers take LOCK_SH on the same lockfile writers hold
    LOCK_EX — locking the journal itself would exclude nothing (Kimi).
    Reentrant no-op when this process already holds LOCK_EX (the writer's
    rebuild path reads through last_event/verify without re-acquiring)."""
    if _EXCLUSIVE_DEPTH > 0:
        yield
        return
    case.lockfile.parent.mkdir(parents=True, exist_ok=True)
    if not case.lockfile.exists() or case.lockfile.stat().st_size == 0:
        with open(case.lockfile, "a+b") as fh:
            fh.write(b"\x00")
            fh.flush()
            os.fsync(fh.fileno())
    with open(case.lockfile, "a+b") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


_TAIL_READ_CAP = 1 << 20  # 1MB loop-expansion cap (2.1: quotes can exceed 4KB)


def last_event(case: CasePaths) -> dict | None:
    """Phase 2.1: bounded backward tail-seek with loop expansion, instead of
    reading the whole journal to parse one line. Semantics preserved: None on
    empty/missing journal, GiError on invalid final-line JSON."""
    if not case.journal.exists():
        return None
    with shared_lock(case):
        # Review-3: stat INSIDE the lock. Stat-then-lock raced concurrent
        # writers — the window could parse a mid-write partial line as a
        # "torn tail" on a perfectly healthy journal (Grok).
        size = case.journal.stat().st_size
        if size == 0:
            return None
        window = 4096
        with case.journal.open("rb") as fh:
            while True:
                window = min(window * 2, _TAIL_READ_CAP)
                fh.seek(max(0, size - window))
                tail = fh.read(window)
                # Lines are \n-terminated (append writes line + "\\n"), so the
                # last line's content sits BEFORE the final newline. Strip
                # trailing newlines, then the last chunk is the last line.
                stripped = tail.rstrip(b"\n")
                if not stripped:
                    # No line content in window (blank journal / only newlines,
                    # or window landed inside a run of newlines): expand bounded.
                    if window >= _TAIL_READ_CAP and window >= size:
                        return None
                    if window >= _TAIL_READ_CAP:
                        raise GiError("journal tail is corrupt (no final line within 1MB)")
                    continue
                if b"\n" not in stripped and size > window:
                    if window >= _TAIL_READ_CAP:
                        # Last line exceeds the 1MB cap — terminal fallback:
                        # read the entire file (bounded by size itself).
                        fh.seek(0)
                        final_line = fh.read().rstrip(b"\n").rsplit(b"\n", 1)[-1]
                        try:
                            ev = json.loads(final_line.decode("utf-8"))
                        except (json.JSONDecodeError, UnicodeDecodeError) as e:
                            raise GiError(f"journal tail is corrupt (invalid JSON): {e}")
                        if not isinstance(ev, dict) or "seq" not in ev:
                            raise GiError("journal tail is corrupt (not an event object)")
                        return ev
                    continue  # last line is longer than the window: expand
                final_line = stripped.rsplit(b"\n", 1)[-1]
                try:
                    ev = json.loads(final_line.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError) as e:
                    raise GiError(f"journal tail is corrupt (invalid JSON): {e}")
                if not isinstance(ev, dict) or "seq" not in ev:
                    raise GiError("journal tail is corrupt (not an event object)")
                return ev


def iter_journal_lines(case: CasePaths):
    if not case.journal.exists():
        return
    with case.journal.open("r", encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, 1):
            yield lineno, raw.rstrip("\n")


def _append_event_locked(case: CasePaths, op: str, **fields) -> dict:
    prev = last_event(case)
    if prev is None:
        if op != "case_init":
            raise GiError("journal not initialized — first event must be case_init")
        seq, prev_hash = 1, GENESIS_PREV_HASH
    else:
        seq, prev_hash = int(prev["seq"]) + 1, event_hash(prev)
    ev = {"seq": seq, "ts": utc_now(), "prev_hash": prev_hash, "op": op, **fields}
    with case.journal.open("ab") as fh:
        fh.write(canonical_json(ev) + b"\n")
        fh.flush()
        os.fsync(fh.fileno())
    return ev


def transact(case: CasePaths, op: str, **fields) -> dict:
    with exclusive_lock(case):
        ev = _append_event_locked(case, op, **fields)
        full_rebuild(case)
        return ev


def transact_batch(case: CasePaths, events: list[tuple[str, dict]]) -> list[dict]:
    """Append many events atomically under one lock, then rebuild the projection
    once. Semantically identical to calling transact() per event, but O(n) instead
    of O(n^2): the per-event full_rebuild is what made multi-claim runs quadratic."""
    if not events:
        return []
    with exclusive_lock(case):
        out: list[dict] = []
        for op, fields in events:
            out.append(_append_event_locked(case, op, **fields))
        full_rebuild(case)
    return out


# --------------------------------------------------------------------------
# Chain verification (with torn-tail detection)
# --------------------------------------------------------------------------

def verify_chain(case: CasePaths) -> tuple[list[str], int, str]:
    errors: list[str] = []
    expected_prev = GENESIS_PREV_HASH
    expected_seq = 1
    last_hash = ""

    # Review-3: read under the shared lock so a concurrent writer's
    # mid-append partial line isn't misread as a torn tail (Grok).
    with shared_lock(case):
        raw_bytes = case.journal.read_bytes() if case.journal.exists() else b""
    ends_with_newline = raw_bytes.endswith(b"\n") if raw_bytes else True
    lines = raw_bytes.decode("utf-8", errors="replace").splitlines()
    n_lines = len(lines)

    for i, line in enumerate(lines):
        lineno = i + 1
        is_last = (i == n_lines - 1)

        if not line.strip():
            errors.append(f"line {lineno}: unexpected blank line; stopping verification")
            break
        try:
            ev = json.loads(line)
        except json.JSONDecodeError as e:
            if is_last and not ends_with_newline:
                errors.append(
                    f"line {lineno}: TORN TAIL — the journal was likely interrupted "
                    f"mid-write. The last line is incomplete JSON. "
                    f"Run 'rebuild --repair' to truncate to the last complete event."
                )
            else:
                errors.append(f"line {lineno}: invalid JSON ({e}); stopping verification")
            break
        if not isinstance(ev, dict):
            errors.append(f"line {lineno}: event is not a JSON object; stopping")
            break
        missing = [k for k in ("seq", "ts", "prev_hash", "op") if k not in ev]
        if missing:
            errors.append(f"line {lineno}: missing required fields {missing}; stopping")
            break
        ev_seq = ev.get("seq")
        if not isinstance(ev_seq, int) or isinstance(ev_seq, bool) or ev_seq != expected_seq:
            errors.append(f"line {lineno}: seq is {ev_seq!r}, expected {expected_seq}; stopping")
            break
        ts = ev.get("ts")
        if not isinstance(ts, str) or not ts.endswith("Z"):
            errors.append(f"line {lineno}: ts {ts!r} is not ISO-8601 UTC ('...Z'); stopping")
            break
        if expected_seq == 1 and ev.get("op") != "case_init":
            errors.append("line 1: first event must be case_init; stopping")
            break
        if expected_seq == 1 and ev.get("prev_hash") != GENESIS_PREV_HASH:
            errors.append("line 1: genesis prev_hash must be sha256 of the empty string; stopping")
            break
        if ev.get("prev_hash") != expected_prev:
            errors.append(
                f"line {lineno} (seq {ev_seq}): prev_hash does not match the derived "
                f"hash of the previous event; chain is broken; stopping")
            break
        last_hash = event_hash(ev)
        expected_prev = last_hash
        expected_seq += 1
    return errors, expected_seq - 1, last_hash


# --------------------------------------------------------------------------
# Content-addressed store
# --------------------------------------------------------------------------

def read_bytes(case: CasePaths, hash_str: str) -> bytes:
    p = case.object_path(hash_str)
    try:
        data = p.read_bytes()
    except FileNotFoundError:
        raise GiError(f"evidence object {hash_str} not found in CAS (expected at {p})")
    if sha256_hex(data) != hash_str.split(":", 1)[1]:
        raise GiError(f"evidence object {hash_str} is CORRUPT: content re-hash mismatch")
    return data


def store_file(case: CasePaths, path: Path) -> tuple[str, int]:
    src = Path(path)
    if not src.is_file():
        raise GiError(f"not a regular file: {src}")
    case.evidence.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha256()
    size = 0
    fd, tmpname = tempfile.mkstemp(prefix=".ingest-", dir=str(case.evidence))
    try:
        with src.open("rb") as inf, os.fdopen(fd, "wb") as out:
            while chunk := inf.read(1 << 20):
                h.update(chunk)
                size += len(chunk)
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        hs = f"sha256:{h.hexdigest()}"
        dest = case.object_path(hs)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            os.unlink(tmpname)
        else:
            os.replace(tmpname, dest)
        return hs, size
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmpname)
        raise


# --------------------------------------------------------------------------
# Phase 4: HTML companions (html-visible-v1)
# --------------------------------------------------------------------------

def store_blob_bytes(case: CasePaths, data: bytes) -> tuple[str, int]:
    """Store an in-memory blob into the CAS (content-addressed, atomic)."""
    case.evidence.mkdir(parents=True, exist_ok=True)
    hs = "sha256:" + sha256_hex(data)
    dest = case.object_path(hs)
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmpname = tempfile.mkstemp(prefix=".companion-", dir=str(case.evidence))
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(data)
                out.flush()
                os.fsync(out.fileno())
            if dest.exists():
                os.unlink(tmpname)
            else:
                os.replace(tmpname, dest)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmpname)
            raise
    return hs, len(data)


def maybe_companion(case: CasePaths, hash_str: str, uri: str, content_type: str | None = None) -> str | None:
    """Phase 4.2: if the artifact is HTML, extract visible text and journal
    it as a DERIVED companion artifact. Returns the companion hash or None.

    Derived-event shape (uri untouched — Grok's treaty override):
      op=artifact, hash=<companion sha256>, uri=<source uri> (same as parent,
      so provenance reads naturally), derived_from=<parent hash>,
      extractor="html-visible-v1", extractor_code_hash=<module hash>
    Idempotent: re-fetching the same page journals no duplicate companion
    event (hash is content-addressed; we check the artifacts table first).
    """
    import poneglyph_extract

    data = read_bytes(case, hash_str)
    if not poneglyph_extract.is_html(data, content_type):
        return None
    text = poneglyph_extract.extract_visible_text(data.decode("utf-8", errors="replace"))
    if not text.strip():
        return None
    chs, csize = store_blob_bytes(case, text.encode("utf-8"))

    # idempotence: skip journaling if this companion is already recorded
    conn = open_projection(case)
    try:
        row = conn.execute(
            "SELECT 1 FROM artifacts WHERE hash = ?", (chs,)
        ).fetchone()
    finally:
        conn.close()
    if row:
        return chs

    transact(case, "artifact", hash=chs, uri=uri, size=csize,
             derived_from=hash_str, extractor=poneglyph_extract.EXTRACTOR_VERSION,
             extractor_code_hash=poneglyph_extract.extractor_code_hash())
    return chs


# --------------------------------------------------------------------------
# Projection (case.db): event validation, apply, rebuild
# --------------------------------------------------------------------------

def upsert_meta(cur: sqlite3.Cursor, key: str, value: str) -> None:
    cur.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def _require_str(ev: dict, key: str) -> str:
    v = ev.get(key)
    if not isinstance(v, str) or not v:
        raise GiError(f"field {key!r} must be a non-empty string (got {v!r})")
    return v


def _validate_claim_fields(ev: dict) -> None:
    for k in ("claim_id", "subj", "pred", "obj"):
        _require_str(ev, k)
    if ev.get("polarity", "supports") not in POLARITIES:
        raise GiError(f"polarity must be one of {POLARITIES}")
    if ev.get("evidence", "direct") not in EVIDENCE_KINDS:
        raise GiError(f"evidence must be one of {EVIDENCE_KINDS}")
    conf = ev.get("confidence")
    if conf is not None:
        if not isinstance(conf, (int, float)) or isinstance(conf, bool) \
                or not (0.0 <= float(conf) <= 1.0):
            raise GiError(f"confidence must be a number in [0.0, 1.0] (got {conf!r})")
    art = ev.get("artifact")
    if art is not None:
        if not isinstance(art, str) or not HASH_RE.match(art):
            raise GiError(f"artifact must be 'sha256:<64hex>' or null (got {art!r})")
        ss, se = ev.get("span_start"), ev.get("span_end")
        if not (isinstance(ss, int) and isinstance(se, int)
                and not isinstance(ss, bool) and not isinstance(se, bool)) \
                or ss < 0 or se <= ss:
            raise GiError(f"claims citing an artifact need integer spans with 0 <= start < end (got {ss!r}, {se!r})")
        q = ev.get("quote")
        if not isinstance(q, str) or not q.strip():
            raise GiError("claims citing an artifact must carry a non-empty quote")
    via_run = ev.get("via_run")
    if via_run is not None and not isinstance(via_run, str):
        raise GiError(f"via_run must be string or null (got {via_run!r})")
    # --- tritemporal fields -------------------------------------------
    vf, vt, pub = ev.get("valid_from"), ev.get("valid_to"), ev.get("pub_ts")
    src = ev.get("time_source")
    if vf is not None:
        _valid_iso_ts(vf, "valid_from")
    if vt is not None:
        _valid_iso_ts(vt, "valid_to")
    if pub is not None:
        _valid_iso_ts(pub, "pub_ts")
    if vf is not None and vt is not None and vt <= vf:
        raise GiError(
            f"valid_to must be strictly after valid_from in a half-open "
            f"[from, to) interval (got [{vf!r}, {vt!r}))")
    if src is not None and src not in TIME_SOURCES:
        raise GiError(f"time_source must be one of {TIME_SOURCES} (got {src!r})")
    if vf is None and src not in (None, "null"):
        raise GiError(
            f"time_source={src!r} requires valid_from (an undated claim is "
            f"time_source 'null' or absent)")
    if vf is not None and (src is None or src == "null"):
        raise GiError(f"valid_from requires a time_source other than 'null'")


def _validate_merge_fields(ev: dict) -> None:
    absorbed = _require_str(ev, "absorbed")
    survivor = _require_str(ev, "survivor")
    _require_str(ev, "reason")
    if absorbed == survivor:
        raise GiError("self-merge is forbidden")
    art = ev.get("artifact")
    if art is not None:
        if not isinstance(art, str) or not HASH_RE.match(art):
            raise GiError(f"artifact must be 'sha256:<64hex>' or null (got {art!r})")
        ss, se = ev.get("span_start"), ev.get("span_end")
        if not (isinstance(ss, int) and isinstance(se, int)
                and not isinstance(ss, bool) and not isinstance(se, bool)) \
                or ss < 0 or se <= ss:
            raise GiError(
                f"merge citing an artifact needs integer spans with 0 <= start < end "
                f"(got {ss!r}, {se!r})")
        q = ev.get("quote")
        if not isinstance(q, str) or not q.strip():
            raise GiError("merge citing an artifact must carry a non-empty quote")


def _validate_unmerge_fields(ev: dict) -> None:
    _require_str(ev, "entity_id")
    _require_str(ev, "reason")


def _validate_transform_run_fields(ev: dict) -> None:
    _require_str(ev, "run_id")
    _require_str(ev, "transform")
    _require_str(ev, "uri")
    art = _require_str(ev, "artifact_hash")
    if not HASH_RE.match(art):
        raise GiError(f"artifact_hash must be 'sha256:<64hex>' (got {art!r})")
    acc = ev.get("accepted")
    rej = ev.get("rejected")
    if not isinstance(acc, int) or isinstance(acc, bool) or acc < 0:
        raise GiError(f"accepted must be a non-negative integer (got {acc!r})")
    if not isinstance(rej, int) or isinstance(rej, bool) or rej < 0:
        raise GiError(f"rejected must be a non-negative integer (got {rej!r})")


def _validate_prospect_fields(ev: dict) -> None:
    for k in ("prospect_id", "run_id", "subj", "pred", "obj", "thesis"):
        _require_str(ev, k)
    kc = ev.get("kill_criterion")
    if not isinstance(kc, dict) or not isinstance(kc.get("observation"), str) \
            or not kc["observation"].strip():
        raise GiError("kill_criterion must be an object with a non-empty 'observation'")
    if kc.get("polarity") not in ("supports", "refutes"):
        raise GiError("kill_criterion.polarity must be supports|refutes")
    if not isinstance(kc.get("source_class"), str) or not kc["source_class"].strip():
        raise GiError("kill_criterion.source_class must be a non-empty string")
    anchors = ev.get("anchors")
    if not isinstance(anchors, list) or not anchors \
            or not all(isinstance(a, str) and a for a in anchors):
        raise GiError("anchors must be a non-empty list of claim ids")
    if ev.get("novelty_against") is not None \
            and not isinstance(ev["novelty_against"], list):
        raise GiError("novelty_against must be a list of claim ids when present")
    if ev.get("fetch_targets") is not None \
            and not isinstance(ev["fetch_targets"], list):
        raise GiError("fetch_targets must be a list of strings when present")


def apply_event(cur: sqlite3.Cursor, ev: dict, aliases: "AliasMap | None" = None, case: "CasePaths | None" = None) -> None:
    op = ev.get("op")

    if op == "case_init":
        fmt = ev.get("format")
        if fmt != FORMAT_VERSION:
            raise GiError(f"unsupported journal format {fmt!r}; this build understands {FORMAT_VERSION}")
        upsert_meta(cur, "format_version", str(fmt))
        upsert_meta(cur, "created_at", ev["ts"])

    elif op == "entity":
        eid = _require_str(ev, "id")
        name = _require_str(ev, "name")
        kind = ev.get("kind") or "entity"
        attrs = ev.get("attrs")
        if attrs is not None and not isinstance(attrs, dict):
            raise GiError(f"entity attrs must be an object (got {type(attrs).__name__})")
        cur.execute(
            "INSERT INTO entities(id, kind, name, attrs) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET kind=excluded.kind, name=excluded.name, attrs=excluded.attrs",
            (eid, kind, name, canonical_json(attrs or {}).decode("utf-8")),
        )

    elif op == "artifact":
        h = _require_str(ev, "hash")
        if not HASH_RE.match(h):
            raise GiError(f"artifact event with malformed hash {h!r}")
        # Phase 2.4: artifacts are now projected (previously validated then
        # discarded). URI/content-type/derived_from/extractor recorded when
        # present; size measured from the CAS store at rebuild time.
        size = None
        if case is not None:
            blob = case.object_path(h)
            if blob.is_file():
                size = blob.stat().st_size
        cur.execute(
            "INSERT INTO artifacts(hash, uri, size, content_type, derived_from, extractor, seq) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(hash) DO UPDATE SET uri=excluded.uri, size=excluded.size, "
            "content_type=excluded.content_type, derived_from=excluded.derived_from, "
            "extractor=excluded.extractor, seq=excluded.seq",
            (h, ev.get("uri"), size, ev.get("content_type"),
             ev.get("derived_from"), ev.get("extractor"), int(ev.get("seq", 0) or 0)),
        )

    elif op == "claim":
        _validate_claim_fields(ev)
        row = cur.execute(
            "SELECT claim_id FROM claims WHERE claim_id = ?", (ev["claim_id"],)
        ).fetchone()
        if row is not None:
            # 1.2 (plan-review 2026-08-16): INSERT OR IGNORE silently dropped a
            # second event with the same claim_id — journal/projection
            # divergence with zero error. Identical payload = idempotent replay
            # (kept); differing payload = raise, with an emergency hatch for
            # legacy journals whose conflicts predate this rule.
            existing_raw = cur.execute(
                "SELECT subj, pred, obj, polarity, evidence, artifact, span_start, "
                "span_end, quote, valid_from, valid_to, pub_ts, time_source "
                "FROM claims WHERE claim_id = ?", (ev["claim_id"],)
            ).fetchone()
            # via_run is deliberately EXCLUDED from the identity tuple: a
            # re-run of the same transform re-emits the same claim with a new
            # via_run. Including it raised on rebuild AFTER the events were
            # journaled — bricking the case (review-3 P0). Provenance is not
            # identity; _derive_claim_id omits it, so this tuple must too.
            new_raw = (
                ev["subj"], ev["pred"], ev["obj"],
                ev.get("polarity", "supports"), ev.get("evidence", "direct"),
                ev.get("artifact"), ev.get("span_start"), ev.get("span_end"),
                ev.get("quote"),
                ev.get("valid_from"), ev.get("valid_to"),
                ev.get("pub_ts"), ev.get("time_source"))
            if existing_raw != new_raw:
                if os.environ.get("GI2_IGNORE_CLAIM_ID_CONFLICT"):
                    sys.stderr.write(
                        f"warning: claim_id {ev['claim_id']} seq {ev.get('seq')}: "
                        "differing payload; GI2_IGNORE_CLAIM_ID_CONFLICT set, ignoring\n")
                else:
                    raise GiError(
                        f"duplicate claim_id {ev['claim_id']} with differing payload "
                        f"(seq {ev.get('seq')}); journal and projection diverge. "
                        "Set GI2_IGNORE_CLAIM_ID_CONFLICT=1 (emergency hatch) or "
                        "supersede the stale claim.")
            # identical payload → idempotent replay, INSERT OR IGNORE below
        cur.execute(
            "INSERT OR IGNORE INTO claims(claim_id, subj, pred, obj, polarity, evidence, "
            "confidence, artifact, span_start, span_end, quote, via_run, "
            "valid_from, valid_to, pub_ts, time_source, asserted_ts, filed_seq) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ev["claim_id"], ev["subj"], ev["pred"], ev["obj"],
                ev.get("polarity", "supports"), ev.get("evidence", "direct"),
                ev.get("confidence"), ev.get("artifact"),
                ev.get("span_start"), ev.get("span_end"), ev.get("quote"),
                ev.get("via_run"),
                ev.get("valid_from"), ev.get("valid_to"),
                ev.get("pub_ts"), ev.get("time_source"),
                ev.get("ts"), ev.get("seq"),
            ),
        )

    elif op == "supersede":
        cid = _require_str(ev, "claim_id")      # the new/surviving claim
        target = _require_str(ev, "target_id")  # the claim being corrected
        kind = ev.get("kind", "retracts")
        if kind not in ("retracts", "corrects", "updates"):
            raise GiError(f"supersede kind must be retracts|corrects|updates (got {kind!r})")
        if cid == target:
            raise GiError("a claim cannot supersede itself")
        cur.execute(
            "INSERT OR REPLACE INTO supersedes(seq, claim_id, target_id, kind, reason, via_run) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ev.get("seq", 0), cid, target, kind,
             ev.get("reason", ""), ev.get("via_run")),
        )

    elif op == "retract":
        cid = _require_str(ev, "claim_id")
        scored = ev.get("scored", False)
        if not isinstance(scored, bool):
            raise GiError("retract field 'scored' must be a boolean")
        vr = ev.get("via_run")
        if vr is not None and not isinstance(vr, str):
            raise GiError("retract field 'via_run' must be a string or null")
        row = cur.execute(
            "SELECT via_run FROM claims WHERE claim_id = ?", (cid,)).fetchone()
        eff_run = vr or (row[0] if row else None)
        cur.execute("DELETE FROM claims WHERE claim_id = ?", (cid,))
        # Slice 5: the retraction is itself history — re-asserting the same
        # claim later does NOT erase the scored admission (no reputation
        # laundering); PK on seq keeps every retract event distinct.
        cur.execute(
            "INSERT OR REPLACE INTO retractions(seq, claim_id, via_run, scored, reason) "
            "VALUES (?, ?, ?, ?, ?)",
            (ev.get("seq", 0), cid, eff_run, 1 if scored else 0, ev.get("reason", "")),
        )

    elif op == "merge":
        _validate_merge_fields(ev)
        if aliases is not None:
            absorbed, survivor = ev["absorbed"], ev["survivor"]
            if aliases.is_absorbed(absorbed) and aliases.parent.get(absorbed) != survivor:
                raise GiError(
                    f"journal seq {ev.get('seq')}: {absorbed} is already absorbed into "
                    f"{aliases.parent[absorbed]}; re-homing requires an unmerge event first")
            if aliases.would_cycle(absorbed, survivor):
                raise GiError(
                    f"journal seq {ev.get('seq')}: merge {absorbed} into {survivor} "
                    f"would create an identity cycle")
            aliases.merge(absorbed, survivor)

    elif op == "unmerge":
        _validate_unmerge_fields(ev)
        if aliases is not None:
            aliases.unmerge(ev["entity_id"])

    elif op == "transform_run":
        _validate_transform_run_fields(ev)
        cur.execute(
            "INSERT OR REPLACE INTO transform_runs(run_id, transform, uri, artifact_hash, accepted, rejected, args) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                ev["run_id"], ev["transform"], ev["uri"], ev["artifact_hash"],
                ev["accepted"], ev["rejected"],
                canonical_json(ev.get("args") or {}).decode("utf-8"),
            ),
        )

    elif op == "mark":
        cur.execute(
            "INSERT OR REPLACE INTO marks (entity_id, mark, reason, seq) "
            "VALUES (?, ?, ?, ?)",
            (ev["entity_id"], ev["mark"], ev.get("reason"), int(ev["seq"])))

    elif op == "prospect":
        _validate_prospect_fields(ev)
        cur.execute(
            "INSERT OR REPLACE INTO prospects(prospect_id, run_id, subj, pred, obj, "
            "thesis, mechanism, anchors, kill_criterion, fetch_targets, "
            "novelty_against, seq, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ev["prospect_id"], ev["run_id"], ev["subj"], ev["pred"], ev["obj"],
             ev["thesis"], ev.get("mechanism"),
             canonical_json(ev["anchors"]).decode("utf-8"),
             canonical_json(ev["kill_criterion"]).decode("utf-8"),
             canonical_json(ev.get("fetch_targets") or []).decode("utf-8"),
             canonical_json(ev.get("novelty_against") or []).decode("utf-8"),
             int(ev["seq"]), ev["ts"]))

    elif op == "dig_accept":
        _require_str(ev, "prospect_id")
        _require_str(ev, "claim_id")
        cur.execute(
            "INSERT OR REPLACE INTO dig_accepts(prospect_id, claim_id, reason, seq) "
            "VALUES (?, ?, ?, ?)",
            (ev["prospect_id"], ev["claim_id"], ev.get("reason"), int(ev["seq"])))

    elif op == "dig_test":
        _require_str(ev, "prospect_id")
        h = _require_str(ev, "plan_hash")
        if not HASH_RE.match(h):
            raise GiError(f"dig_test plan_hash must be 'sha256:<64hex>' (got {h!r})")
        cur.execute(
            "INSERT OR REPLACE INTO dig_tests(seq, prospect_id, plan_hash) VALUES (?, ?, ?)",
            (int(ev["seq"]), ev["prospect_id"], h))

    elif op == "dig_withdraw":
        _require_str(ev, "prospect_id")
        _require_str(ev, "reason")
        cur.execute(
            "INSERT OR REPLACE INTO dig_withdraws(prospect_id, reason, seq) VALUES (?, ?, ?)",
            (ev["prospect_id"], ev["reason"], int(ev["seq"])))

    elif op == "dig_close":
        _require_str(ev, "prospect_id")
        v = _require_str(ev, "verdict")
        if v not in ("corroborated", "killed", "expired"):
            raise GiError(f"dig_close verdict must be corroborated|killed|expired (got {v!r})")
        cur.execute(
            "INSERT OR REPLACE INTO dig_closes(prospect_id, verdict, claim_id, reason, evidence, seq) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ev["prospect_id"], v, ev.get("claim_id"), ev.get("reason"),
             canonical_json(ev.get("evidence") or []).decode("utf-8"), int(ev["seq"])))

    else:
        # Treaty rule: unknown ops in old journals copy to meta table
        seq = ev.get("seq", "unknown")
        upsert_meta(cur, f"unknown_op_{seq}", canonical_json(ev).decode("utf-8"))


def full_rebuild(case: CasePaths) -> tuple[int, str]:
    errors, last_seq, last_hash = verify_chain(case)
    if errors:
        raise GiError(
            "refusing to build a projection from a corrupt journal:\n  - "
            + "\n  - ".join(errors))
    if last_seq == 0:
        raise GiError("journal is empty")

    tmp = case.root / f".case.db.rebuild-{os.getpid()}"
    with contextlib.suppress(FileNotFoundError):
        tmp.unlink()
    conn = sqlite3.connect(str(tmp))
    aliases = AliasMap()
    try:
        cur = conn.cursor()
        cur.executescript(SCHEMA)
        for _, line in iter_journal_lines(case):
            ev = json.loads(line)
            try:
                apply_event(cur, ev, aliases, case=case)
            except GiError as e:
                raise GiError(f"journal seq {ev.get('seq')} ({ev.get('op')}): {e}") from e
        _persist_aliases(cur, aliases)
        _install_claims_view(cur)
        upsert_meta(cur, "journal_seq", str(last_seq))
        upsert_meta(cur, "journal_hash", last_hash)
        upsert_meta(cur, "schema_version", str(CURRENT_SCHEMA_VERSION))
        conn.commit()
        conn.close()
        os.replace(tmp, case.db)
    except BaseException:
        with contextlib.suppress(Exception):
            conn.close()
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        raise
    return last_seq, last_hash


def ensure_projection(case: CasePaths) -> None:
    if not _projection_is_current(case):
        full_rebuild(case)


def open_projection(case: CasePaths) -> sqlite3.Connection:
    conn = sqlite3.connect(str(case.db))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def projection_has_claim(case: CasePaths, claim_id: str) -> bool:
    conn = open_projection(case)
    try:
        return conn.execute(
            "SELECT 1 FROM claims WHERE claim_id = ?", (claim_id,)
        ).fetchone() is not None
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Quote / span verification
# --------------------------------------------------------------------------

_WS_RUN = re.compile(r"\s+")


def _normalize_ws(s: str) -> str:
    return _WS_RUN.sub(" ", s).strip()


def verify_quote_span(artifact_bytes: bytes, span_start: int, span_end: int, quote: str) -> None:
    if not isinstance(quote, str) or not quote.strip():
        raise GiError("quote must be non-empty")
    if span_start < 0 or span_end <= span_start:
        raise GiError(f"invalid span [{span_start}, {span_end})")
    text = artifact_bytes.decode("utf-8", errors="replace")
    lo = max(0, span_start - QUOTE_SPAN_SLACK)
    hi = min(len(text), span_end + QUOTE_SPAN_SLACK)
    window = text[lo:hi]
    if _normalize_ws(quote) not in _normalize_ws(window):
        raise GiError(
            f"quote does not appear within ±{QUOTE_SPAN_SLACK} chars of span "
            f"[{span_start}, {span_end}) in the cited artifact; refusing to record claim"
        )


def _normalized_with_map(text: str) -> tuple[str, list[int]]:
    """Whitespace-normalized text plus a map from normalized index -> raw
    index. Mirrors _normalize_ws exactly, so spans found through this map
    satisfy the quote gate's window check. (Council r6: the gate's coordinate
    system -- utf-8 chars, errors=replace, no newline translation -- is not
    reproducible agent-side; the tool must locate quotes itself.)"""
    norm: list[str] = []
    raw_map: list[int] = []
    prev_ws = False
    for i, ch in enumerate(text):
        if ch.isspace():
            if not prev_ws and norm:
                norm.append(" ")
                raw_map.append(i)
            prev_ws = True
        else:
            norm.append(ch)
            raw_map.append(i)
            prev_ws = False
    # trailing space mirrors .strip(); a leading space cannot occur because
    # we only append " " once norm is non-empty
    while norm and norm[-1] == " ":
        norm.pop()
        raw_map.pop()
    return "".join(norm), raw_map


def quote_occurrences(artifact_bytes: bytes, quote: str, max_hits: int = 50) -> list[tuple[int, int]]:
    """All raw character spans [start, end) where `quote` occurs in the
    artifact under the gate's exact decode (utf-8, errors=replace) and the
    gate's whitespace normalization -- the same coordinate system as
    verify_quote_span. Exact matches are preferred; a whitespace-flexible
    search is the fallback. Empty list means the gate would reject any span.
    Council r6: refusing ambiguity is the analyst-attestation rule -- silently
    recording the first of several occurrences would launder the span."""
    if not isinstance(quote, str) or not quote.strip():
        raise GiError("quote must be non-empty")
    text = artifact_bytes.decode("utf-8", errors="replace")
    hits: list[tuple[int, int]] = []
    start = 0
    while True:  # exact matches first: the common, most precise case
        i = text.find(quote, start)
        if i < 0:
            break
        hits.append((i, i + len(quote)))
        if len(hits) >= max_hits:
            return hits
        start = i + 1
    if hits:
        return hits
    norm, raw_map = _normalized_with_map(text)
    qn = _normalize_ws(quote)
    if not qn:
        return []
    start = 0
    while True:
        i = norm.find(qn, start)
        if i < 0:
            break
        hits.append((raw_map[i], raw_map[i + len(qn) - 1] + 1))
        if len(hits) >= max_hits:
            break
        start = i + 1
    return hits


# --------------------------------------------------------------------------
# Network fetch (hardened)
# --------------------------------------------------------------------------

class _SchemeGuardRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        scheme = urllib.parse.urlsplit(newurl).scheme.lower()
        allowed = ("http", "https")
        if os.environ.get("GI2_ALLOW_FILE_URI") == "1":
            allowed = ("http", "https", "file")
        if scheme not in allowed:
            raise urllib.error.HTTPError(
                req.full_url, code,
                f"refusing redirect to disallowed scheme {scheme!r}", headers, fp)
        # Council r4 fix (SSRF): a redirect is a NEW destination — the same
        # public-host rule applies on every hop (open-redirect → internal
        # network must not pass through the redirect handler).
        if scheme in ("http", "https") and os.environ.get("GI2_ALLOW_PRIVATE_FETCH") != "1":
            _assert_public_host(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _assert_public_host(url: str) -> None:
    """Council r4 fix (SSRF): refuse to fetch hosts that resolve to private,
    loopback, link-local, or unspecified addresses. The transform runs with
    the host's env — an http://169.254.169.254/ or http://10.0.0.1/ fetch is
    an internal-network probe. Raises GiError before any connection is
    made. DNS is resolved HERE and the connection is made to the same host
    (urllib re-resolves; TOCTOU is accepted — the guard stops accidental
    probes, not a determined attacker with code execution)."""
    host = urllib.parse.urlsplit(url).hostname or ""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise GiError(f"cannot resolve host {host!r} for fetch") from None
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr.split("%", 1)[0])
        except ValueError:
            continue
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_unspecified or ip.is_multicast or ip.is_reserved):
            raise GiError(
                f"refusing to fetch non-public host {host!r} (resolves to "
                f"{addr}); set GI2_ALLOW_PRIVATE_FETCH=1 to override")


def fetch_url(case: CasePaths, url: str, max_bytes: int = FETCH_MAX_BYTES) -> tuple[str, int, str, str | None]:
    """Returns (hash, size, final_url, content_type). content_type is the
    Content-Type header at fetch time (Phase 2.4) — None for file:// URIs."""
    scheme = urllib.parse.urlsplit(url).scheme.lower()
    allow_file = os.environ.get("GI2_ALLOW_FILE_URI") == "1"
    allowed_schemes = ("http", "https", "file") if allow_file else ("http", "https")

    if scheme not in allowed_schemes:
        prefix = "file://" if allow_file else ""
        raise GiError(f"refusing to fetch {scheme or '(no scheme)'}:// URL; only http/https{f'/{prefix}' if allow_file else ''} allowed")

    # Council r4 fix (SSRF): destination guard before any connection
    if scheme in ("http", "https") and os.environ.get("GI2_ALLOW_PRIVATE_FETCH") != "1":
        _assert_public_host(url)

    if scheme == "file":
        raw_path = urllib.parse.unquote(urllib.parse.urlsplit(url).path)
        file_path = Path(raw_path).resolve()
        if not file_path.is_file():
            raise GiError(f"file not found for file:// URI: {raw_path}")
        hs, size = store_file(case, file_path)
        return hs, size, url, None

    opener = urllib.request.build_opener(_SchemeGuardRedirectHandler())
    req = urllib.request.Request(url, headers={"User-Agent": "gi2/1.0 (evidence fetch)"})
    case.evidence.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha256()
    size = 0
    final_url = url
    content_type = None
    fd, tmpname = tempfile.mkstemp(prefix=".fetch-", dir=str(case.evidence))
    try:
        with opener.open(req, timeout=FETCH_TIMEOUT_S) as resp, os.fdopen(fd, "wb") as out:
            final_url = resp.geturl() or url
            ct = (resp.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower() or None
            content_type = ct
            while True:
                chunk = resp.read(FETCH_CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise GiError(f"download exceeds limit of {max_bytes} bytes; aborting")
                h.update(chunk)
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
    except GiError:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmpname)
        raise
    except urllib.error.URLError as e:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmpname)
        raise GiError(f"fetch failed for {url}: {e}") from e
    hs = f"sha256:{h.hexdigest()}"
    dest = case.object_path(hs)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        os.unlink(tmpname)
    else:
        os.replace(tmpname, dest)
    return hs, size, final_url, content_type


# --------------------------------------------------------------------------
# CLI commands
# --------------------------------------------------------------------------

def cmd_init(args) -> None:
    root = resolve_case(args)
    case = CasePaths(root)
    if case.journal.exists() and case.journal.stat().st_size > 0:
        die(f"case already initialized at {root}")
    case.evidence.mkdir(parents=True, exist_ok=True)
    ev = transact(case, "case_init", format=FORMAT_VERSION, tool="gi2", version="1.0")
    print(f"initialized case at {root} (journal seq {ev['seq']})")


def _parse_attrs(pairs: list[str]) -> dict | None:
    out = {}
    for p in pairs:
        if "=" not in p:
            die(f"--attr expects KEY=VALUE (got {p!r})")
        k, v = p.split("=", 1)
        if not k:
            die("--attr key must be non-empty")
        out[k] = v
    return out or None


def _canon_norm(s: str) -> str:
    """Shared id normalization (review-3): strip kind prefixes, fold dots
    and all non-alphanumerics to dashes. Used by BOTH the registration
    guard (1.7) and lint's twin check — review-3 found lint using a weaker
    normalizer that was blind to exactly the model:-prefixed twins 1.7
    was built to catch (Kimi)."""
    s = s.split(":", 1)[1] if ":" in s else s
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _entity_dup_guard(case: CasePaths, eid: str, force: bool) -> None:
    """1.7 (plan-review 2026-08-16): refuse registrations that duplicate an
    existing entity modulo kind-prefix/dot-dash normalization -- the exact
    mechanism behind every identity split in case 'ai' (glm-5.2 vs glm-5-2,
    model:glm-5-3 vs glm-5-3, model:gemini-3-7-flash vs gemini-3.7-flash).
    Exact re-registration is a warning (attrs-updates are legitimate);
    normalized collisions are an error unless --force."""
    conn = open_projection(case)
    try:
        existing = {r[0] for r in conn.execute("SELECT id FROM entities")}
        existing |= {r[0] for r in conn.execute("SELECT absorbed_id FROM aliases")}
    finally:
        conn.close()
    if eid in existing:
        print(f"warning: entity {eid!r} is already registered; if this is a rename "
              f"or absorb, consider 'merge' instead", file=sys.stderr)
    n_new = _canon_norm(eid)
    for other in sorted(existing):
        if other != eid and _canon_norm(other) == n_new:
            msg = (f"entity {eid!r} duplicates {other!r} modulo kind-prefix/"
                   f"dot-dash normalization; cite the existing id, or 'merge' if "
                   f"these are truly one thing. Use --force to override")
            if not force:
                raise GiError(msg)
            print(f"warning: {msg}", file=sys.stderr)


def cmd_entity(args) -> None:
    case = require_case(resolve_case(args))
    kind = args.kind or "entity"
    if args.id:
        eid = args.id
    else:
        slug = re.sub(r"[^a-z0-9]+", "-", args.name.lower()).strip("-") or "unnamed"
        eid = f"{kind}:{slug}"
    _entity_dup_guard(case, eid, getattr(args, "force", False))
    ev = transact(case, "entity", id=eid, kind=kind, name=args.name,
                  attrs=_parse_attrs(args.attr))
    print(f"entity {eid} [{kind}] \"{args.name}\" recorded at seq {ev['seq']}")


def _derive_claim_id(f: dict) -> str:
    # 1.3 (plan-review 2026-08-16): tritemporal fields enter the stable hash —
    # the same quote asserted with a different validity window is a DIFFERENT
    # claim; omitting them collided ids and (with 1.2) would raise on
    # legitimate re-assertions.
    stable = {k: f.get(k) for k in
              ("subj", "pred", "obj", "polarity", "evidence",
               "artifact", "span_start", "span_end", "quote",
               "valid_from", "valid_to", "pub_ts", "time_source")}
    return "c_" + sha256_hex(canonical_json(stable))[:16]


def _known_entity_ids(case: CasePaths) -> set[str]:
    """Every id a claim endpoint may legitimately cite: registered entities,
    canonical ids (post-merge citable forms), and absorbed aliases (claims
    filed under an absorbed id before a merge remain legitimate)."""
    conn = open_projection(case)
    try:
        ids = {r[0] for r in conn.execute("SELECT id FROM entities")}
        ids |= {r[0] for r in conn.execute("SELECT canon_id FROM id_canon")}
        ids |= {r[0] for r in conn.execute("SELECT absorbed_id FROM aliases")}
        return ids
    finally:
        conn.close()


def _guard_claim_entities(subj: str, obj: str, entity_ids: set[str], *, strict: bool) -> None:
    """Council r6 (entity drift): warn -- or refuse under --strict-entities --
    when a claim endpoint is not a registered entity, with fuzzy suggestions.
    Lives at the CLI write edge only: never in _validate_claim_fields or
    apply_event, which run on replay and must tolerate historical journals."""
    ordered = sorted(entity_ids)
    for role, endpoint in (("subj", subj), ("obj", obj)):
        if endpoint in entity_ids:
            continue
        sugg = [e for e in ordered if e.endswith(":" + endpoint)]  # kind-prefix drop: glm-5-3 -> model:glm-5-3
        sugg += [e for e in difflib.get_close_matches(endpoint, ordered, n=3, cutoff=0.6)
                 if e not in sugg]
        hint = (" did you mean: " + ", ".join(sugg[:3]) + "?") if sugg else \
               " no close match -- register it with 'entity' first (ids are kind:slug)"
        msg = f"{role} {endpoint!r} is not a registered entity;{hint}"
        if strict:
            raise GiError(msg)
        print(f"warning: {msg}", file=sys.stderr)


def _companion_for(case: CasePaths, artifact: str) -> str | None:
    """Return the html-visible-v1 companion hash for a raw-HTML artifact, if
    one exists in the projection. Companion-aware filing (4.3): when a quote
    is ambiguous or absent in raw HTML but present in the companion's clean
    prose, the gate prints the companion hash and refuses to guess."""
    conn = sqlite3.connect(case.db)
    try:
        row = conn.execute(
            "SELECT hash FROM artifacts WHERE derived_from = ? AND extractor = 'html-visible-v1' LIMIT 1",
            (artifact,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _prepare_claim(case: CasePaths, raw: dict, *, entity_ids: set[str] | None = None,
                   strict_entities: bool = False, art_cache: dict | None = None,
                   prefer_extract: bool = False) -> dict:
    """Validate one claim (CLI args or NDJSON batch line) and return the
    journal-ready event fields, claim_id included. Raises GiError on any
    failure; never appends to the journal. Shared by the single-claim path,
    --batch, and any future caller. Council r6."""
    subj = raw.get("subj")
    pred = raw.get("pred")
    obj = raw.get("obj")
    polarity = raw.get("polarity") or "supports"
    evidence = raw.get("evidence") or "direct"
    confidence = raw.get("confidence")
    artifact = raw.get("artifact")
    quote = raw.get("quote")
    span = raw.get("span")
    valid_from = raw.get("valid_from")
    valid_to = raw.get("valid_to")
    pub_ts = raw.get("pub_ts")
    time_source = raw.get("time_source")

    if not subj or not pred or not obj:
        raise GiError("subj, pred and obj are required")

    # 3.2 (plan-review 2026-08-16): shell-scar linter. Claim text passed
    # through shell heredocs gets mangled by interpolation ($0 -> script
    # name, $VAR -> empty, stray /bin/sh fragments). Case ai carries live
    # examples ('/bin/sh.70 blended'). Refuse at the gate — the corruption
    # class dies here rather than being superseded later.
    SHELL_SCAR_PATTERNS = (
        "/bin/sh", "/bin/bash", "sh -c", "bash -c",
    )
    for field_name, value in (("subj", subj), ("pred", pred), ("obj", obj)):
        if not isinstance(value, str):
            continue
        for pat in SHELL_SCAR_PATTERNS:
            if pat in value:
                raise GiError(
                    f"{field_name} {value!r} contains shell interpolation scar "
                    f"({pat!r}) — claim text was likely mangled by a heredoc. "
                    "File claims via --batch JSONL or --file, never inline "
                    "shell strings")
    if isinstance(confidence, str) and confidence.startswith("$"):
        raise GiError("confidence looks like an unexpanded shell variable "
                      f"({confidence!r})")

    if confidence is not None and (isinstance(confidence, bool) or
                                   not isinstance(confidence, (int, float)) or
                                   not 0.0 <= float(confidence) <= 1.0):
        raise GiError(f"confidence must be in [0.0, 1.0] (got {confidence!r})")
    if confidence is not None:
        confidence = float(confidence)
    else:
        # 1.4 remediation (review-3): the filing-time default previously lived
        # only in cmd_claim, so --batch/--file lines with omitted confidence
        # journaled NULL — which the belief kernel treats as categorical
        # certainty (1.0). The exact bug 1.4 existed to close. Every filing
        # path now defaults; hypothesis claims stay at their explicit 0.0
        # convention unless the analyst says otherwise.
        confidence = 0.0 if evidence == "hypothesis" else CLI_DEFAULT_CONFIDENCE
    # Council r4 fix (honor system): 'direct' and 'inferred' claims carry the
    # analyst's implicit warrant that a source was actually consulted -- an
    # uncited 'direct' claim is belief-laundering by declaration. Hypotheses
    # are exempt (they are explicitly unevidenced, confidence 0).
    if evidence in ("direct", "inferred") and not artifact:
        raise GiError(f"--evidence {evidence} requires --artifact sha256:… -- "
                      f"assertions about the world must cite stored evidence; "
                      f"use --evidence hypothesis for unevidenced candidate edges")

    span_start = span_end = None
    if artifact:
        # 4.3 --prefer-extract: redirect the citation to the companion's clean
        # prose when one exists. The claim then cites the companion hash and
        # verify checks exactly that hash (one-hash-one-claim, treaty intact).
        if prefer_extract:
            comp = _companion_for(case, artifact)
            if comp:
                print(f"note: --prefer-extract: citing companion {comp} "
                      "(html-visible-v1) instead of raw HTML", file=sys.stderr)
                artifact = comp
        if not HASH_RE.match(artifact):
            raise GiError(f"--artifact must be 'sha256:<64hex>' (got {artifact!r})")
        if quote is None:
            raise GiError("--artifact requires --quote TEXT "
                          "(the span is auto-located when --span is omitted)")
        if art_cache is not None and artifact in art_cache:
            data = art_cache[artifact]  # read_bytes re-hashes per call; memoize in batch
        else:
            data = read_bytes(case, artifact)
            if art_cache is not None:
                art_cache[artifact] = data
        hits = quote_occurrences(data, quote)
        if span:
            span_start, span_end = int(span[0]), int(span[1])
            if not any(a == span_start and b == span_end for a, b in hits):
                if hits:
                    print(f"warning: span [{span_start}, {span_end}) does not point "
                          f"exactly at the quote (true span [{hits[0][0]}, {hits[0][1]})); "
                          f"accepted only via ±{QUOTE_SPAN_SLACK}-char slack", file=sys.stderr)
                # no hits at all -> verify_quote_span below is the final word
        else:
            # Council r6: auto-locate, but refuse ambiguity -- the first of
            # several occurrences is a location the analyst never attested.
            if not hits:
                # 4.3: a companion may hold the quote in clean prose where raw
                # HTML hides it in markup duplication. Point, never guess.
                comp = _companion_for(case, artifact)
                raise GiError("quote not found anywhere in the artifact under the "
                              "gate's decode (utf-8 chars, errors=replace, ws-normalized); "
                              "no span can be accepted -- re-quote from the stored bytes "
                              f"(see find-quote){f'; a html-visible-v1 companion exists: {comp} -- re-file against the companion hash' if comp else ''}")
            if len(hits) > 1:
                comp = _companion_for(case, artifact)
                shown = ", ".join(f"[{a}, {b})" for a, b in hits[:5])
                more = ", …" if len(hits) > 5 else ""
                hint = f"; NOTE: a html-visible-v1 companion exists ({comp}) -- quoting from the companion's clean prose avoids tag-duplication ambiguity; re-file with --artifact {comp}" if comp else ""
                raise GiError(f"quote is ambiguous: {len(hits)} occurrences ({shown}{more}); "
                              "give a longer quote or an explicit --span (see find-quote)"
                              f"{hint}")
            span_start, span_end = hits[0]
            print(f"note: auto-located quote at [{span_start}, {span_end})", file=sys.stderr)
        verify_quote_span(data, span_start, span_end, quote)  # the verbatim gate, always
    elif span or quote:
        raise GiError("--span / --quote are only meaningful with --artifact")

    if entity_ids is not None:
        _guard_claim_entities(subj, obj, entity_ids, strict=strict_entities)

    fields = dict(subj=subj, pred=pred, obj=obj, polarity=polarity,
                  evidence=evidence, confidence=confidence, artifact=artifact,
                  span_start=span_start, span_end=span_end, quote=quote,
                  valid_from=valid_from, valid_to=valid_to,
                  pub_ts=pub_ts, time_source=time_source)
    claim_id = raw.get("id") or _derive_claim_id(fields)
    fields["claim_id"] = claim_id
    _validate_claim_fields(fields)
    return fields


def cmd_claim(args) -> None:
    case = require_case(resolve_case(args))
    ensure_projection(case)
    if getattr(args, "batch", None):
        if args.subj or args.pred or args.obj:
            die("--batch takes no positional subj/pred/obj")
        _cmd_claim_batch(case, args)
        return
    # 3.1 (plan-review 2026-08-16): --file is sugar over --batch — a single
    # JSON object or NDJSON lines, from a path or '-' (stdin). Routed through
    # the SAME _cmd_claim_batch pipeline so file claims get identical
    # validation, linter, auto-span and staging guarantees — not a third path.
    if getattr(args, "file", None):
        if args.subj or args.pred or args.obj:
            die("--file takes no positional subj/pred/obj")
        raw = sys.stdin.read() if args.file == "-" else Path(args.file).read_text(encoding="utf-8")
        lines = [ln for ln in (l.strip() for l in raw.splitlines()) if ln]
        if not lines:
            die(f"--file {args.file!r} contains no JSON")
        objs = []
        for i, ln in enumerate(lines, 1):
            try:
                o = json.loads(ln)
            except json.JSONDecodeError as e:
                # 3.1: allow a single pretty-printed JSON object spanning lines
                if len(lines) == 1:
                    die(f"--file line 1 is not valid JSON: {e}")
                die(f"--file line {i} is not valid JSON: {e}")
            if not isinstance(o, dict):
                die(f"--file line {i} is not a JSON object")
            objs.append(o)
        import tempfile as _tf
        with _tf.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8") as tf:
            for o in objs:
                tf.write(json.dumps(o) + "\n")
            tmp = tf.name
        args.batch = tmp
        try:
            _cmd_claim_batch(case, args)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
        return
    if not (args.subj and args.pred and args.obj):
        die("claim requires SUBJ PRED OBJ (or use --batch FILE)")

    if args.confidence is not None and not (0.0 <= args.confidence <= 1.0):
        die("--confidence must be in [0.0, 1.0]")

    if args.evidence == "hypothesis" and args.confidence is not None \
            and args.confidence > HYPOTHESIS_CONF_CAP:
        print(f"warning: hypothesis confidence {args.confidence} exceeds the "
              f"{HYPOTHESIS_CONF_CAP} cap; the belief kernel will treat it as "
              f"{HYPOTHESIS_CONF_CAP} (the journal still records {args.confidence})",
              file=sys.stderr)
    if args.confidence is None:
        # 1.4 (plan-review 2026-08-16): unquantified confidence fused at 1.0 —
        # ~15 historical claims ride at full certainty by omission. New CLI
        # filings now default to 0.7 (documented), keeping the analyst honest
        # without touching historical rows (belief.py stays out of this by
        # treaty: defaults are written at filing time, never in the kernel).
        args.confidence = CLI_DEFAULT_CONFIDENCE
        print(f"note: no --confidence given; defaulting to {CLI_DEFAULT_CONFIDENCE} "
              "(pass --confidence to override)", file=sys.stderr)

    try:
        fields = _prepare_claim(
            case,
            dict(subj=args.subj, pred=args.pred, obj=args.obj,
                 polarity=args.polarity, evidence=args.evidence,
                 confidence=args.confidence, artifact=args.artifact,
                 span=tuple(args.span) if args.span else None,
                 quote=args.quote, id=args.id,
                 valid_from=args.valid_from, valid_to=args.valid_to,
                 pub_ts=args.pub_ts, time_source=args.time_source),
            entity_ids=_known_entity_ids(case),
            strict_entities=bool(getattr(args, "strict_entities", False)),
            prefer_extract=bool(getattr(args, "prefer_extract", False)))
    except GiError as e:
        die(str(e))
    claim_id = fields["claim_id"]
    if projection_has_claim(case, claim_id):
        die(f"claim {claim_id} already exists (identical claim already asserted); "
            f"use 'retract {claim_id}' first if you intend to revise it")
    ev = transact(case, "claim", **fields)
    cited = f" citing {fields['artifact']} [{fields['span_start']}:{fields['span_end']}]" if fields["artifact"] else ""
    window = ""
    if args.valid_from:
        window = f" valid [{args.valid_from or '-∞'}, {args.valid_to or '+∞'})"
    print(f"claim {claim_id} recorded at seq {ev['seq']}{cited}{window}")


def _cmd_claim_batch(case: CasePaths, args) -> None:
    """Council r6: NDJSON batch claims. Every line is fully validated and
    staged BEFORE any event is appended -- a line that trips a check during
    rebuild after appending would brick the case (no un-journal command
    exists). Duplicates are caught via a seen-set because the pre-batch
    projection cannot see staged events. One lock, one rebuild."""
    path = Path(args.batch)
    if not path.is_file():
        die(f"batch file not found: {path}")
    entity_ids = _known_entity_ids(case)
    conn = open_projection(case)
    try:
        existing = {r[0] for r in conn.execute("SELECT claim_id FROM claims")}
    finally:
        conn.close()
    art_cache: dict[str, bytes] = {}
    staged: list[tuple[int, dict]] = []
    failures: list[tuple[int, str]] = []
    seen: set[str] = set()
    n_lines = 0
    for lineno, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line:
            continue
        n_lines += 1
        try:
            item = json.loads(line)
            if not isinstance(item, dict):
                raise GiError("batch line is not a JSON object")
            span = item.get("span")
            if span is None:
                pass
            elif isinstance(span, (list, tuple)) and len(span) == 2:
                item["span"] = (int(span[0]), int(span[1]))
            else:
                raise GiError(f"invalid span {span!r} (expect [start, end])")
            fields = _prepare_claim(case, item, entity_ids=entity_ids,
                                    strict_entities=bool(getattr(args, "strict_entities", False)),
                                    art_cache=art_cache)
            cid = fields["claim_id"]
            if cid in seen:
                raise GiError(f"duplicate claim id {cid} (repeated within this batch)")
            if cid in existing:
                raise GiError(f"claim {cid} already exists in the case")
            seen.add(cid)
            staged.append((lineno, fields))
        except GiError as e:
            failures.append((lineno, str(e)))
        except json.JSONDecodeError as e:
            failures.append((lineno, f"invalid JSON: {e}"))
    events = transact_batch(case, [("claim", f) for _, f in staged]) if staged else []
    for (lineno, f), ev in zip(staged, events):
        print(f"[ok] line {lineno}: {f['claim_id']} at seq {ev['seq']}")
    for lineno, reason in failures:
        print(f"[fail] line {lineno}: {reason}", file=sys.stderr)
    print(f"batch: {len(staged)} recorded, {len(failures)} failed, {n_lines} line(s)")
    if failures:
        raise SystemExit(1)


def _citation_fields(case: CasePaths, artifact: str | None, span, quote: str | None) -> dict:
    if artifact:
        if not HASH_RE.match(artifact):
            die(f"--artifact must be 'sha256:<64hex>' (got {artifact!r})")
        if span is None or quote is None:
            die("--artifact requires --span START END and --quote TEXT")
        span_start, span_end = span
        data = read_bytes(case, artifact)
        verify_quote_span(data, span_start, span_end, quote)
        return dict(artifact=artifact, span_start=span_start,
                    span_end=span_end, quote=quote)
    if span is not None or quote is not None:
        die("--span / --quote are only meaningful with --artifact")
    return dict(artifact=None, span_start=None, span_end=None, quote=None)


def _load_entity_row(conn: sqlite3.Connection, eid: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM entities WHERE id = ?", (eid,)).fetchone()


def cmd_merge(args) -> None:
    case = require_case(resolve_case(args))
    ensure_projection(case)
    if not args.reason.strip():
        die("--reason must be non-empty (this is a forensic journal)")

    absorbed, survivor = args.absorbed, args.survivor
    if absorbed == survivor:
        die(f"self-merge refused: {absorbed} into itself")

    conn = open_projection(case)
    try:
        a_row = _load_entity_row(conn, absorbed)
        s_row = _load_entity_row(conn, survivor)
        parent = _load_parent_map(conn)
        aliases = AliasMap(parent)
    finally:
        conn.close()

    if a_row is None and s_row is None:
        die(f"unknown IDs: {absorbed!r} and {survivor!r} are not registered entities")
    if a_row is None:
        die(f"unknown ID: {absorbed!r} is not a registered entity")
    if s_row is None:
        die(f"unknown ID: {survivor!r} is not a registered entity")

    if aliases.would_cycle(absorbed, survivor):
        die(f"cycle refused: merging {absorbed} into {survivor} would invert "
            f"an existing identity chain (root {aliases.find(absorbed)})")

    if aliases.find(absorbed) == aliases.find(survivor):
        print(f"already merged: {absorbed} resolves to {aliases.find(survivor)}; "
              f"idempotent no-op; nothing journaled")
        return

    if aliases.is_absorbed(absorbed):
        die(f"{absorbed} is already absorbed into {aliases.parent[absorbed]}; "
            f"unmerge it first if you want to re-home it into {survivor}")

    if aliases.is_absorbed(survivor):
        canonical = aliases.find(survivor)
        die(f"{survivor} is itself absorbed into {canonical}; "
            f"merge into {canonical} instead (survivor must be canonical)")

    a_kind = a_row["kind"] or "entity"
    s_kind = s_row["kind"] or "entity"
    if a_kind != s_kind:
        print(f"warning: kind mismatch: {absorbed} is {a_kind!r}, {survivor} is {s_kind!r}; "
              f"merge allowed but this is usually a mistake", file=sys.stderr)

    cite = _citation_fields(case, args.artifact, args.span, args.quote)
    ev = transact(case, "merge", absorbed=absorbed, survivor=survivor,
                  reason=args.reason, **cite)
    cited = f" citing {cite['artifact']} [{cite['span_start']}:{cite['span_end']}]" \
        if cite["artifact"] else ""
    print(f"merged {absorbed} into {survivor} at seq {ev['seq']}{cited}")


def cmd_unmerge(args) -> None:
    case = require_case(resolve_case(args))
    ensure_projection(case)
    if not args.reason.strip():
        die("--reason must be non-empty (this is a forensic journal)")

    conn = open_projection(case)
    try:
        hop = conn.execute(
            "SELECT survivor_id FROM aliases WHERE absorbed_id = ?",
            (args.entity_id,),
        ).fetchone()
        root = _resolve_id(conn, args.entity_id)
    finally:
        conn.close()

    if hop is None:
        die(f"unmerge refused: {args.entity_id} is not currently absorbed "
            f"(never merged, or already unmerged)")

    ev = transact(case, "unmerge", entity_id=args.entity_id, reason=args.reason)
    print(f"unmerged {args.entity_id} (was absorbed into {hop['survivor_id']}, "
          f"canonical {root}) at seq {ev['seq']}")


def cmd_retract(args) -> None:
    case = require_case(resolve_case(args))
    ensure_projection(case)
    if not args.reason.strip():
        die("--reason must be non-empty (this is a forensic journal)")
    conn = open_projection(case)
    try:
        row = conn.execute(
            "SELECT via_run FROM claims WHERE claim_id = ?", (args.claim_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        die(f"no such claim in the projection: {args.claim_id}")
    via_run = row["via_run"] if row else None
    ev = transact(case, "retract", claim_id=args.claim_id, reason=args.reason,
                  scored=args.scored, via_run=via_run)
    scored_note = " [SCORED: counts against its source's reputation]" if args.scored else \
                  " [unscored: reputation-neutral]"
    print(f"claim {args.claim_id} retracted at seq {ev['seq']}{scored_note} "
          f"(event is preserved in the journal; the projection no longer shows it)")


def cmd_retract_run(args) -> None:
    case = require_case(resolve_case(args))
    ensure_projection(case)
    if not args.reason.strip():
        die("--reason must be non-empty (this is a forensic journal)")
    conn = open_projection(case)
    try:
        rows = conn.execute(
            "SELECT claim_id FROM claims WHERE via_run = ? ORDER BY claim_id",
            (args.run_id,)).fetchall()
        known = [r["run_id"] for r in conn.execute(
            "SELECT run_id FROM transform_runs ORDER BY run_id")]
    finally:
        conn.close()
    if not rows:
        if args.run_id in known:
            die(f"run {args.run_id} has no active claims (already retracted?)")
        die(f"no claims found for run {args.run_id!r}; "
            f"known runs: {', '.join(known) if known else '(none)'}")
    scored = not args.no_scored
    ids = [r["claim_id"] for r in rows]
    # Batch: append all retract events under ONE lock, rebuild once.
    with exclusive_lock(case):
        last = None
        for cid in ids:
            last = _append_event_locked(case, "retract", claim_id=cid,
                                        reason=args.reason, scored=scored,
                                        via_run=args.run_id)
        full_rebuild(case)
    scored_note = " (SCORED against the run's reputation)" if scored else " (unscored)"
    print(f"retracted {len(ids)} claim(s) from run {args.run_id} through seq {last['seq']}{scored_note}")


def cmd_supersede(args) -> None:
    case = require_case(resolve_case(args))
    ensure_projection(case)
    if not args.reason.strip():
        die("--reason must be non-empty (this is a forensic journal)")
    conn = open_projection(case)
    try:
        new_row = conn.execute(
            "SELECT 1 FROM claims WHERE claim_id = ?", (args.claim_id,)).fetchone()
        tgt_row = conn.execute(
            "SELECT superseded FROM claims WHERE claim_id = ?", (args.target_id,)
        ).fetchone()
        already = conn.execute(
            "SELECT claim_id FROM supersedes WHERE target_id = ?", (args.target_id,)
        ).fetchone()
    finally:
        conn.close()
    if new_row is None:
        die(f"no such claim in the projection: {args.claim_id}")
    if tgt_row is None:
        die(f"no such claim in the projection: {args.target_id}")
    if already is not None:
        die(f"claim {args.target_id} is already superseded by {already['claim_id']}; "
            f"supersede the surviving claim instead if it too is wrong")
    ev = transact(case, "supersede", claim_id=args.claim_id,
                  target_id=args.target_id, kind=args.kind, reason=args.reason)
    print(f"claim {args.target_id} {args.kind} by {args.claim_id} at seq {ev['seq']} "
          f"(target is excluded from fusion but preserved in history; "
          f"see 'timeline {args.target_id}')")


def cmd_reputation(args) -> None:
    case = require_case(resolve_case(args))
    ensure_projection(case)
    conn = open_projection(case)
    try:
        scores = score_sources(conn)
    finally:
        conn.close()
    if not scores:
        print("(no active claims — no sources to score)")
        return
    print(f"{'source':<20} {'kind':<7} {'claims':>6} {'corr':>5} {'punish':>6} "
          f"{'reliab':>7} {'mult':>6}")
    for s in scores.values():
        print(f"{s.source:<20} {s.kind:<7} {s.n_claims:>6} {s.alpha:>5.0f} {s.beta:>6.0f} "
              f"{s.reliability:>7.3f} {s.multiplier:>6.3f}")
    discounted = [s for s in scores.values() if s.multiplier < 1.0]
    if not discounted:
        print("\n(no source is currently discounted; all multipliers are 1.000)")
    else:
        print("\ndiscounted source(s):")
        for s in discounted:
            print(f"  {s.source}: ×{s.multiplier:.3f} "
                  f"(α={s.alpha:.0f} corroborations, β={s.beta:.0f} punishments)")


CLASS_GLYPH = {"FLIPS": "\u26a1 FLIPS", "WEAKENED": "weakened",
               "STRENGTHENED": "strengthened", "SURVIVES": "survives",
               "UNTOUCHED": "untouched", "COLLAPSES": "collapses"}


def cmd_whatif(args) -> None:
    case = require_case(resolve_case(args))
    ensure_projection(case)
    conn = open_projection(case)
    try:
        disc = None if args.no_rep else discount_from_scores(score_sources(conn))
        try:
            rows = _whatif(conn, args.source, mode=args.mode, discount=disc)
        except KeyError as e:
            die(str(e.args[0] if e.args else e))
        masked_total = 0
        edges_here = []
        for r in rows:
            masked_total += r["masked_here"]
            if r["classification"] != "UNTOUCHED" or r["masked_here"]:
                edges_here.append(r)
        src = args.source
        print(f"discrediting {src} — {masked_total} claim(s) masked, "
              f"mode={args.mode} — SIMULATION ONLY, journal untouched")
        if not edges_here:
            print("  (no active claims match this source; nothing to simulate)")
            return
        flips = [r for r in edges_here if r["classification"] == "FLIPS"]
        weak = [r for r in edges_here if r["classification"] == "WEAKENED"]
        strong = [r for r in edges_here if r["classification"] == "STRENGTHENED"]
        print(f"\n{len(edges_here)} edge(s) affected:")
        for r in edges_here:
            e = r["edge"]; bf, af = r["before"], r["after"]
            g = CLASS_GLYPH.get(r["classification"], r["classification"])
            print(f"  {e[0]} --{e[1]}--> {e[2]}")
            print(f"      {bf['verdict']:9s} b={bf['b']:.2f} d={bf['d']:.2f}  →  "
                  f"{af['verdict']:9s} b={af['b']:.2f} d={af['d']:.2f}   {g}")
        n_flip = len(flips); n_weak = len(weak); n_strong = len(strong)
        print(f"\n{n_flip} flip(s) · {n_weak} weakened · {n_strong} strengthened "
              f"(bogus refuter removed)")
        # load-bearing warnings: edges that now rest on a single artifact
        for r in flips + weak:
            clusters = [c for c in r["after"]["clusters"]]
            arts = {c["artifact"] for c in clusters if c["artifact"]}
            if len(arts) == 1:
                a = next(iter(arts))
                print(f"\u26a0 {r['edge'][0]} --{r['edge'][1]}--> {r['edge'][2]} now rests "
                      f"on a SINGLE artifact {_short_hash(a)}…")
        if flips:
            print("\nto make this real: gi2.py retract-run <run> --reason \"...\" "
                  "(scored surgery, not simulation)")
    finally:
        conn.close()


def cmd_loadbearing(args) -> None:
    case = require_case(resolve_case(args))
    ensure_projection(case)
    conn = open_projection(case)
    try:
        subj = _resolve_id(conn, args.subj)
        obj = _resolve_id(conn, args.obj)
        disc = None if args.no_rep else discount_from_scores(score_sources(conn))
        try:
            res = _loadbearing(conn, subj, args.pred, obj,
                               threshold=args.threshold, discount=disc)
        except Exception as e:
            die(f"loadbearing failed: {e}")
        base = res["base"]
        if not base["clusters"]:
            print(f"edge {subj} --{args.pred}--> {obj}: no active claims")
            return
        print(f"edge {subj} --{args.pred}--> {obj}: [{base['verdict']}] "
              f"(b={base['b']:.4f}, d={base['d']:.4f}) — what holds it up:")
        for m in res["marginals"]:
            star = "\u2605 load-bearing" if (m["marginal_b"] + m["marginal_d"]) >= 0.15 \
                else "redundant" if (m["marginal_b"] + m["marginal_d"]) < 0.02 \
                else "partial"
            print(f"  artifact {_short_hash(m['artifact'])}…  "
                  f"marginal b\u2212{m['marginal_b']:.2f} d\u2212{m['marginal_d']:.2f}  "
                  f"[{star}] (without it: {m['verdict_without']})")
        cut = res["cut"]
        if cut:
            print(f"\ngreedy minimum cut ({len(cut)} artifact(s), approximate): "
                  f"{', '.join(_short_hash(a) + '\u2026' for a in cut)}")
            ac = res["after_cut"]
            print(f"  removing them → [{ac['verdict']}] b={ac['b']:.2f} "
                  f"(collapse threshold b<{res['threshold']})")
        else:
            print("\n(claims without artifacts dominate this edge; "
                  "no artifact cut exists)")
    finally:
        conn.close()


def _load_marks(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in conn.execute("SELECT entity_id, mark, reason FROM marks ORDER BY seq"):
        out.setdefault(r["entity_id"], []).append(
            {"mark": r["mark"], "reason": r["reason"]})
    return out


def _mark_suffix(marks: dict[str, list[dict]], eid: str) -> str:
    ms = marks.get(eid)
    if not ms:
        return ""
    return "  " + " ".join(f"[{m['mark']}]" for m in ms)


def cmd_neighbors(args) -> None:
    case = require_case(resolve_case(args))
    ensure_projection(case)
    conn = open_projection(case)
    try:
        root = _resolve_id(conn, args.entity)
        scores = score_sources(conn)
        disc = None if args.no_rep else discount_from_scores(scores)
        ranked = rank_neighbors(conn, root, discount=disc, limit=args.limit)
        visited = visited_ids(case.root)
        marks = _load_marks(conn)
        touch_trail(case.root, root, utc_now())

        ent = _load_entity_row(conn, root)
        label = f" \"{ent['name']}\"" if ent is not None and ent["name"] else ""
        deg = degree_of(conn, root)
        print(f"entity {root}{label} — degree {deg}")
        trail = load_session(case.root)
        if len(trail) > 1:
            recent = " → ".join(t["id"] for t in trail[:6])
            print(f"trail: {recent}")
        if not ranked:
            print("(no active edges — dead end or isolated entity)")
            return
        # collapse to one row per neighbour (strongest connecting edge wins)
        best: dict[str, dict] = {}
        for n in ranked:
            cur = best.get(n["other"])
            if cur is None or n["score"] > cur["score"]:
                best[n["other"]] = n
        for n in sorted(best.values(), key=lambda x: -x["score"]):
            arrow = "--{}-->".format(n["pred"]) if n["dir"] == "out" \
                else "<--{}--".format(n["pred"])
            vis = "  [visited]" if n["other"] in visited and n["other"] != root else ""
            nedges = len([e for e in ranked if e["other"] == n["other"]])
            multi = f"  ({nedges} edges)" if nedges > 1 else ""
            dead = "  (dead end)" if n["degree"] == 0 else ""
            hub = "  ⚠ hub" if n["hub"] > 1.0 else ""
            print(f"  {n['other']:<40} {arrow:<28} {n['verdict']:<9} "
                  f"b={n['b']:.2f} d={n['d']:.2f}  degree {n['degree']:>3}"
                  f"{multi}{vis}{dead}{hub}{_mark_suffix(marks, n['other'])}")
    finally:
        conn.close()


def cmd_expand(args) -> None:
    case = require_case(resolve_case(args))
    ensure_projection(case)
    conn = open_projection(case)
    try:
        root = _resolve_id(conn, args.entity)
        scores = score_sources(conn)
        disc = None if args.no_rep else discount_from_scores(scores)
        res = expand_rings(conn, root, depth=args.depth, budget=args.budget,
                           discount=disc)
        marks = _load_marks(conn)
        touch_trail(case.root, root, utc_now())
        print(f"expansion from {root} — depth {args.depth}, budget {args.budget}: "
              f"{len(res['nodes'])} node(s), {len(res['edges'])} edge(s)"
              + (f", {res['cut']} cut by budget" if res["cut"] else ""))
        for d in sorted(res["by_ring"]):
            ring = res["by_ring"][d]
            if not ring:
                continue
            print(f"  ring {d}:")
            for n in ring:
                mk = _mark_suffix(marks, n["id"])
                hub = "  ⚠ hub" if max(1.0, n["degree"] / 8.0) > 1.0 else ""
                print(f"    {n['id']:<40} score={n['score']:.3f}  degree {n['degree']:>3}{hub}{mk}")
        if res["cut"]:
            print("  (budget reached — the cut discarded the lowest-ranked; "
                  "raise --budget or increase --depth to see more)")
    finally:
        conn.close()


def cmd_mark(args) -> None:
    case = require_case(resolve_case(args))
    ensure_projection(case)
    conn = open_projection(case)
    try:
        root = _resolve_id(conn, args.entity)
        if _load_entity_row(conn, args.entity) is None and not conn.execute(
                "SELECT 1 FROM claims WHERE subj = ? OR obj = ? LIMIT 1",
                (root, root)).fetchone():
            die(f"unknown entity {args.entity!r} (not registered, not in any claim)")
        transact(case, "mark", entity_id=args.entity, mark=args.mark,
                 reason=args.reason)
        note = f": {args.reason}" if args.reason else ""
        print(f"[ok] {args.entity} marked [{args.mark}]{note} "
              f"(journal event — survives rebuild)")
    finally:
        conn.close()


def cmd_trail(args) -> None:
    case = require_case(resolve_case(args))
    trail = load_session(case.root)
    if args.clear:
        session_path(case.root).unlink(missing_ok=True)
        print("[ok] session trail cleared (dotfile only — journal untouched)")
        return
    if not trail:
        print("(empty trail — no pivots yet this session)")
        return
    for i, t in enumerate(trail):
        print(f"{i + 1:>3}. {t['id']}  ({t['ts']})")


def _print_claim(r: sqlite3.Row, indent: str = "") -> None:
    conf = f"{r['confidence']:.2f}" if r["confidence"] is not None else "-"
    print(f"{indent}{r['claim_id']}: {r['subj']} --{r['pred']}--> {r['obj']}")
    print(f"{indent}    {r['polarity']} | {r['evidence']} | confidence={conf}")
    keys = set(r.keys())
    if "filed_subj" in keys and "filed_obj" in keys:
        if r["filed_subj"] != r["subj"] or r["filed_obj"] != r["obj"]:
            print(f"{indent}    filed as {r['filed_subj']} --{r['pred']}--> {r['filed_obj']}")
    if r["artifact"]:
        print(f"{indent}    artifact {r['artifact']} [{r['span_start']}:{r['span_end']}]")
        if r["quote"]:
            print(f"{indent}    quote: {r['quote']}")


def _print_claims_with_belief(conn, claims: list[sqlite3.Row], min_belief, indent="  ",
                              no_rep: bool = False) -> int:
    disc = None if no_rep else discount_from_scores(score_sources(conn))
    ordered = sorted(claims, key=lambda r: (r["subj"], r["pred"], r["obj"], r["claim_id"]))
    shown = 0
    i = 0
    while i < len(ordered):
        edge = (ordered[i]["subj"], ordered[i]["pred"], ordered[i]["obj"])
        group: list[sqlite3.Row] = []
        while i < len(ordered) and (ordered[i]["subj"], ordered[i]["pred"],
                                    ordered[i]["obj"]) == edge:
            group.append(ordered[i])
            i += 1
        bel = compute_edge_belief(conn, *edge, discount=disc)
        if min_belief is not None and bel["b"] < min_belief:
            continue
        b, d, u = bel["b"], bel["d"], bel["u"]
        n_clusters = len(bel["clusters"])
        conflict_note = ""
        if any(c["has_internal_conflict"] for c in bel["clusters"]):
            conflict_note = "  [internal conflict within a cited artifact]"
        print(f"{indent}edge {edge[0]} --{edge[1]}--> {edge[2]}: "
              f"{bel['verdict']} b={b:.2f} d={d:.2f} u={u:.2f} "
              f"({len(group)} claim(s), {n_clusters} cluster(s)){conflict_note}")
        for r in group:
            _print_claim(r, indent=indent + "  ")
            shown += 1
    return shown


def cmd_show(args) -> None:
    case = require_case(resolve_case(args))
    ensure_projection(case)
    if args.min_belief is not None and not (0.0 <= args.min_belief <= 1.0):
        die("--min-belief must be in [0.0, 1.0]")
    conn = open_projection(case)
    try:
        if args.id:
            ent = _load_entity_row(conn, args.id)
            root = _resolve_id(conn, args.id)
            absorbed_hop = conn.execute(
                "SELECT survivor_id FROM aliases WHERE absorbed_id = ?",
                (args.id,)).fetchone()

            if absorbed_hop:
                filed = conn.execute(
                    "SELECT * FROM claims_filed WHERE subj = ? OR obj = ? ORDER BY claim_id",
                    (args.id, args.id)).fetchall()
                if ent:
                    print(f"entity {ent['id']}  [{ent['kind']}]  \"{ent['name']}\"")
                print(f"  MERGED into {absorbed_hop['survivor_id']} (canonical: {root}) "
                      f"[{len(filed)} claim(s) filed under this id]")
                for r in filed:
                    _print_claim(r, indent="  ")
                return

            claims = conn.execute(
                "SELECT * FROM claims WHERE subj = ? OR obj = ? ORDER BY claim_id",
                (args.id, args.id)).fetchall()
            if ent:
                print(f"entity {ent['id']}  [{ent['kind']}]  \"{ent['name']}\"")
                if ent["attrs"] and ent["attrs"] != "{}":
                    print(f"    attrs: {ent['attrs']}")
                absorbed_list = conn.execute(
                    "SELECT absorbed_id FROM aliases WHERE survivor_id = ?", (args.id,)
                ).fetchall()
                if absorbed_list:
                    names = ", ".join(r["absorbed_id"] for r in absorbed_list)
                    print(f"    aliases absorbed into this entity: {names}")
            shown = _print_claims_with_belief(conn, claims, args.min_belief, indent="",
                                               no_rep=args.no_rep)
            if not ent and not claims:
                die(f"nothing found for {args.id!r} (no entity, and it is named in no claim)")
            if claims and shown == 0:
                print(f"  (all {len(claims)} claim(s) hidden by --min-belief {args.min_belief})")
        else:
            ents = conn.execute("SELECT * FROM entities ORDER BY id").fetchall()
            claims = conn.execute("SELECT * FROM claims ORDER BY claim_id").fetchall()
            print(f"entities ({len(ents)}):")
            for r in ents:
                note = ""
                if conn.execute(
                    "SELECT 1 FROM aliases WHERE absorbed_id = ?", (r["id"],)
                ).fetchone():
                    note = f"  -> merged into {_resolve_id(conn, r['id'])}"
                print(f"  {r['id']}  [{r['kind']}]  {r['name']}{note}")
            print(f"claims ({len(claims)}):")
            shown = _print_claims_with_belief(conn, claims, args.min_belief, no_rep=args.no_rep)
            if claims and shown == 0:
                print(f"  (all {len(claims)} claim(s) hidden by --min-belief {args.min_belief})")
    finally:
        conn.close()


def _short_hash(artifact: str) -> str:
    return artifact[7:23] if artifact.startswith("sha256:") else artifact


def _temporal_where(as_of: str | None, as_learned: str | None):
    """WHERE fragments for world-time / assertion-time claim selection.

    as_of (world time T): claim holds at T ⟺ valid_from ≤ T < valid_to
    (NULL bounds = indefinite; a claim with no valid_from never matches an
    as_of filter — temporally indefinite claims cannot masquerade as dated).
    as_learned (assertion time T): claims whose journal seq is within the
    prefix asserted ≤ T; enforced against the events view, not the snapshot.
    """
    clauses, params = [], []
    if as_of is not None:
        clauses.append("(valid_from IS NOT NULL AND valid_from <= ? "
                       "AND (valid_to IS NULL OR valid_to > ?))")
        params.extend([as_of, as_of])
    if as_learned is not None:
        clauses.append("(asserted_ts IS NOT NULL AND asserted_ts <= ?)")
        params.append(as_learned)
    return " AND ".join(clauses), params


def cmd_why(args) -> None:
    case = require_case(resolve_case(args))
    ensure_projection(case)
    conn = open_projection(case)
    tw, tp = _temporal_where(getattr(args, "as_of", None),
                             getattr(args, "as_learned", None))
    try:
        q_subj = _resolve_id(conn, args.subj)
        q_obj = _resolve_id(conn, args.obj)
        scores = score_sources(conn)
        disc = None if args.no_rep else discount_from_scores(scores)
        bel = compute_edge_belief(conn, q_subj, args.pred, q_obj, discount=disc,
                                  extra_where=tw, extra_params=tp)
        sel = ("SELECT * FROM claims WHERE subj = ? AND pred = ? AND obj = ? "
               "AND superseded = 0")
        if tw:
            sel += " AND " + tw
        sel += " ORDER BY claim_id"
        rows = conn.execute(sel, [q_subj, args.pred, q_obj, *tp]).fetchall()
        active_ids = {r["claim_id"] for r in rows}
        parent = _load_parent_map(conn)
    finally:
        conn.close()

    aliases = AliasMap(parent)
    print(f"edge {q_subj} --{args.pred}--> {q_obj}")
    if q_subj != args.subj or q_obj != args.obj:
        print(f"  resolved from {args.subj} --{args.pred}--> {args.obj}")
    if not bel["clusters"]:
        print("verdict: UNKNOWN  (b=0.0000, d=0.0000, u=1.0000) — no active claims")
    else:
        print(f"verdict: [{bel['verdict']}]  "
              f"(b={bel['b']:.4f}, d={bel['d']:.4f}, u={bel['u']:.4f})")
        print(f"{len(rows)} active claim(s) in {len(bel['clusters'])} "
              f"independent cluster(s):")
        claims_by_id = {r["claim_id"]: r for r in rows}
        for cl in bel["clusters"]:
            cb, cd, cu = cl["opinion"]
            corr = ""
            if cl["artifact"] and len(cl["claim_ids"]) > 1:
                corr = (f"  [correlated: {len(cl['claim_ids'])} claims cite "
                        f"{_short_hash(cl['artifact'])}…, fused ONCE]")
            buried = ""
            if cl["has_internal_conflict"]:
                buried = (f"  [internal conflict: supports≤{cl['max_supports_conf']:.2f} "
                          f"vs refutes≤{cl['max_refutes_conf']:.2f} within one artifact]")
            rep_discount = ""
            rep_row = claims_by_id.get(cl["representative"])
            if rep_row is not None and disc is not None:
                src = rep_row["via_run"] if rep_row["via_run"] else f"claim:{rep_row['claim_id']}"
                s = scores.get(src)
                if s is not None and s.multiplier < 1.0:
                    rep_discount = f"  [reputation ×{s.multiplier:.3f}: {s.source}]"
            print(f"  cluster {_short_hash(cl['key']) if cl['artifact'] else cl['key']}: "
                  f"(b={cb:.4f}, d={cd:.4f}, u={cu:.4f})  via {cl['representative']}{corr}{buried}{rep_discount}")
            for cid in cl["claim_ids"]:
                _print_claim(claims_by_id[cid], indent="    ")

    retracted = []
    for _, line in iter_journal_lines(case):
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not (isinstance(e, dict) and e.get("op") == "claim"):
            continue
        if e.get("pred") != args.pred or e.get("claim_id") in active_ids:
            continue
        if aliases.find(e.get("subj", "")) == q_subj and aliases.find(e.get("obj", "")) == q_obj:
            retracted.append(e)
    if retracted:
        print(f"retracted (journal history; excluded from belief): {len(retracted)}")
        for e in retracted:
            conf = e.get("confidence")
            conf_s = f"{conf:.2f}" if isinstance(conf, (int, float)) else "-"
            print(f"    {e['claim_id']}: {e.get('polarity')} confidence={conf_s} "
                  f"seq={e.get('seq')}")


def cmd_timeline(args) -> None:
    """Claims touching an entity, ordered by world time (valid_from).

    The sequenced projection: temporally indefinite claims sort last and
    are flagged, superseded/retracted claims show their status inline, and
    fundamentals' point-intervals render as the metric time series.
    """
    case = require_case(resolve_case(args))
    ensure_projection(case)
    conn = open_projection(case)
    try:
        eid = _resolve_id(conn, args.entity)
        q = ("SELECT * FROM claims WHERE (subj = ? OR obj = ?)")
        params = [eid, eid]
        if args.pred:
            q += " AND pred = ?"
            params.append(args.pred)
        q += " ORDER BY valid_from IS NULL, valid_from ASC, claim_id ASC LIMIT ?"
        params.append(args.limit)
        rows = conn.execute(q, params).fetchall()
    finally:
        conn.close()

    print(f"timeline for {eid}" + (f"  [{args.pred}]" if args.pred else ""))
    if not rows:
        print("  (no claims)")
        return
    for r in rows:
        vf, vt = r["valid_from"], r["valid_to"]
        if vf is None:
            window = "[indefinite]"
        elif vt is None:
            window = f"[{vf}, ∞)"
        else:
            window = f"[{vf}, {vt})"
        flag = "  [SUPERSEDED]" if r["superseded"] else ""
        pub = f"  pub={r['pub_ts']}" if r["pub_ts"] else ""
        conf = r["confidence"]
        conf_s = f"{conf:.2f}" if isinstance(conf, (int, float)) else "-"
        arrow = "->" if r["subj"] == eid else "<-"
        other = r["obj"] if r["subj"] == eid else r["subj"]
        print(f"  {window:<28} {arrow} {r['pred']:<24} {other:<28} "
              f"conf={conf_s} ts={r['time_source'] or '-'}{pub}{flag}")


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


# --------------------------------------------------------------------------
# lint — case health checks (plan 5.3). Standalone value via `lint`; brief
# §7 calls collect_lint() and omits the section when empty. Checks mirror
# the scar tissue of case `ai`'s first ten sessions: drift, shell scars,
# missing confidence, hypothesis-over-cap, duplicate ids, twin entities.
# --------------------------------------------------------------------------


def collect_lint(case: CasePaths) -> list[dict]:
    """Return lint findings as dicts: {check, claim_id, detail}.

    Read-only over the projection; never mutates. Designed for <2KB
    rendered output on a healthy case (the common path: empty list,
    no section in brief).
    """
    ensure_projection(case)
    conn = open_projection(case)
    findings: list[dict] = []
    try:
        # 1) shell scars in claim text (the /bin/sh.70 class, Phase 1.6).
        #    Superseded claims are excluded — their corrections are live.
        for row in conn.execute(
                "SELECT claim_id, subj, pred, obj FROM claims "
                "WHERE superseded = 0 "
                "AND (subj LIKE '%/bin/sh%' OR obj LIKE '%/bin/sh%')"):
            findings.append({
                "check": "shell-scar",
                "claim_id": row["claim_id"],
                "detail": f"{row['subj']} --{row['pred']}--> {row['obj']}",
            })
        # 2) missing confidence on non-hypothesis claims (1.4 defaults are
        #    filing-time; historical nulls are treaty — flag, don't fix)
        for row in conn.execute(
                "SELECT claim_id, subj, pred, evidence FROM claims "
                "WHERE superseded = 0 AND confidence IS NULL "
                "AND evidence != 'hypothesis'"):
            findings.append({
                "check": "missing-confidence",
                "claim_id": row["claim_id"],
                "detail": f"{row['subj']} --{row['pred']}--> "
                          f"[{row['evidence']}]",
            })
        # 3) confidence-over-cap on MACHINE evidence only (1.4's 0.80 cap
        #    is the LLM ceiling; analyst-filed direct claims are exempt)
        for row in conn.execute(
                "SELECT claim_id, subj, pred, confidence FROM claims "
                "WHERE superseded = 0 AND evidence = 'inferred' "
                "AND confidence > 0.80"):
            findings.append({
                "check": "confidence-over-cap",
                "claim_id": row["claim_id"],
                "detail": f"{row['subj']} --{row['pred']}--> "
                          f"conf={row['confidence']}",
            })
        # 4) duplicate (subj, pred, obj, artifact) tuples — same content
        #    filed twice is drift, not corroboration
        for row in conn.execute(
                "SELECT subj, pred, obj, COUNT(*) AS n FROM claims "
                "WHERE superseded = 0 AND artifact IS NOT NULL "
                "GROUP BY subj, pred, obj, artifact HAVING n > 1"):
            findings.append({
                "check": "duplicate-claim",
                "claim_id": "(group)",
                "detail": f"{row['subj']} --{row['pred']}--> {row['obj']} "
                          f"x{row['n']}",
            })
        # 5) twin entities: distinct active ids normalizing to the same
        #    canonical form (the glm-5.2/glm-5-2 class; 1.7 is filing-time,
        #    the journal may predate it). Absorbed (merged-away) ids are
        #    skipped — they're history, not drift.
        absorbed = {
            row["absorbed_id"] for row in conn.execute(
                "SELECT absorbed_id FROM aliases")
        }
        seen_norm: dict[str, str] = {}
        for row in conn.execute(
                "SELECT id, kind FROM entities ORDER BY id"):
            if row["id"] in absorbed:
                continue
            # review-3: shared normalizer — lint was blind to model:-prefixes
            norm = _canon_norm(row["id"])
            if norm in seen_norm:
                findings.append({
                    "check": "twin-entity",
                    "claim_id": "(entities)",
                    "detail": f"{row['id']} ~ {seen_norm[norm]}",
                })
            else:
                seen_norm[norm] = row["id"]
        # 6) hypothesis share over 25% — drift from evidence to speculation
        total = conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0]
        hyp = conn.execute(
            "SELECT COUNT(*) FROM claims WHERE evidence = 'hypothesis'"
        ).fetchone()[0]
        if total and hyp / total > 0.25:
            findings.append({
                "check": "hypothesis-heavy",
                "claim_id": "(case)",
                "detail": f"{hyp}/{total} claims are hypotheses",
            })
    finally:
        conn.close()
    return findings


def cmd_lint(args) -> None:
    findings = collect_lint(require_case(resolve_case(args)))
    if not findings:
        print("lint: clean (0 findings)")
        return
    by_check: dict[str, list[dict]] = {}
    for f in findings:
        by_check.setdefault(f["check"], []).append(f)
    print(f"lint: {len(findings)} finding(s) across {len(by_check)} check(s)")
    for check, items in sorted(by_check.items()):
        print(f"\n  [{check}] - {len(items)}")
        for f in items[:10]:
            print(f"    {f['claim_id']}: {f['detail']}")
        if len(items) > 10:
            print(f"    ... +{len(items) - 10} more")


def cmd_brief(args) -> None:
    """brief — session-start reorientation (plan 5.1). ~2KB cap, seven
    sections, priority-ordered drops (lowest first) when over budget.
    Reuses dig's pivot families and collect_lint(); nothing new is
    computed, only aggregated cheaply."""
    case = require_case(resolve_case(args))
    ensure_projection(case)
    conn = open_projection(case)
    try:
        sections: list[tuple[str, str]] = []  # (name, body)

        # §1 case envelope (never dropped)
        ents = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        clm = conn.execute(
            "SELECT COUNT(*) FROM claims WHERE superseded=0").fetchone()[0]
        art = conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
        max_seq = conn.execute(
            "SELECT MAX(filed_seq) FROM claims_filed").fetchone()[0] or 0
        sections.append(("envelope",
                         f"entities={ents} active-claims={clm} "
                         f"artifacts={art} last-claim-seq={max_seq}"))

        # §2 recent journal events (tail 5, from the journal itself)
        body = ""
        try:
            jp = case.journal
            if jp.exists():
                with shared_lock(case):
                    size = jp.stat().st_size
                    window = min(size, 16384)
                    with jp.open("rb") as f:
                        if size > window:
                            f.seek(size - window)
                        data = f.read()
                lines = [l for l in data.decode(
                    "utf-8", errors="replace").splitlines() if l.strip()]
                tail = []
                for l in lines[-5:]:
                    try:
                        ev = json.loads(l)
                        d = ev.get("data", ev)
                        extra = ""
                        for k in ("uri", "subj", "id", "claim_id",
                                  "transform", "absorbed_id", "target_id"):
                            if d.get(k):
                                extra = f" {str(d[k])[:48]}"
                                break
                        tail.append(f"  {ev.get('seq','?'):>4} "
                                    f"{ev.get('op','?')}{extra}")
                    except Exception:
                        pass
                body = "\n".join(tail) or "  (empty)"
        except Exception:
            body = "  (unavailable)"
        sections.append(("recent", body))

        # §3 open hypotheses
        hyps = conn.execute(
            "SELECT claim_id, subj, pred, obj, confidence FROM claims "
            "WHERE evidence='hypothesis' AND superseded=0").fetchall()
        if hyps:
            body = "\n".join(
                f"  {h['claim_id'][:14]} {h['subj']} --{h['pred']}--> "
                f"{str(h['obj'])[:60]}"
                + (f" [{h['confidence']}]" if h["confidence"] else "")
                for h in hyps[:5])
            if len(hyps) > 5:
                body += f"\n  ... +{len(hyps) - 5} more"
        else:
            body = "  (none open)"
        sections.append(("hypotheses", body))

        # §4 catalysts: dated expectations still in the future
        now = utc_now()
        future = conn.execute(
            "SELECT subj, pred, obj, valid_from FROM claims "
            "WHERE superseded=0 AND valid_from IS NOT NULL "
            "AND valid_from > ? ORDER BY valid_from LIMIT 5",
            (now,)).fetchall()
        body = "\n".join(
            f"  {f['valid_from'][:10]} {f['subj']} --{f['pred']}--> "
            f"{str(f['obj'])[:48]}" for f in future) or "  (none dated)"
        sections.append(("catalysts", body))

        # §5 pivot signals (top rows from dig's families)
        sig: list[str] = []
        absorbed = {r[0] for r in conn.execute(
            "SELECT absorbed_id FROM aliases")}
        degree: dict[str, int] = {}
        for row in conn.execute(
                "SELECT subj, obj FROM claims WHERE superseded=0"):
            for end in (row["subj"], row["obj"]):
                if end:
                    degree[end] = degree.get(end, 0) + 1
        unexp = sorted(
            ((e["id"], degree.get(e["id"], 0))
             for e in conn.execute("SELECT id FROM entities")
             if e["id"] not in absorbed and degree.get(e["id"], 0) <= 1),
            key=lambda x: x[1])[:3]
        for eid, d in unexp:
            sig.append(f"  unexpanded: {eid} (degree {d})")
        passive = conn.execute(
            "SELECT c.obj AS id, COUNT(*) AS n FROM claims c "
            "WHERE c.superseded=0 GROUP BY c.obj HAVING n >= 3 "
            "AND c.obj NOT IN (SELECT DISTINCT subj FROM claims "
            "WHERE superseded=0) LIMIT 3").fetchall()
        for r in passive:
            oid = str(r['id'])
            if oid.isdigit():
                continue  # numeric literals are attribute values, not entities
            sig.append(f"  passive: {oid} (cited {r['n']}x, "
                       f"never subject)")
        sections.append(("signals", "\n".join(sig) or "  (none)"))

        # §6 last transform run (scan journal tail; cheap)
        body = "  (none recent)"
        try:
            for l in reversed(lines[-30:] if lines else []):
                ev = json.loads(l)
                if ev.get("op") == "transform_run":
                    d = ev
                    body = (f"  seq {ev.get('seq','?')} "
                            f"{d.get('transform','?')} "
                            f"accepted={d.get('accepted','?')} "
                            f"rejected={d.get('rejected','?')}")
                    break
        except Exception:
            pass
        sections.append(("transform", body))

        # §7 lint (omitted entirely when clean)
        findings = collect_lint(case)
        if findings:
            body = "\n".join(
                f"  [{f['check']}] {f['claim_id']}: "
                f"{f['detail'][:60]}" for f in findings[:6])
            if len(findings) > 6:
                body += f"\n  ... +{len(findings) - 6} more"
            sections.append(("lint", body))
    finally:
        conn.close()

    def _cap(body: str, limit: int = 700) -> str:
        return body if len(body) <= limit else body[:limit - 3] + "..."

    blocks = {name: _cap(body) for name, body in sections}
    # render with 2KB budget; drop lowest-value sections first
    PRIORITY = ["envelope", "recent", "hypotheses", "catalysts",
                "signals", "transform", "lint"]
    DROP_ORDER = ["lint", "transform", "signals", "catalysts",
                  "recent", "hypotheses"]
    budget = 2048
    total = sum(len(b) for b in blocks.values())
    for name in DROP_ORDER:
        if total <= budget:
            break
        total -= len(blocks.pop(name, ""))
    out = [f"brief — {blocks.get('envelope', '')}"]
    for name in PRIORITY[1:]:
        if name in blocks and blocks[name]:
            out.append(f"[{name}]")
            out.append(blocks[name])
    print("\n".join(out))


def cmd_verify(args) -> None:
    case = require_case(resolve_case(args))
    errors, last_seq, last_hash = verify_chain(case)

    referenced: set[str] = set()
    claim_events: list[dict] = []
    merge_cite_events: list[dict] = []
    for _, line in iter_journal_lines(case):
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(ev, dict):
            if ev.get("op") == "artifact" and isinstance(ev.get("hash"), str):
                referenced.add(ev["hash"])
            if ev.get("op") == "transform_run" and isinstance(ev.get("artifact_hash"), str):
                referenced.add(ev["artifact_hash"])
            if ev.get("op") == "claim":
                if isinstance(ev.get("artifact"), str):
                    referenced.add(ev["artifact"])
                claim_events.append(ev)
            if ev.get("op") == "merge" and isinstance(ev.get("artifact"), str):
                referenced.add(ev["artifact"])
                merge_cite_events.append(ev)

    quick = getattr(args, "quick", False)
    _memo: dict[str, tuple[int, int]] = {}
    sidecar = case.root / ".verify-checkpoint.json"
    if quick and sidecar.exists():
        try:
            _memo = {k: tuple(v) for k, v in json.loads(sidecar.read_text()).items()}
        except Exception:
            _memo = {}
    checked = skipped = 0
    for hs in sorted(referenced):
        if not HASH_RE.match(hs):
            errors.append(f"journal references malformed artifact hash {hs!r}")
            continue
        p = case.object_path(hs)
        if not p.exists():
            errors.append(f"missing evidence object {hs} (referenced by journal, absent from CAS)")
            continue
        st = p.stat()
        stamp = (st.st_size, st.st_mtime_ns)
        if quick and _memo.get(hs) == stamp:
            skipped += 1
            continue
        actual = _hash_file(p)
        if actual != hs:
            errors.append(f"evidence object {hs} FAILED re-hash check (got {actual}); CAS is corrupted")
            continue
        checked += 1
        _memo[hs] = stamp
    if quick:
        try:
            sidecar.write_text(json.dumps(_memo))
        except OSError:
            pass

    # 6.1 remediation (review-3): claims are grouped by cited artifact and
    # each artifact is read + hashed ONCE per verify run. The pre-remediation
    # loop called read_bytes (full CAS read + SHA-256 re-hash) per citing
    # claim — O(claims × artifact-size) I/O, dozens of reads per heavily
    # cited artifact at 10× scale, and the reason --quick bought almost
    # nothing (the quote loop re-paid the hash the checkpoint skipped).
    art_buffers: dict[str, bytes] = {}

    def _buf(art: str) -> bytes | None:
        if art in art_buffers:
            return art_buffers[art]
        p = case.object_path(art)
        if not p.exists():
            return None
        data = read_bytes(case, art)
        art_buffers[art] = data
        return data

    quote_checked = 0
    for ev in claim_events:
        art = ev.get("artifact")
        if not art or not HASH_RE.match(art):
            continue
        data = _buf(art)
        if data is None:
            continue
        try:
            verify_quote_span(data, ev["span_start"], ev["span_end"], ev["quote"])
            quote_checked += 1
        except GiError as e:
            errors.append(
                f"claim {ev.get('claim_id')}: quote/span verification FAILED: {e}")

    merge_quote_checked = 0
    for ev in merge_cite_events:
        art = ev.get("artifact")
        if not art or not HASH_RE.match(art):
            continue
        data = _buf(art)
        if data is None:
            continue
        try:
            verify_quote_span(data, ev["span_start"], ev["span_end"], ev["quote"])
            merge_quote_checked += 1
        except GiError as e:
            errors.append(
                f"merge {ev.get('absorbed')} into {ev.get('survivor')} (seq {ev.get('seq')}): "
                f"citation quote/span verification FAILED: {e}")

    if errors:
        print("VERIFY FAILED:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        raise SystemExit(1)
    print(f"OK: {last_seq} journal event(s), chain head {last_hash}")
    print(f"OK: {checked} referenced evidence object(s) present and re-hash-verified"
          + (f" (+{skipped} skipped via --quick checkpoint, size+mtime unchanged)" if skipped else ""))
    print(f"OK: {quote_checked} claim quote(s) verified against cited artifacts")
    print(f"OK: {merge_quote_checked} merge citation quote(s) verified against cited artifacts")


def _truncate_torn_tail(case: CasePaths) -> int:
    raw = case.journal.read_bytes()
    if raw.endswith(b"\n"):
        return 0
    last_nl = raw.rfind(b"\n")
    if last_nl == -1:
        removed = len(raw)
        case.journal.write_bytes(b"")
        return removed
    truncated = raw[:last_nl + 1]
    removed = len(raw) - len(truncated)
    case.journal.write_bytes(truncated)
    return removed


def cmd_rebuild(args) -> None:
    case = require_case(resolve_case(args))

    if args.repair:
        removed = _truncate_torn_tail(case)
        if removed > 0:
            print(f"repair: truncated {removed} byte(s) of torn tail from journal", file=sys.stderr)
        else:
            print("repair: journal already ends with a newline; no torn tail detected")

    seq, h = full_rebuild(case)
    print(f"rebuilt projection through seq {seq} (journal tail {h})")


def _render_event_pretty(ev: dict) -> str:
    op = ev.get("op")
    seq, ts = ev.get("seq"), ev.get("ts", "")
    if op == "merge":
        cite = f" citing {ev['artifact']}" if ev.get("artifact") else ""
        return (f"[{seq}] {ts} MERGE: {ev.get('absorbed')} into {ev.get('survivor')} "
                f"| reason: {ev.get('reason')}{cite}")
    if op == "unmerge":
        return f"[{seq}] {ts} UNMERGE: {ev.get('entity_id')} | reason: {ev.get('reason')}"
    if op == "claim":
        conf = ev.get("confidence")
        conf_s = f" conf={conf}" if conf is not None else ""
        art = f" citing {ev['artifact']}" if ev.get("artifact") else ""
        via = f" via_run={ev['via_run']}" if ev.get("via_run") else ""
        return (f"[{seq}] {ts} CLAIM: {ev.get('claim_id')} "
                f"({ev.get('subj')} --{ev.get('pred')}--> {ev.get('obj')}) "
                f"[{ev.get('polarity')}]{conf_s}{art}{via}")
    if op == "retract":
        scored = " [SCORED]" if ev.get("scored") else ""
        via = f" (run {ev['via_run']})" if ev.get("via_run") else ""
        return f"[{seq}] {ts} RETRACT: {ev.get('claim_id')}{via}{scored} | reason: {ev.get('reason')}"
    if op == "entity":
        return f"[{seq}] {ts} ENTITY: {ev.get('id')} [{ev.get('kind')}] \"{ev.get('name')}\""
    if op == "artifact":
        return f"[{seq}] {ts} ARTIFACT: {ev.get('hash')} ({ev.get('size')} bytes)"
    if op == "case_init":
        return f"[{seq}] {ts} INIT: format={ev.get('format')}"
    if op == "transform_run":
        return (f"[{seq}] {ts} TRANSFORM_RUN: {ev.get('transform')} on {ev.get('uri')} "
                f"| run_id={ev.get('run_id')} accepted={ev.get('accepted')} rejected={ev.get('rejected')}")
    return f"[{seq}] {ts} {op}: {canonical_json(ev).decode('utf-8')}"


def cmd_log(args) -> None:
    case = require_case(resolve_case(args))
    since = max(1, args.since)
    shown = 0
    for _, line in iter_journal_lines(case):
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
            seq = ev.get("seq")
        except json.JSONDecodeError:
            seq = None
            ev = None
        if isinstance(seq, int) and seq < since:
            continue
        if getattr(args, "pretty", False) and isinstance(ev, dict):
            print(_render_event_pretty(ev))
        else:
            print(line)
        shown += 1
    if shown == 0:
        print(f"(no events with seq >= {since})", file=sys.stderr)


def cmd_fetch(args) -> None:
    case = require_case(resolve_case(args))
    hs, size, final_url, content_type = fetch_url(case, args.url, max_bytes=args.max_bytes)
    ev = transact(case, "artifact", hash=hs, uri=final_url, size=size, content_type=content_type)
    print(f"fetched {final_url}")
    print(f"  stored as {hs} ({size} bytes, {content_type or 'unknown type'}), recorded at seq {ev['seq']}")
    # Phase 4.2: HTML fetches gain a visible-text companion (html-visible-v1),
    # journaled as a derived artifact. Non-HTML is a no-op. --no-companion
    # opts out (diagnostics on unusual pages).
    if not getattr(args, "no_companion", False):
        chs = maybe_companion(case, hs, final_url, content_type)
        if chs:
            print(f"  companion {chs} (html-visible-v1 visible text), available for quoting")


def cmd_ingest(args) -> None:
    case = require_case(resolve_case(args))
    src = Path(args.file).expanduser().resolve()
    hs, size = store_file(case, src)
    ev = transact(case, "artifact", hash=hs, uri=src.as_uri(), size=size)
    print(f"stored {src} as {hs} ({size} bytes), recorded at seq {ev['seq']}")


# --------------------------------------------------------------------------
# Transforms (Slice 4)
# --------------------------------------------------------------------------

def cmd_find_quote(args) -> None:
    """Council r6: read-only diagnostic. Locates a quote in a stored artifact
    and prints the exact spans the claim gate accepts, computed through the
    gate's own decode and normalization (a different coordinate system would
    promise spans the gate then rejects -- the original bug). Journals nothing."""
    case = require_case(resolve_case(args))
    if not HASH_RE.match(args.artifact):
        die(f"--artifact must be 'sha256:<64hex>' (got {args.artifact!r})")
    data = read_bytes(case, args.artifact)
    text = data.decode("utf-8", errors="replace")
    try:
        hits = quote_occurrences(data, args.quote, max_hits=args.max_hits)
    except GiError as e:
        die(str(e))
    print(f"artifact {args.artifact}: {len(text)} utf-8 chars "
          f"(errors=replace, newlines preserved)")
    if not hits:
        die("quote not found under the gate's decode/normalization -- no span "
            "will be accepted; re-quote from the stored bytes")
    for n, (a, b) in enumerate(hits, 1):
        lo, hi = max(0, a - 100), min(len(text), b + 100)
        ctx = text[lo:hi].replace("\n", "\\n")
        print(f"match {n}: --span {a} {b}   (len {b - a})")
        print(f"  …{ctx}…")
    if len(hits) > 1:
        print("note: ambiguous quote -- pass an explicit --span above; "
              "a claim without --span will refuse this quote", file=sys.stderr)


def _resolve_transform_path(name: str) -> Path:
    if not name or "/" in name or "\\" in name or ".." in name or "\0" in name:
        die(f"invalid transform name {name!r}: path traversal characters are forbidden")
    base_dir = Path(__file__).resolve().parent / "transforms"
    script = (base_dir / f"{name}.py").resolve()
    if not script.is_file():
        die(f"transform {name!r} not found (expected at {script})")
    return script


def cmd_run(args) -> None:
    case = require_case(resolve_case(args))
    ensure_projection(case)

    script_path = _resolve_transform_path(args.transform)

    parsed_args = {}
    for pair in (args.arg or []):
        if "=" not in pair:
            die(f"--arg expects KEY=VALUE (got {pair!r})")
        k, v = pair.split("=", 1)
        if not k:
            die("--arg key must be non-empty")
        parsed_args[k] = v

    # 1. Host fetches the artifact into CAS
    hs, size, final_uri, content_type = fetch_url(case, args.uri)
    transact(case, "artifact", hash=hs, uri=final_uri, size=size, content_type=content_type)
    artifact_path = case.object_path(hs)
    artifact_bytes = read_bytes(case, hs)
    artifact_text = artifact_bytes.decode("utf-8", errors="replace")

    # 4.4 (plan-review 2026-08-16): when the artifact is HTML, create the
    # html-visible-v1 companion BEFORE the transform_run (frozen event order
    # preserved: artifact → [companion] → transform_run → claims), then pass
    # the companion's clean prose in the payload. The LLM reader — Phase 4's
    # largest text consumer — reads prose, not markup. --raw-text opts out.
    companion_hash = None
    companion_text = None
    if not getattr(args, "raw_text", False):
        companion_hash = maybe_companion(case, hs, final_uri, content_type)
        if companion_hash:
            companion_text = read_bytes(case, companion_hash).decode("utf-8", errors="replace")

    timeout_s = args.timeout if args.timeout is not None else TRANSFORM_TIMEOUT_S

    # 2. Prepare payload for transform (3.3: artifact_text capped at 8MB;
    # larger artifacts are always available to the transform at
    # artifact_path — the cap bounds the pipe + transform memory, and Phase
    # 4 companions (compact visible text) make the cap a non-issue in practice)
    # Review-3 fix: the old slice cut CHARACTERS against a BYTE budget (a
    # 4-byte-heavy text could pass ~4x the cap), and companion text bypassed
    # the cap entirely. Both text paths now cap on measured bytes.
    payload_text = companion_text if companion_text is not None else artifact_text
    text_truncated = False
    if payload_text is not None:
        encoded = payload_text.encode("utf-8", errors="replace")
        if len(encoded) > TRANSFORM_PAYLOAD_MAX_BYTES:
            # truncate on the byte budget, then cut back to a clean UTF-8
            # boundary so the decoded text isn't split mid-codepoint
            cut = encoded[:TRANSFORM_PAYLOAD_MAX_BYTES]
            payload_text = cut.decode("utf-8", errors="ignore")
            text_truncated = True
    payload = canonical_json({
        "case_dir": str(case.root),
        "artifact_hash": hs,
        "artifact_path": str(artifact_path),
        "artifact_text": payload_text,
        "artifact_text_truncated": text_truncated,
        "companion_hash": companion_hash,
        "uri": final_uri,
        "args": parsed_args,
    })

    # 3. Spawn transform as prisoner
    try:
        proc = subprocess.run(
            [sys.executable, str(script_path)],
            input=payload,
            capture_output=True,
            timeout=timeout_s,
            cwd=str(case.root),
            check=False,
        )
    except subprocess.TimeoutExpired:
        die(f"transform {args.transform} timed out after {timeout_s}s")

    if proc.returncode != 0:
        stderr_tail = (proc.stderr.decode("utf-8", errors="replace"))[-2000:]
        # Transforms log operational errors (e.g. model endpoint failures) to
        # STDOUT as {"op":"log"} records; surface those too or the failure is
        # silent. Pull out the message field of any log records found.
        stdout_logs = []
        for line in proc.stdout.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if isinstance(rec, dict) and rec.get("op") == "log":
                    stdout_logs.append(f"[{rec.get('level', 'log')}] {rec.get('message', '')}")
            except json.JSONDecodeError:
                continue
        detail = "\n".join(filter(None, ["\n".join(stdout_logs), stderr_tail]))
        die(f"transform {args.transform} failed with exit code {proc.returncode}:\n{detail}")

    # 4. Parse transform stdout
    stdout_lines = proc.stdout.decode("utf-8", errors="replace").splitlines()
    if proc.stderr:
        # Pass non-fatal transform stderr through
        print(proc.stderr.decode("utf-8", errors="replace"), file=sys.stderr, end="")

    raw_items: list[dict] = []
    for lineno, line in enumerate(stdout_lines, 1):
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
            if isinstance(item, dict):
                raw_items.append(item)
            else:
                print(f"transform line {lineno}: not a JSON object (skipped)", file=sys.stderr)
        except json.JSONDecodeError as e:
            print(f"transform line {lineno}: invalid JSON ({e}) (skipped)", file=sys.stderr)

    run_id = sha256_hex(canonical_json({
        "transform": args.transform,
        "uri": final_uri,
        "ts": utc_now(),
        "args": parsed_args,
    }))[:16]

    accepted_entities: list[dict] = []
    rejected_reasons: list[str] = []
    staged_claims: list[dict] = []
    valid_lines_count = 0

    # 5. Process entities first
    conn = open_projection(case)
    try:
        for item in raw_items:
            op = item.get("op")
            if op == "log":
                valid_lines_count += 1
                lvl = item.get("level", "info")
                msg = item.get("message", "")
                print(f"[{args.transform}:{lvl}] {msg}", file=sys.stderr)
            elif op == "entity":
                eid = item.get("id")
                name = item.get("name")
                kind = item.get("kind") or "entity"
                attrs = item.get("attrs") or {}
                if not eid or not isinstance(eid, str) or not name or not isinstance(name, str):
                    rejected_reasons.append(f"entity line malformed (id={eid!r}, name={name!r})")
                    continue
                existing = _load_entity_row(conn, eid)
                if existing:
                    ex_kind = existing["kind"] or "entity"
                    ex_name = existing["name"] or ""
                    if ex_kind != kind or ex_name != name:
                        rejected_reasons.append(
                            f"entity {eid} mismatch (existing {ex_kind}:{ex_name!r} vs emitted {kind}:{name!r})")
                        print(f"warning: entity {eid} kind/name mismatch against case; skipped", file=sys.stderr)
                        continue
                accepted_entities.append(dict(id=eid, name=name, kind=kind, attrs=attrs))
                valid_lines_count += 1
            elif op == "claim":
                staged_claims.append(item)
            else:
                rejected_reasons.append(f"unknown op {op!r}")
    finally:
        conn.close()

    # 2.5 (plan-review 2026-08-16): entities are no longer journaled in their
    # own transaction — they join transform_run + claims in ONE final batch
    # (journal writes: 3 -> 2: the artifact fetch + this batch). The claim
    # gate validates against the projection UNION the ids about to be
    # registered, so deferral is safe; entities precede their citing claims
    # in append order.
    registered_eids = {ent["id"] for ent in accepted_entities}

    # Refresh projection view of entities (1.8: also load the alias map so
    # absorbed ids — glm-5.2 vs glm-5-2, model:-prefixed twins — validate
    # as their survivor instead of being rejected or, worse, registered anew)
    conn = open_projection(case)
    try:
        all_entities = {r["id"] for r in conn.execute("SELECT id FROM entities")}
        all_entities.update(registered_eids)
        alias_map: dict[str, str] = {
            row[0]: row[1]
            for row in conn.execute("SELECT absorbed_id, survivor_id FROM aliases")
        }
    finally:
        conn.close()

    def _alias_resolve(x: str) -> str:
        seen: set[str] = set()
        while x in alias_map and x not in seen:
            seen.add(x)
            x = alias_map[x]
        return x

    # 6. Gate claims
    accepted_claims: list[dict] = []
    seen_claim_ids: set[str] = set()
    # 4.4 remediation (review-3): the gate must verify spans in the SAME
    # coordinate system the transform was given. When a companion exists the
    # payload carried companion prose, so spans are companion offsets against
    # companion bytes, and the claim must cite the companion hash (one-hash-
    # one-claim holds: the quote lives in the companion). The pre-remediation
    # gate verified against raw HTML unconditionally — every honest companion
    # citation was rejected, and 4.4 was inert plumbing.
    cite_bytes = companion_text.encode("utf-8", errors="replace") if companion_text is not None else artifact_bytes
    cite_text_len = len(companion_text) if companion_text is not None else len(artifact_text)
    cite_hash = companion_hash if companion_hash is not None else hs
    text_len = cite_text_len

    for item in staged_claims:
        subj = item.get("subj")
        pred = item.get("pred")
        obj = item.get("obj")
        if not subj or not isinstance(subj, str):
            rejected_reasons.append(f"claim missing/invalid subj: {subj!r}")
            continue
        if not pred or not isinstance(pred, str):
            rejected_reasons.append(f"claim missing/invalid pred: {pred!r}")
            continue
        if not obj or not isinstance(obj, str):
            rejected_reasons.append(f"claim missing/invalid obj: {obj!r}")
            continue

        # 1.8: resolve through the merge chain BEFORE validation — a transform
        # citing an absorbed id is citing the survivor's identity, and the
        # claim should be journaled against the canonical id.
        subj = _alias_resolve(subj)
        obj = _alias_resolve(obj)

        if subj not in all_entities:
            rejected_reasons.append(f"subj {subj!r} is not a registered entity")
            continue
        if obj not in all_entities:
            rejected_reasons.append(f"obj {obj!r} is not a registered entity")
            continue

        polarity = item.get("polarity", "supports")
        if polarity not in POLARITIES:
            rejected_reasons.append(f"invalid polarity {polarity!r}")
            continue

        # Untrusted defaults: omitted evidence is 'inferred'
        evidence = item.get("evidence", "inferred")
        if evidence not in EVIDENCE_KINDS:
            rejected_reasons.append(f"invalid evidence kind {evidence!r}")
            continue
        # Council r4 fix (honor system): transforms may only mint the evidence
        # kinds their class permits — llm/wiki-style readers mint 'inferred'
        # (quote-gated against a fetched artifact), the dig transform mints
        # 'hypothesis'. No transform may ever emit 'direct': that class is
        # reserved for the analyst's hand-filed claims citing stored evidence.
        # Council r6 fix: .get("default") returned None (no such key), letting
        # unlisted transforms skip the restriction entirely — belief
        # laundering through the back door. Unlisted now defaults to the
        # restrictive set the comment always promised.
        allowed_kinds = TRANSFORM_EVIDENCE_POLICY.get(args.transform,
                                                      ("inferred", "hypothesis"))
        if allowed_kinds is not None and evidence not in allowed_kinds:
            rejected_reasons.append(
                f"transform {args.transform!r} may not emit evidence={evidence!r} "
                f"(allowed: {', '.join(allowed_kinds)})")
            continue

        conf = item.get("confidence")
        if conf is not None:
            if not isinstance(conf, (int, float)) or isinstance(conf, bool) or not (0.0 <= float(conf) <= 1.0):
                rejected_reasons.append(f"invalid confidence: {conf!r}")
                continue
            conf = float(conf)
            # 1.4 (plan-review 2026-08-16): LLM-inferred claims may never
            # exceed the transform cap — previously an omitted confidence
            # fused at 1.0, OVER the documented 0.80 LLM ceiling: a live
            # laundering channel for machine certainty.
            if conf > TRANSFORM_MAX_CONFIDENCE:
                rejected_reasons.append(
                    f"transform confidence {conf} exceeds the LLM cap "
                    f"{TRANSFORM_MAX_CONFIDENCE} (transforms are inferred, "
                    "not witnessed)")
                continue
        else:
            conf = TRANSFORM_DEFAULT_CONFIDENCE

        ss = item.get("span_start")
        se = item.get("span_end")
        quote = item.get("quote")

        if not (isinstance(ss, int) and isinstance(se, int) and not isinstance(ss, bool) and not isinstance(se, bool)):
            rejected_reasons.append(f"invalid span types: ({ss!r}, {se!r})")
            continue
        if not (0 <= ss < se <= text_len):
            rejected_reasons.append(f"span [{ss}, {se}) out of bounds [0, {text_len})")
            continue
        if not isinstance(quote, str) or not quote.strip():
            rejected_reasons.append("quote is empty or missing")
            continue

        try:
            verify_quote_span(cite_bytes, ss, se, quote)
        except GiError as e:
            rejected_reasons.append(f"quote verification failed: {e}")
            continue

        claim_fields = dict(
            subj=subj, pred=pred, obj=obj,
            polarity=polarity, evidence=evidence,
            confidence=conf, artifact=cite_hash,
            span_start=ss, span_end=se, quote=quote,
            via_run=run_id,
        )
        claim_id = item.get("id") or _derive_claim_id(claim_fields)
        # 1.2a (plan-review 2026-08-16): within-run claim_id dedupe. cmd_run
        # previously lacked the `seen` set _cmd_claim_batch has — two
        # same-content claims in one run journaled two events, the second
        # silently dropped on replay (the INSERT OR IGNORE divergence's
        # source). Duplicates are rejected here, never journaled.
        if claim_id in seen_claim_ids:
            rejected_reasons.append(
                f"duplicate claim_id {claim_id} within run (already accepted "
                "from an earlier line)")
            continue
        seen_claim_ids.add(claim_id)
        claim_fields["claim_id"] = claim_id
        accepted_claims.append(claim_fields)
        valid_lines_count += 1

    if valid_lines_count == 0 and len(rejected_reasons) > 0:
        die(f"no valid output from transform {args.transform}:\n  - " + "\n  - ".join(rejected_reasons))

    # 7. Record transform_run provenance event + transact entities and
    # accepted claims as one batched transaction: N+2 events appended under
    # one lock, one rebuild. Entities precede their citing claims in append
    # order (2.5: single transaction for the run's entire journal write).
    entity_events = [
        ("entity", dict(id=ent["id"], kind=ent["kind"], name=ent["name"], attrs=ent["attrs"]))
        for ent in accepted_entities
    ]
    claim_events = [("claim", cf) for cf in accepted_claims]
    transact_batch(case, entity_events + [
        ("transform_run", dict(
            run_id=run_id,
            transform=args.transform,
            uri=final_uri,
            artifact_hash=hs,
            companion_hash=companion_hash,
            accepted=len(accepted_claims),
            rejected=len(rejected_reasons),
            args=parsed_args,
        ))
    ] + claim_events)

    print(f"transform {args.transform} finished (run_id: {run_id}):")
    print(f"  {len(accepted_entities)} entity(ies) registered")
    print(f"  {len(accepted_claims)} claim(s) accepted and journaled")
    if rejected_reasons:
        print(f"  {len(rejected_reasons)} item(s) rejected:")
        for r in rejected_reasons:
            print(f"    - {r}")


# --------------------------------------------------------------------------
# Slice 8 (dig): the exploratory pivot — see references/DIG.md
# --------------------------------------------------------------------------

def _prospect_status(pid: str, accepts: set, tests: set, withdraws: set,
                     closes: dict[str, str] | None = None) -> str:
    # Slice 8b: a journaled verdict supersedes everything (close is refused
    # for withdrawn prospects, so the two can never conflict).
    if closes and pid in closes:
        return closes[pid]
    if pid in withdraws:
        return "withdrawn"
    if pid in accepts:
        return "tested" if pid in tests else "accepted"
    return "unreviewed"


def _dig_map(case: CasePaths) -> None:
    conn = open_projection(case)
    try:
        prospects = conn.execute(
            "SELECT * FROM prospects ORDER BY seq").fetchall()
        accepts = {r["prospect_id"] for r in conn.execute(
            "SELECT prospect_id FROM dig_accepts")}
        tests = {r["prospect_id"] for r in conn.execute(
            "SELECT DISTINCT prospect_id FROM dig_tests")}
        withdraws = {r["prospect_id"] for r in conn.execute(
            "SELECT prospect_id FROM dig_withdraws")}
        closes = {r["prospect_id"]: r["verdict"] for r in conn.execute(
            "SELECT prospect_id, verdict FROM dig_closes")}
        hd = conn.execute(
            "SELECT COUNT(*) FROM claims WHERE evidence='hypothesis'").fetchone()[0]
        cited = conn.execute(
            "SELECT COUNT(*) FROM claims WHERE artifact IS NOT NULL "
            "AND evidence != 'hypothesis'").fetchone()[0]
    finally:
        conn.close()

    statuses = [_prospect_status(p["prospect_id"], accepts, tests,
                                 withdraws, closes) for p in prospects]
    # Council r3 fix: the map's quota arithmetic must MATCH the gate's —
    # open = not withdrawn, not closed (tested stay in quota until closed).
    open_n = sum(1 for s in statuses if s not in ("withdrawn",)
                 and s not in ("corroborated", "killed", "expired"))
    tested_open = sum(1 for s in statuses if s == "tested")
    print("dig — the frontier map")
    print(f"  hypotheses: {hd}  cited evidence claims: {cited}  "
          f"(H:D 1:{cited // max(hd, 1)})  open prospects: {open_n}/"
          f"{max(cited // 3, 0)}")
    if tested_open:
        print(f"  {tested_open} tested (legacy state machine — informational only)")
    if not prospects:
        print("  no prospects yet")
        return
    for p, status in zip(prospects, statuses):
        anchors = json.loads(p["anchors"])
        kc = json.loads(p["kill_criterion"])
        print(f"  {p['prospect_id']}  [{status}]  "
              f"{p['subj']} --{p['pred']}--> {p['obj']}")
        print(f"    thesis: {p['thesis']}")
        print(f"    anchors: {', '.join(anchors)}")
        print(f"    kill if: {kc['observation']} ({kc['source_class']})")
        targets = json.loads(p["fetch_targets"] or "[]")
        if targets:
            print(f"    targets: {', '.join(targets)}")
        # Near-duplicate advisory (council r2 #8): exact-triple dedupe
        # cannot see rephrasings — surface shared-endpoint siblings for
        # the analyst's eye. Advisory, never a gate (Heretic's concession).
        near = [q["prospect_id"] for q in prospects
                if q["prospect_id"] != p["prospect_id"]
                and {q["subj"], q["obj"]} & {p["subj"], p["obj"]}]
        if near:
            print(f"    ≈ shares an endpoint with: {', '.join(near)}")


def cmd_dig(args) -> None:
    # 2026-08-15 redesign: dig is READ-ONLY graph synthesis. The old
    # prospect→accept→test→close state machine (LLM-in-a-transform, quotas,
    # H:D caps) was overengineered — the analyst invokes `dig`, reads the
    # synthesis, and brainstorms dig sites with the innovate skill; the
    # fetch/claim loop expands the graph. Write ops retired; the six
    # `prospect` events already in journals replay via the handlers in
    # apply_event (the journal is the truth; history replays identically).
    case = require_case(resolve_case(args))
    ensure_projection(case)
    conn = open_projection(case)
    try:
        _dig_map(case)
        print()
        # Synthesis digest: entities + cited claims + open questions,
        # formatted for feeding to the innovate skill.
        entities = conn.execute(
            "SELECT id, kind, name FROM entities ORDER BY kind, id").fetchall()
        claims = conn.execute(
            "SELECT claim_id, subj, pred, obj, polarity, evidence, confidence, quote "
            "FROM claims WHERE artifact IS NOT NULL AND evidence != 'hypothesis' "
            "ORDER BY subj, pred, obj").fetchall()
        degree = {}
        subj_of, obj_of = {}, {}
        for row in conn.execute(
                "SELECT subj, obj FROM claims WHERE artifact IS NOT NULL"):
            s, o = row["subj"], row["obj"]
            degree[s] = degree.get(s, 0) + 1
            degree[o] = degree.get(o, 0) + 1
            subj_of.setdefault(s, set()).add(o)
            obj_of.setdefault(o, set()).add(s)
        hyps = conn.execute(
            "SELECT claim_id, subj, pred, obj, confidence FROM claims "
            "WHERE evidence = 'hypothesis'").fetchall()
        models_by_lab = {}
        for row in conn.execute(
                "SELECT subj, obj FROM claims WHERE pred = 'released-by'"):
            models_by_lab.setdefault(row["obj"], []).append(row["subj"])
        absorbed = {r[0] for r in conn.execute("SELECT absorbed_id FROM aliases")}
    finally:
        conn.close()
    print(f"synthesis — {len(entities)} entities, {len(claims)} cited claims")
    for e in entities:
        label = f" {e['name']}" if e['name'] and e['name'] != e['id'] else ""
        print(f"  {e['kind']:14s} {e['id']}{label}")
    print()
    for c in claims:
        conf = f" conf={c['confidence']}" if c['confidence'] is not None else ""
        print(f"  {c['claim_id']}  {c['subj']} --{c['pred']}--> {c['obj']} "
              f"[{c['evidence']}{conf}]")
        print(f"      \"{c['quote']}\"")
    print()
    # Pivot board (Maltego-style): structural signals for where to dig next.
    # The tool reports facts; the ANALYST judges which pivot matters — no
    # hidden LLM, no scoring theater. Signals:
    #   1. unexpanded nodes (degree <= 1)
    #   2. open hypotheses (evidence='hypothesis', active)
    #   3. disconnected clusters (labs sharing models but no cross-claims)
    #   4. asymmetries (entities cited as object many times, subject never)
    print("pivot board — structural signals (judgment is the analyst's)")
    unexpanded = [(e, degree.get(e["id"], 0)) for e in entities
                  if e["id"] not in absorbed and degree.get(e["id"], 0) <= 1]
    if unexpanded:
        print("  unexpanded nodes (degree <= 1):")
        for e, d in sorted(unexpanded, key=lambda x: x[1]):
            print(f"    {e['kind']:14s} {e['id']}  (degree {d})")
    if hyps:
        print("  open hypotheses (untested analyst judgment):")
        for h in hyps:
            conf = f" conf={h['confidence']}" if h['confidence'] is not None else ""
            print(f"    {h['claim_id']}  {h['subj']} --{h['pred']}--> {h['obj']}{conf}")
    # disconnected clusters: pairs of models under the same lab with no
    # claim path between them (no shared pred object at distance 1)
    for lab, models in sorted(models_by_lab.items()):
        if len(models) < 2:
            continue
        linked: set = set()
        for m in models:
            linked |= subj_of.get(m, set()) & set(models)
            linked |= {s for s in obj_of.get(m, set()) if s in models}
        if len(linked) < len(models) - 1:
            print(f"  intra-lab gap: {lab} models {sorted(models)} — "
                  f"no claims link {sorted(set(models) - linked)} to siblings")
    # asymmetries: cited often as object, never as subject (passive nodes)
    passive = [e for e in entities
               if e["id"] not in absorbed and e["id"] in obj_of
               and e["id"] not in subj_of and len(obj_of[e["id"]]) >= 3]
    if passive:
        print("  passive nodes (object of >=3 claims, subject of none):")
        for e in passive:
            print(f"    {e['kind']:14s} {e['id']}  (cited by "
                  f"{len(obj_of[e['id']])} claims)")
    if not (unexpanded or hyps or passive):
        print("  (no structural signals — the graph is evenly expanded)")


# --------------------------------------------------------------------------
# Argument parsing / entry point
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="gi2.py",
        description="GI v2 — forensic investigation tool (Slice 8: dig)")
    p.add_argument("--case", help="case directory (default: $GI_CASE or ./case)")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("init", help="create a new case")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("entity", help="register an entity")
    s.add_argument("name")
    s.add_argument("--id", help="entity id (default: derived from kind+name)")
    s.add_argument("--kind", help="entity kind (default: 'entity')")
    s.add_argument("--attr", action="append", default=[], metavar="KEY=VALUE",
                   help="entity attribute (repeatable)")
    s.add_argument("--force", action="store_true",
                   help="override the normalized-duplicate guard (1.7)")
    s.set_defaults(func=cmd_entity)

    s = sub.add_parser("claim", help="assert a claim")
    s.add_argument("subj", nargs="?")
    s.add_argument("pred", nargs="?")
    s.add_argument("obj", nargs="?")
    s.add_argument("--batch", metavar="FILE",
                   help="NDJSON claims {subj,pred,obj,quote,artifact,confidence,…}; "
                        "spans auto-located and verified; per-line results")
    s.add_argument("--file", metavar="FILE|'-'", dest="file",
                   help="single JSON object or NDJSON claims (3.1): identical "
                        "validation to --batch; '-' reads stdin")
    s.add_argument("--strict-entities", action="store_true", dest="strict_entities",
                   help="refuse claims whose subj/obj is not a registered entity (default: warn)")
    s.add_argument("--prefer-extract", action="store_true", dest="prefer_extract",
                   help="(4.3) when quoting a raw-HTML artifact that has an "
                        "html-visible-v1 companion, verify the quote against the "
                        "companion's clean prose and file the citation against the "
                        "companion hash")
    s.add_argument("--polarity", choices=POLARITIES, default="supports")
    s.add_argument("--evidence", choices=EVIDENCE_KINDS, default="direct")
    s.add_argument("--confidence", type=float, default=None)
    s.add_argument("--artifact", help="sha256:... of a stored evidence object")
    s.add_argument("--span", nargs=2, type=int, metavar=("START", "END"),
                   help="character offsets into the artifact decoded as UTF-8 "
                        "(errors=replace), newlines preserved — char offsets, NOT "
                        "byte offsets; omit to auto-locate a unique quote")
    s.add_argument("--quote", help="verbatim text found at the cited span")
    s.add_argument("--id", dest="id", help="explicit claim id (default: derived)")
    s.add_argument("--valid-from", dest="valid_from", default=None,
                   help="world-time lower bound of the claim (ISO-8601); "
                        "NULL = temporally indefinite")
    s.add_argument("--valid-to", dest="valid_to", default=None,
                   help="world-time upper bound (exclusive); NULL = open-ended")
    s.add_argument("--pub-ts", dest="pub_ts", default=None,
                   help="publication date of the cited artifact (ISO-8601)")
    s.add_argument("--time-source", dest="time_source", default=None,
                   choices=list(TIME_SOURCES),
                   help="how valid_from was established")
    s.set_defaults(func=cmd_claim)

    s = sub.add_parser("supersede", help="mark a claim retracted/corrected/updated by another")
    s.add_argument("claim_id", help="the surviving claim (the correction)")
    s.add_argument("target_id", help="the claim being superseded")
    s.add_argument("--kind", choices=["retracts", "corrects", "updates"],
                   default="retracts")
    s.add_argument("--reason", required=True)
    s.set_defaults(func=cmd_supersede)

    s = sub.add_parser("retract", help="retract a claim")
    s.add_argument("claim_id")
    s.add_argument("--reason", required=True)
    s.add_argument("--scored", action="store_true",
                   help="count this retraction against the source's reputation "
                        "(default: reputation-neutral)")
    s.set_defaults(func=cmd_retract)

    s = sub.add_parser("retract-run", help="retract every claim produced by a transform run")
    s.add_argument("run_id")
    s.add_argument("--reason", required=True)
    s.add_argument("--no-scored", action="store_true",
                   help="do not count the retraction against the run's reputation "
                        "(default: scored — runs are retracted because they were wrong)")
    s.set_defaults(func=cmd_retract_run)

    s = sub.add_parser("reputation", help="show per-source reputation scores")
    s.set_defaults(func=cmd_reputation)

    s = sub.add_parser("whatif", help="simulate discrediting a source (journal untouched)")
    s.add_argument("source", help="run:<run_id> | sha256:<hex> | c_<hex> | claim:<id>")
    s.add_argument("--mode", choices=["exclude", "floor"], default="exclude",
                   help="exclude: claims vanish (fabricated source); "
                        "floor: claims stay, floored to 0.10 (unreliable source)")
    s.add_argument("--no-rep", action="store_true", help="skip reputation discounting")
    s.set_defaults(func=cmd_whatif)

    s = sub.add_parser("loadbearing", help="which artifacts hold up this edge?")
    s.add_argument("subj")
    s.add_argument("pred")
    s.add_argument("obj")
    s.add_argument("--threshold", type=float, default=0.5,
                   help="collapse threshold for the greedy cut (default 0.5)")
    s.add_argument("--no-rep", action="store_true", help="skip reputation discounting")
    s.set_defaults(func=cmd_loadbearing)

    s = sub.add_parser("merge", help="absorb one entity into another (identity alias)")
    s.add_argument("absorbed", help="entity id to absorb (goes away as an independent node)")
    s.add_argument("into", choices=["into"], metavar="into",
                   help="the literal word 'into'")
    s.add_argument("survivor", help="surviving entity id")
    s.add_argument("--reason", required=True,
                   help="why these identities are the same (forensic journal)")
    s.add_argument("--artifact", help="sha256:... of a stored evidence object")
    s.add_argument("--span", nargs=2, type=int, metavar=("START", "END"))
    s.add_argument("--quote", help="verbatim text supporting the identity decision")
    s.set_defaults(func=cmd_merge)

    s = sub.add_parser("unmerge", help="restore an absorbed entity's independence")
    s.add_argument("entity_id")
    s.add_argument("--reason", required=True)
    s.set_defaults(func=cmd_unmerge)

    s = sub.add_parser("show", help="show one entity/claims, or everything")
    s.add_argument("id", nargs="?")
    s.add_argument("--min-belief", type=float, default=None, metavar="FLOAT",
                   help="hide edges whose fused belief b is below FLOAT")
    s.add_argument("--no-rep", action="store_true", dest="no_rep",
                   help="disable reputation discounting in belief output")
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("why", help="show all evidence for one edge")
    s.add_argument("subj")
    s.add_argument("pred")
    s.add_argument("obj")
    s.add_argument("--as-of", dest="as_of", default=None,
                   help="world time T: only claims whose valid interval covers T "
                        "(ISO-8601)")
    s.add_argument("--as-learned", dest="as_learned", default=None,
                   help="assertion time T: only claims asserted by T (ISO-8601); "
                        "replaying what was known at T")
    s.add_argument("--no-rep", action="store_true", dest="no_rep",
                   help="disable reputation discounting in belief output")
    s.set_defaults(func=cmd_why)

    s = sub.add_parser("timeline", help="claims touching an entity, ordered by world time")
    s.add_argument("entity", help="entity id")
    s.add_argument("--pred", default=None, help="filter to one predicate")
    s.add_argument("--limit", type=int, default=50,
                   help="max claims to show (default 50)")
    s.set_defaults(func=cmd_timeline)

    s = sub.add_parser("neighbors", help="ranked neighbours of an entity (the pivot)")
    s.add_argument("entity", help="entity id")
    s.add_argument("--limit", type=int, default=None, help="show top N only")
    s.add_argument("--no-rep", action="store_true", help="skip reputation discounting")
    s.set_defaults(func=cmd_neighbors)

    s = sub.add_parser("expand", help="breadth-first rings with a node budget")
    s.add_argument("entity", help="entity id")
    s.add_argument("--depth", type=int, default=2, help="ring depth (default 2)")
    s.add_argument("--budget", type=int, default=40, help="max nodes admitted (default 40)")
    s.add_argument("--no-rep", action="store_true", help="skip reputation discounting")
    s.set_defaults(func=cmd_expand)

    s = sub.add_parser("mark", help="tag an entity with analyst judgment (journal event)")
    s.add_argument("entity", help="entity id")
    s.add_argument("mark", choices=list(MARK_KINDS),
                   help="one of: " + ", ".join(MARK_KINDS))
    s.add_argument("--reason", default=None, help="why (recorded)")
    s.set_defaults(func=cmd_mark)

    s = sub.add_parser("trail", help="show / clear the session breadcrumb trail")
    s.add_argument("--clear", action="store_true", help="wipe the trail dotfile")
    s.set_defaults(func=cmd_trail)

    s = sub.add_parser("verify", help="verify the hash chain, evidence store, and quote citations")
    s.add_argument("--quick", action="store_true",
                   help="skip CAS re-hash for unchanged-size artifacts "
                        "(chain walk + all quote checks still run)")
    s.set_defaults(func=cmd_verify)

    s = sub.add_parser("lint", help="case health checks: drift, scars, confidence, dupes, twins")
    s.set_defaults(func=cmd_lint)

    s = sub.add_parser("brief", help="session-start reorientation: envelope, hypotheses, signals, lint")
    s.set_defaults(func=cmd_brief)


    s = sub.add_parser("rebuild", help="force a full projection rebuild")
    s.add_argument("--repair", action="store_true",
                   help="truncate a torn tail (incomplete last line) before rebuilding")
    s.set_defaults(func=cmd_rebuild)

    s = sub.add_parser("log", help="show journal events")
    s.add_argument("--since", type=int, default=1, metavar="N",
                   help="first seq to show (default: 1)")
    s.add_argument("--pretty", action="store_true",
                   help="human-readable rendering (default is raw NDJSON for piping)")
    s.set_defaults(func=cmd_log)

    s = sub.add_parser("fetch", help="download a URL into the evidence store")
    s.add_argument("url")
    s.add_argument("--max-bytes", type=int, default=FETCH_MAX_BYTES)
    s.add_argument("--no-companion", action="store_true",
                   help="skip html-visible-v1 companion creation (diagnostics)")
    s.set_defaults(func=cmd_fetch)

    s = sub.add_parser("ingest", help="store a local file into the evidence store")
    s.add_argument("file")
    s.set_defaults(func=cmd_ingest)

    s = sub.add_parser("find-quote",
                       help="locate a quote in a stored artifact; prints the exact "
                            "spans the claim gate accepts")
    s.add_argument("--artifact", required=True, help="sha256:... of a stored evidence object")
    s.add_argument("--quote", required=True, help="text to locate (verbatim or whitespace-normalized)")
    s.add_argument("--max-hits", type=int, default=20)
    s.set_defaults(func=cmd_find_quote)

    s = sub.add_parser("run", help="execute a transform against an evidence URI")
    s.add_argument("transform", help="transform name (in scripts/transforms/<name>.py)")
    s.add_argument("--uri", required=True, help="URI to fetch and transform")
    s.add_argument("--arg", action="append", default=[], metavar="KEY=VALUE",
                   help="transform argument (repeatable)")
    s.add_argument("--timeout", type=int, default=None,
                   help="transform timeout in seconds (default: 60)")
    s.add_argument("--raw-text", action="store_true", default=False,
                   help="pass raw artifact bytes as artifact_text, skip the "
                        "html-visible-v1 companion (Phase 4.4 opt-out)")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("dig", help="read-only graph synthesis — entities, claims, "
                        "open questions; feed to the innovate skill to pick dig sites")
    s.set_defaults(func=cmd_dig)

    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except GiError as e:
        die(str(e))
    except sqlite3.Error as e:
        die(f"sqlite error: {e} (the projection is disposable — try 'rebuild')")
    except BrokenPipeError:
        try:
            sys.stdout.close()
        except Exception:
            pass
        raise SystemExit(0)
    except KeyboardInterrupt:
        die("interrupted", 130)


if __name__ == "__main__":
    main()
