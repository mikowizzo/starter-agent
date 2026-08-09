#!/usr/bin/env python3
"""gi — Graph Investigator, journal edition.

An append-only claim journal that you fold, not a mutable JSON document that
you patch. Design session: Kimi K3 (ask-crew, 2026-08-08).

The case file is a directory:
    case/
      journal.ndjson   # THE TRUTH — append-only, never rewritten
      case.db          # SQLite materialized view — disposable cache
      evidence/        # content-addressed artifacts (sha256/<h[:2]>/<h[2:]>)
      gi.toml          # case-local config

Contradiction is data, not a status flag. Undo is an event. Time travel is a
query flag.

Usage:
  gi --case PATH new CASE
  gi entity NAME [--id ID] [--kind K] [--ext-id K=V]... [--alias A]...
  gi claim SUBJ PRED OBJ [--evidence direct|inferred|hypothesis] [--confidence F]
         [--polarity supports|refutes] [--cite SHA:START:END --quote "..."
         | --basis "..."] [--valid-from D] [--valid-to D]
  gi retract CLAIM_ID --reason TEXT
  gi merge SRC INTO [--reason TEXT]
  gi unmerge SRC [--reason TEXT]
  gi resolve [--auto] [--review] [--threshold F]
  gi review [--apply N | --reject N]
  gi query components|hubs|bridges|path [--from X --to Y]
         [--as-of D] [--min-belief F] [--include-disputed]
  gi neighbors ENTITY [--pred P]... [--max-degree N] [--limit N]
         [--as-of D] [--min-belief F] [--include-disputed]
  gi expand ENTITY [--depth N] [--budget N] [--pred P]... [--max-degree N]
         [--as-of D] [--min-belief F] [--include-disputed]
  gi why A B                          evidence behind a specific edge
  gi mark ENTITY LABEL                annotate (interesting/cleared/suspicious)
  gi session [--reset]                show or reset traversal breadcrumbs
  gi search QUERY                     find entities by name/kind/external-id
  gi run TRANSFORM --entity ID
  gi fetch URL
  gi ingest [FILE|-]
  gi log [--since SEQ] [--actor A]
  gi show [ID]
  gi export dot|json|csv [--as-of D] [--min-belief F]
  gi check
  gi vocab

Global: --case PATH (default: $GI_CASE or ./case). All subcommands read the
journal; the DB is a rebuildable cache (drop-and-rebuild on any mismatch).

Evidence discipline (enforced, not aspirational):
  - every non-hypothesis claim must cite a verbatim quote verified against a
    content-addressed artifact (span-checked with slack; the quote is the
    real check)
  - uncited claims are legal only as hypotheses with a stated basis
  - hashes are recomputed at ingest; a transform cannot claim it fetched
    something it did not
"""
import argparse
import hashlib
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import unicodedata
import urllib.request
from collections import defaultdict, deque
from pathlib import Path

FORMAT = 1
KNOWN_OPS = {"entity", "claim", "retract", "merge", "unmerge",
             "review_candidate", "review_decision"}
REGISTRY_PREFIXES = {"abn", "acn", "arsn", "afsl", "lei", "houjin", "asx"}
VOCAB_PATH = Path(__file__).resolve().parent / "vocab.toml"

DEFAULT_CONFIG = {
    "created_by": "gi",
    "timezone": "UTC",
    "http": {"timeout": 60, "user_agent": "gi/0.1 (+https://example.com; educational research)"},
    "transform": {"timeout": 120, "default": "transform-wiki"},
    "resolve": {"auto_merge": True, "review_threshold": 8.0},
    "belief": {"weights": {"direct": 1.0, "inferred": 0.65, "hypothesis": 0.25}},
    "check": {"disputed_max_age_days": 30},
}

CONFIG_TEMPLATE = """\
created_by = "gi"
timezone = "UTC"

[http]
timeout = 60
user_agent = "gi/0.1 (+https://example.com; educational research)"

[transform]
timeout = 120
default = "transform-wiki"

[resolve]
auto_merge = true
review_threshold = 8.0

[belief.weights]
direct = 1.0
inferred = 0.65
hypothesis = 0.25

[check]
disputed_max_age_days = 30
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS entities(
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL,
  attrs TEXT NOT NULL DEFAULT '{}', external_ids TEXT NOT NULL DEFAULT '{}',
  kind_authority TEXT NOT NULL DEFAULT 'default');
CREATE TABLE IF NOT EXISTS claims(
  claim_id TEXT PRIMARY KEY, subj TEXT NOT NULL, pred TEXT NOT NULL, obj TEXT NOT NULL,
  polarity TEXT NOT NULL CHECK (polarity IN ('supports','refutes')),
  evidence TEXT NOT NULL CHECK (evidence IN ('direct','inferred','hypothesis')),
  confidence REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','retracted')),
  valid_from TEXT, valid_to TEXT, seq INTEGER NOT NULL, actor TEXT NOT NULL,
  basis TEXT);
CREATE TABLE IF NOT EXISTS citations(
  claim_id TEXT NOT NULL, artifact TEXT NOT NULL,
  span_start INTEGER, span_end INTEGER, quote TEXT);
CREATE TABLE IF NOT EXISTS aliases(alias TEXT NOT NULL, canonical TEXT NOT NULL, seq INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS review_queue(
  id INTEGER PRIMARY KEY, a TEXT NOT NULL, b TEXT NOT NULL,
  score REAL NOT NULL, ts TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


# ─────────────────────────── small utilities ───────────────────────────

def die(msg: str, code: int = 2) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def normalize_ws(s: str) -> str:
    """Contract-normalized text — transforms duplicate this exactly."""
    return re.sub(r"\s+", " ", s).strip()


def slugify(name: str) -> str:
    s = unicodedata.normalize("NFKC", name).casefold()
    s = "".join(c if c.isalnum() else "-" for c in s)
    return re.sub(r"-+", "-", s).strip("-")


def is_registry_id(x: str) -> bool:
    return ":" in x and x.split(":", 1)[0] in REGISTRY_PREFIXES


def load_toml(path: Path) -> dict:
    try:
        import tomllib
    except ImportError:  # pragma: no cover — py <3.11
        die("tomllib required (Python >= 3.11)")
    with path.open("rb") as f:
        return tomllib.load(f)


def deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


# ─────────────────────────── journal ───────────────────────────

class Journal:
    """Append-only NDJSON. One event per line. Never rewritten."""

    def __init__(self, path: Path, case_name: str):
        self.path = path
        self.case_name = case_name
        self._last_seq = 0
        if not path.exists():
            self._create()
        else:
            self._check_format()
            self._read_tail()

    def _check_format(self) -> None:
        """Silent misinterpretation is the one sin an audit log must never commit."""
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    header = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if header.get("header"):
                    fmt = header.get("format", 0)
                    if fmt > FORMAT:
                        die(f"journal format {fmt} > supported {FORMAT}; upgrade gi")
                    return
                return  # first line wasn't a header — legacy/unknown; tolerate
        die(f"journal at {self.path} has no header")

    def _create(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        header = {"header": True, "format": FORMAT, "case": self.case_name,
                  "created": now_iso()}
        with self.path.open("w", encoding="utf-8") as f:
            f.write(json.dumps(header, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def _read_tail(self) -> None:
        last = 0
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("header"):
                    continue
                last = max(last, ev.get("seq", 0))
        self._last_seq = last

    @property
    def next_seq(self) -> int:
        return self._last_seq + 1

    def append(self, op: str, **fields) -> int:
        # Exclusive lock across processes — two concurrent `gi` runs must not
        # mint duplicate seqs (the audit trail's total order is the point).
        import fcntl
        with open(self.path, "a", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                self._read_tail()  # re-read under the lock; another process may have appended
                seq = self.next_seq
                ev = {"seq": seq, "ts": now_iso(), "op": op, **fields}
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        self._last_seq = seq
        return seq

    def replay(self):
        unknown = set()
        with self.path.open("r", encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    print(f"WARNING: journal line {lineno} unparseable; skipped",
                          file=sys.stderr)
                    continue
                if ev.get("header"):
                    fmt = ev.get("format", 0)
                    if fmt > FORMAT:
                        die(f"journal format {fmt} > supported {FORMAT}; upgrade gi")
                    continue
                op = ev.get("op")
                if op not in KNOWN_OPS:
                    if op not in unknown:
                        unknown.add(op)
                        print(f"WARNING: unknown journal op {op!r} (seq {ev.get('seq')}) "
                              f"ignored", file=sys.stderr)
                yield ev

    def line_count(self) -> int:
        n = 0
        with self.path.open("r", encoding="utf-8") as f:
            for _ in f:
                n += 1
        return n


# ─────────────────────────── identity (union-find) ───────────────────────────

class Identity:
    """Union-find over entity ids, folded from merge/unmerge events."""

    def __init__(self):
        self.merges = []      # (seq, src, into)
        self.excluded = set()  # merge seqs undone by unmerge events
        self._cache = None

    def add_merge(self, seq: int, src: str, into: str) -> None:
        self.merges.append((seq, src, into))
        self._cache = None

    def exclude(self, seq: int) -> None:
        self.excluded.add(seq)
        self._cache = None

    def _build(self):
        parent = {}

        def find(x):
            parent.setdefault(x, x)
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for seq, src, into in self.merges:
            if seq in self.excluded:
                continue
            a, b = find(src), find(into)
            # Registry-keyed id always wins over a name slug; tie -> target.
            root = a if (is_registry_id(a) and not is_registry_id(b)) else b
            parent[a if root is b else b] = root
        self._cache = (parent, find)

    def find(self, x: str) -> str:
        if self._cache is None:
            self._build()
        return self._cache[1](x)


# ─────────────────────────── case ───────────────────────────

class Case:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.journal_path = self.path / "journal.ndjson"
        self.db_path = self.path / "case.db"
        self.evidence_dir = self.path / "evidence"
        self.config_path = self.path / "gi.toml"
        if not self.journal_path.exists():
            die(f"no case at {self.path} — run: gi new {self.path}")
        self.config = self._load_config()
        self.journal = Journal(self.journal_path, self.path.name)
        self.identity = Identity()
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row
        self._maybe_rebuild()

    def _load_config(self) -> dict:
        cfg = dict(DEFAULT_CONFIG)
        if self.config_path.is_file():
            cfg = deep_merge(cfg, load_toml(self.config_path))
        return cfg

    def _maybe_rebuild(self) -> None:
        has_meta = self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
        ).fetchone()
        if has_meta is None:
            self._rebuild()
        else:
            cur = self.db.execute("SELECT value FROM meta WHERE key='journal_lines'").fetchone()
            n = self.journal.line_count()
            if cur is None or int(cur[0]) != n:
                self._rebuild()
        # Identity is folded from the journal on EVERY open — the DB may be
        # current while this fresh process's union-find is still empty.
        self._fold_identity()

    def _fold_identity(self) -> None:
        self.identity = Identity()
        for ev in self.journal.replay():
            if ev["op"] == "merge":
                self.identity.add_merge(ev["seq"], ev["src"], ev["into"])
            elif ev["op"] == "unmerge":
                self.identity.exclude(ev["merge_seq"])

    def _rebuild(self) -> None:
        with self.db:
            for t in ("entities", "claims", "citations", "aliases",
                      "review_queue", "meta"):
                self.db.execute(f"DROP TABLE IF EXISTS {t}")
            self.db.executescript(SCHEMA)
            self.identity = Identity()
            for ev in self.journal.replay():
                self._apply(ev)
            self.db.execute(
                "INSERT OR REPLACE INTO meta(key,value) VALUES('journal_lines',?)",
                (str(self.journal.line_count()),),
            )

    def _apply(self, ev: dict) -> None:
        op = ev["op"]
        if op == "entity":
            self._apply_entity(ev)
        elif op == "claim":
            self._apply_claim(ev)
        elif op == "retract":
            self.db.execute("UPDATE claims SET status='retracted' WHERE claim_id=?",
                            (ev["claim_id"],))
        elif op == "merge":
            self._apply_merge(ev)
        elif op == "unmerge":
            self.identity.exclude(ev["merge_seq"])
        elif op == "review_candidate":
            self.db.execute(
                "INSERT OR REPLACE INTO review_queue(id,a,b,score,ts) VALUES(?,?,?,?,?)",
                (ev["seq"], ev["a"], ev["b"], ev["score"], ev.get("ts", now_iso())))
        elif op == "review_decision":
            self.db.execute("DELETE FROM review_queue WHERE id=?", (ev["cand_seq"],))

    def _apply_entity(self, ev: dict) -> None:
        eid, name = ev["id"], ev.get("name", ev["id"])
        kind = ev.get("kind", "unknown")
        attrs = json.dumps(ev.get("attrs", {}), ensure_ascii=False)
        ext = json.dumps(ev.get("external_ids", {}), ensure_ascii=False)
        auth = ev.get("kind_authority", "default")
        row = self.db.execute("SELECT kind_authority FROM entities WHERE id=?",
                              (eid,)).fetchone()
        if row:
            rank = {"default": 0, "guessed": 1, "override": 2, "statutory": 3}
            if rank.get(auth, 0) < rank.get(row["kind_authority"], 0):
                auth = row["kind_authority"]
        self.db.execute(
            "INSERT OR REPLACE INTO entities(id,kind,name,attrs,external_ids,kind_authority)"
            " VALUES(?,?,?,?,?,?)", (eid, kind, name, attrs, ext, auth))
        for a in ev.get("aliases", []):
            self.db.execute(
                "INSERT OR IGNORE INTO aliases(alias,canonical,seq) VALUES(?,?,?)",
                (a, eid, ev["seq"]))

    def _apply_claim(self, ev: dict) -> None:
        self.db.execute(
            "INSERT INTO claims(claim_id,subj,pred,obj,polarity,evidence,confidence,"
            " status,valid_from,valid_to,seq,actor,basis) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ev["claim_id"], ev["subj"], ev["pred"], ev["obj"],
             ev.get("polarity", "supports"), ev["evidence"], ev["confidence"],
             ev.get("status", "active"), ev.get("valid_from"), ev.get("valid_to"),
             ev["seq"], ev.get("actor", "unknown"), ev.get("basis")))
        for c in ev.get("cites", []):
            self.db.execute(
                "INSERT INTO citations(claim_id,artifact,span_start,span_end,quote)"
                " VALUES(?,?,?,?,?)",
                (ev["claim_id"], c["artifact"], c["span"][0], c["span"][1], c["quote"]))

    def _apply_merge(self, ev: dict) -> None:
        src, into = ev["src"], ev["into"]
        self.identity.add_merge(ev["seq"], src, into)
        root = self.identity.find(src)  # post-merge root
        for alias in {src}:
            self.db.execute(
                "INSERT OR IGNORE INTO aliases(alias,canonical,seq) VALUES(?,?,?)",
                (alias, root, ev["seq"]))
        row = self.db.execute("SELECT name FROM entities WHERE id=?", (src,)).fetchone()
        if row and row["name"] != root:
            self.db.execute(
                "INSERT OR IGNORE INTO aliases(alias,canonical,seq) VALUES(?,?,?)",
                (row["name"], root, ev["seq"]))
        for r in self.db.execute("SELECT alias FROM aliases WHERE canonical=?", (src,)):
            self.db.execute(
                "INSERT OR IGNORE INTO aliases(alias,canonical,seq) VALUES(?,?,?)",
                (r["alias"], root, ev["seq"]))

    def canonical(self, x: str) -> str:
        return self.identity.find(x)

    def evidence_path(self, artifact: str) -> Path:
        # Full validation in the path constructor so EVERY caller inherits it —
        # a hostile transform must not cite files outside the evidence store.
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", artifact):
            raise ValueError(f"artifact must be sha256:<64 hex>, got {artifact!r}")
        h = artifact[len("sha256:"):]
        return self.evidence_dir / h[:2] / h[2:]

    def store_bytes(self, raw: bytes, tmp: Path | None = None) -> str:
        h = hashlib.sha256(raw).hexdigest()
        target = self.evidence_dir / h[:2] / h[2:]
        target.parent.mkdir(parents=True, exist_ok=True)
        if tmp is not None and tmp.exists():
            if not target.exists():
                os.replace(tmp, target)
            else:
                tmp.unlink(missing_ok=True)
        elif not target.exists():
            target.write_bytes(raw)
        return f"sha256:{h}"

    def verify_span(self, artifact: str, span: list, quote: str) -> None:
        path = self.evidence_path(artifact)
        if not path.is_file():
            raise HonestyViolation(f"artifact not in evidence store: {artifact}")
        text = normalize_ws(path.read_bytes().decode("utf-8", "replace"))
        start, end = span
        if not (0 <= start <= end <= len(text)):
            raise HonestyViolation(f"span {span} out of bounds for {artifact} "
                                   f"(len {len(text)})")
        q = normalize_ws(quote)
        # The span must localize the evidence: bound its width so a transform
        # can't emit [0, 2**31] and pass ANY verbatim quote regardless of
        # location. The quote must occur within span ± slack (SLACK = 64).
        if end - start > len(q) + 64:
            raise HonestyViolation(f"span {span} too wide for quote {q!r} "
                                   f"({len(q)} chars)")
        lo, hi = max(0, start - 64), min(len(text), end + 64)
        if q not in text[lo:hi]:
            raise HonestyViolation(
                f"quote not found near span {span} in {artifact}: {quote!r}")

    def close(self) -> None:
        self.db.close()


class HonestyViolation(Exception):
    pass


# ─────────────────────────── session (traversal state) ───────────────────────────

class Session:
    """Traversal state — visited set, marks, breadcrumbs.

    Working file (session.json), not evidence.  Deletable; not journaled.
    The investigator's path through the graph, separate from the graph itself.
    """
    def __init__(self, case_path):
        self.path = Path(case_path) / "session.json"
        self.data = {"seed": None, "focus": None, "trail": [], "visited": {},
                     "created": now_iso(), "updated": now_iso()}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text())
            except (json.JSONDecodeError, OSError):
                pass

    def visit(self, eid):
        is_new = eid not in self.data["visited"]
        if is_new:
            self.data["visited"][eid] = {"mark": None}
        if self.data["seed"] is None:
            self.data["seed"] = eid
        self.data["focus"] = eid
        self.data["trail"].append({"id": eid, "ts": now_iso()})
        self.data["updated"] = now_iso()
        self._save()
        return is_new

    def mark(self, eid, label):
        self.data["visited"].setdefault(eid, {})["mark"] = label
        self.data["updated"] = now_iso()
        self._save()

    def is_visited(self, eid):
        return eid in self.data["visited"]

    def get_mark(self, eid):
        v = self.data["visited"].get(eid)
        return v.get("mark") if v else None

    def reset(self):
        self.data = {"seed": None, "focus": None, "trail": [], "visited": {},
                     "created": now_iso(), "updated": now_iso()}
        self._save()

    def _save(self):
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2))


# ─────────────────────────── belief (noisy-or semilattice) ───────────────────────────

def noisy_or(ps) -> float:
    prod = 1.0
    for p in ps:
        prod *= 1.0 - p
    return 1.0 - prod


def edge_belief(claims, weights, as_of=None) -> dict:
    """Combine claims on one (subj,pred,obj) into a belief.

    Returns {state, strength, strongest}. States: absent/active/disputed/refuted.
    """
    pos, neg = [], []
    strongest = None
    for c in claims:
        if c["status"] != "active":
            continue
        if as_of:
            if c["valid_from"] and c["valid_from"] > as_of:
                continue
            if c["valid_to"] and c["valid_to"] < as_of:
                continue
        w = weights.get(c["evidence"], 0.5)
        p = c["confidence"] * w
        if c["polarity"] == "supports":
            pos.append(p)
            if strongest is None or w > weights.get(strongest, 0):
                strongest = c["evidence"]
        else:
            neg.append(p)
    sp, sn = noisy_or(pos), noisy_or(neg)
    if sp == 0 and sn == 0:
        return {"state": "absent", "strength": 0.0, "strongest": None}
    if sp > 0 and sn > 0:
        return {"state": "disputed", "strength": sp - sn, "strongest": strongest}
    if sn >= sp:
        return {"state": "refuted", "strength": sn, "strongest": None}
    return {"state": "active", "strength": sp, "strongest": strongest}


class Edge:
    __slots__ = ("subj", "pred", "obj", "belief")

    def __init__(self, subj, pred, obj, belief):
        self.subj, self.pred, self.obj, self.belief = subj, pred, obj, belief


def adjacency(case: Case, as_of=None, min_belief=0.0, include_disputed=False,
              before_seq=None):
    """Belief+time-filtered adjacency; undirected view, direction rides along.

    Two independent time axes, both honored:
      as_of       — valid-time: claims whose valid_from/valid_to window
                    contains the date (when the fact was true)
      before_seq  — ingestion-time: claims journaled before this seq
                    (what we believed then; seq = the audit total order)
    """
    weights = case.config["belief"]["weights"]
    groups = defaultdict(list)
    q = ("SELECT subj,pred,obj,polarity,evidence,confidence,status,"
         " valid_from,valid_to FROM claims")
    if before_seq is not None:
        q += " WHERE seq < ?"
        rows = case.db.execute(q, (before_seq,))
    else:
        rows = case.db.execute(q)
    for r in rows:
        s, o = case.canonical(r["subj"]), case.canonical(r["obj"])
        groups[(s, r["pred"], o)].append(dict(r))
    adj = defaultdict(dict)
    for (s, p, o), claims in groups.items():
        b = edge_belief(claims, weights, as_of)
        if b["state"] not in ("active",):
            if not (include_disputed and b["state"] == "disputed"):
                continue
        if b["strength"] < min_belief:
            continue
        e = Edge(s, p, o, b)
        adj[s].setdefault(o, []).append(e)
        adj[o].setdefault(s, []).append(e)
    return adj


# ─────────────────────────── graph interrogation ───────────────────────────

def components(adj) -> list:
    dsu = {}

    def find(x):
        dsu.setdefault(x, x)
        while dsu[x] != x:
            dsu[x] = dsu[dsu[x]]
            x = dsu[x]
        return x

    for u, nbrs in adj.items():
        for v in nbrs:
            dsu[find(u)] = find(v)
    out = defaultdict(set)
    for u in adj:
        out[find(u)].add(u)
    return sorted(out.values(), key=len, reverse=True)


def tarjan(adj):
    """Iterative Tarjan: (bridges, articulation points). No recursion limits."""
    disc, low, parent, children = {}, {}, {}, defaultdict(int)
    bridges, arts = [], set()
    roots = set()
    timer = 0
    for root in adj:
        if root in disc:
            continue
        roots.add(root)
        disc[root] = low[root] = timer
        timer += 1
        stack = [(root, iter(adj[root]))]
        while stack:
            u, it = stack[-1]
            try:
                v = next(it)
            except StopIteration:
                stack.pop()
                if u in parent:
                    p = parent[u]
                    low[p] = min(low[p], low[u])
                    if low[u] > disc[p]:
                        bridges.append((p, u))
                    if p not in roots and low[u] >= disc[p]:
                        arts.add(p)
                continue
            if v == parent.get(u):
                continue
            if v in disc:
                low[u] = min(low[u], disc[v])
            else:
                parent[v] = u
                children[u] += 1
                disc[v] = low[v] = timer
                timer += 1
                stack.append((v, iter(adj[v])))
    for r in roots:
        if children[r] > 1:
            arts.add(r)
    return bridges, arts


def shortest_path(adj, src, dst):
    if src == dst:
        return []
    prev = {src: None}
    q = deque([src])
    while q:
        u = q.popleft()
        if u == dst:
            break
        for v, edges in adj[u].items():
            if v not in prev:
                prev[v] = (u, edges)
                q.append(v)
    if dst not in prev:
        return None
    path, cur = [], dst
    while prev[cur]:
        u, edges = prev[cur]
        path.append(min(edges, key=lambda e: -e.belief["strength"]))
        cur = u
    return path[::-1]


def hubs(adj, k=10):
    deg = {u: sum(len(v) for v in nbrs.values()) for u, nbrs in adj.items()}
    _, arts = tarjan(adj)
    return sorted(deg, key=deg.get, reverse=True)[:k], sorted(arts)


# ─────────────────────────── neighborhood (local traversal) ───────────────────────────

def entity_info(case: Case, eid: str) -> dict:
    """Compact info for one entity — name, kind, external_ids."""
    r = case.db.execute("SELECT kind,name,external_ids FROM entities WHERE id=?",
                        (eid,)).fetchone()
    if r:
        try:
            ext = json.loads(r["external_ids"] or "{}")
        except json.JSONDecodeError:
            ext = {}
        return {"id": eid, "name": r["name"], "kind": r["kind"],
                "external_ids": ext, "stub": False}
    return {"id": eid, "name": eid, "kind": "unknown",
            "external_ids": {}, "stub": True}


def local_neighbors(case: Case, eid: str, as_of=None, min_belief=0.0,
                     include_disputed=False, before_seq=None) -> list:
    """1-hop neighbors of eid with belief, edge type, and degree annotation.

    Unlike adjacency() which materializes the full graph, this queries claims
    directly for eid's neighborhood — O(degree), not O(graph).
    """
    weights = case.config["belief"]["weights"]
    eid_c = case.canonical(eid)

    # Gather claims touching this entity
    q = ("SELECT claim_id,subj,pred,obj,polarity,evidence,confidence,status,"
         "valid_from,valid_to FROM claims")
    if before_seq is not None:
        q += " WHERE seq < ?"
        rows = case.db.execute(q, (before_seq,))
    else:
        rows = case.db.execute(q)
    # Group by edge (s,p,o) and keep claims involving eid_c
    edge_claims = defaultdict(list)
    for r in rows:
        s = case.canonical(r["subj"])
        o = case.canonical(r["obj"])
        if s == eid_c or o == eid_c:
            edge_claims[(s, r["pred"], o)].append(dict(r))

    # Compute belief for each edge and filter
    results = []
    for (s, p, o), claims in edge_claims.items():
        b = edge_belief(claims, weights, as_of)
        if b["state"] not in ("active",):
            if not (include_disputed and b["state"] == "disputed"):
                continue
        if b["strength"] < min_belief:
            continue
        results.append({"subj": s, "pred": p, "obj": o, "belief": b,
                        "claim_count": len(claims)})

    # Degree annotation — count all active claims touching each neighbor.
    deg_cache = {}

    def get_degree(nid):
        if nid not in deg_cache:
            deg_cache[nid] = case.db.execute(
                "SELECT COUNT(*) c FROM claims WHERE (subj=? OR obj=?) "
                "AND status='active'", (nid, nid)).fetchone()["c"]
        return deg_cache[nid]

    for r in results:
        other = r["obj"] if r["subj"] == eid_c else r["subj"]
        r["neighbor_id"] = other
        r["degree"] = get_degree(other)

    # Sort by belief strength descending
    results.sort(key=lambda x: x["belief"]["strength"], reverse=True)
    return results


def expand_bfs(case: Case, eid: str, depth: int, budget: int,
               as_of=None, min_belief=0.0, include_disputed=False,
               preds_filter=None, max_degree=None, before_seq=None,
               session=None) -> dict:
    """Depth-limited BFS expansion from eid.

    Returns {nodes: [...], edges: [...], frontier: [...], truncated: bool}.
    Respects budget (max new nodes), max_degree (skip hubs), preds_filter.
    """
    adj = adjacency(case, as_of=as_of, min_belief=min_belief,
                    include_disputed=include_disputed, before_seq=before_seq)
    start = case.canonical(eid)
    visited = {start}
    visited_order = [start]
    edges_out = []
    frontier = []
    truncated = False

    current = {start}
    for d in range(1, depth + 1):
        next_frontier = set()
        for u in current:
            nbrs = adj.get(u, {})
            for v, edge_list in nbrs.items():
                if v in visited:
                    continue
                if max_degree is not None:
                    v_deg = sum(1 for _ in adj.get(v, {}))
                    if v_deg > max_degree:
                        continue
                # Apply preds_filter
                if preds_filter:
                    matching = [e for e in edge_list if e.pred in preds_filter]
                    if not matching:
                        continue
                    edge_list = matching
                if len(visited) >= budget:
                    truncated = True
                    frontier.append(v)
                    break
                visited.add(v)
                visited_order.append(v)
                next_frontier.add(v)
                best = max(edge_list, key=lambda e: e.belief["strength"])
                edges_out.append({"from": u, "to": v, "pred": best.pred,
                                  "belief": best.belief, "depth": d})
            if truncated:
                break
        if truncated:
            break
        current = next_frontier
        if not current:
            break

    if frontier:
        frontier = list(set(frontier))
    return {"nodes": visited_order, "edges": edges_out,
            "frontier": frontier, "truncated": truncated}


def entity_label(case: Case, eid: str) -> str:
    """Short human label for an entity id."""
    info = entity_info(case, eid)
    return info["name"]


# ─────────────────────────── resolve ───────────────────────────

def jaro_winkler(a: str, b: str) -> float:
    if a == b:
        return 1.0
    la, lb = len(a), len(b)
    if la == 0 or lb == 0:
        return 0.0
    match_dist = max(la, lb) // 2 - 1
    match_dist = max(match_dist, 0)
    a_m, b_m = [False] * la, [False] * lb
    matches = 0
    for i in range(la):
        start = max(0, i - match_dist)
        end = min(i + match_dist + 1, lb)
        for j in range(start, end):
            if b_m[j] or a[i] != b[j]:
                continue
            a_m[i] = b_m[j] = True
            matches += 1
            break
    if matches == 0:
        return 0.0
    trans = 0
    k = 0
    for i in range(la):
        if not a_m[i]:
            continue
        while not b_m[k]:
            k += 1
        if a[i] != b[k]:
            trans += 1
    jaro = (matches / la + matches / lb + (matches - trans / 2) / matches) / 3
    prefix = 0
    for i in range(min(4, la, lb)):
        if a[i] == b[i]:
            prefix += 1
        else:
            break
    return jaro + prefix * 0.1 * (1 - jaro)


def name_fingerprint(name: str) -> str:
    s = unicodedata.normalize("NFKC", name).casefold()
    s = re.sub(r"株式会社|（株）|\(株\)|\bkk\b|\bco\.?,?\s*ltd\.?\b|"
               r"\bpty\.?\s*ltd\.?\b|\binc\.?\b|\blimited\b|\bgmbh\b|"
               r"\bcorp\.?\b|\bcorporation\b|\bcompany\b|\bcompanies\b|\bco\b", " ", s)
    s = "".join(ch for ch in s if ch.isalnum() or ch in " \u3041-\u3093\u30a1-\u30f3\u4e00-\u9fff")
    return " ".join(s.split())


def jaccard(a, b) -> float:
    if not a and not b:
        return 0.0
    inter = len(a & b)
    return inter / len(a | b)


def resolve(case: Case, auto: bool, review: bool, threshold: float | None) -> dict:
    """Candidate pairs via blocking; registry keys auto-merge, fuzzy -> queue."""
    threshold = threshold if threshold is not None else case.config["resolve"]["review_threshold"]
    entities = []
    for r in case.db.execute(
            "SELECT id,kind,name,external_ids FROM entities"):
        try:
            ext = json.loads(r["external_ids"])
        except json.JSONDecodeError:
            ext = {}
        aliases = {r["id"]}
        for a in case.db.execute(
                "SELECT alias FROM aliases WHERE canonical=?", (case.canonical(r["id"]),)):
            aliases.add(a["alias"])
        entities.append({"id": r["id"], "kind": r["kind"], "name": r["name"],
                         "external_ids": ext, "aliases": aliases})

    adj = adjacency(case)
    neighbors = {e["id"]: set(adj[e["id"]]) for e in entities}

    def block_keys(e):
        keys = [f"reg:{k}={v}" for k, v in e["external_ids"].items()]
        fp = name_fingerprint(e["name"])
        keys.append(f"name:{e['kind']}:{fp}")
        toks = sorted(re.findall(r"\w+", fp))
        if len(toks) >= 2:
            keys.append(f"tok:{e['kind']}:{' '.join(toks)}")
        return keys

    blocks = defaultdict(list)
    for e in entities:
        for key in block_keys(e):
            blocks[key].append(e)

    auto_cands, review_cands = [], []
    seen = set()
    for members in blocks.values():
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                a, b = members[i], members[j]
                pair = tuple(sorted((a["id"], b["id"])))
                if pair in seen:
                    continue
                seen.add(pair)
                # Auto-merge only on shared REGISTRY keys (abn:, lei:, ...) —
                # an ABN either matches or it doesn't; a wikidata= tag is not
                # a statutory join key and must go to the review queue.
                shared_reg = {k for k in set(a["external_ids"]) & set(b["external_ids"])
                              if k in REGISTRY_PREFIXES}
                if shared_reg:
                    auto_cands.append(pair)
                    continue
                s = 0.0
                fp_a, fp_b = name_fingerprint(a["name"]), name_fingerprint(b["name"])
                s += 6.0 * jaro_winkler(fp_a, fp_b)
                if fp_a and fp_a == fp_b:
                    s += 3.0  # fingerprint-equality bonus: suffix/orthography variants
                s += 3.0 * jaccard(a["aliases"], b["aliases"])
                s += 1.5 * (a["kind"] == b["kind"])
                s += 4.0 * jaccard(neighbors.get(a["id"], set()),
                                   neighbors.get(b["id"], set()))
                if s >= threshold:
                    review_cands.append((s, a["id"], b["id"]))

    if auto:
        merged = 0
        for a, b in auto_cands:
            src, into = (a, b) if is_registry_id(b) else (b, a)
            seq = case.journal.append("merge", src=src, into=into,
                                      reason="shared registry id", actor="resolve:auto")
            case.identity.add_merge(seq, src, into)
            merged += 1
        auto = merged
    else:
        auto = len(auto_cands)

    if review:
        # Candidates are journaled — the queue must survive rebuilds, and
        # "replay = audit" must be able to explain why a pair was considered.
        for s, a, b in sorted(review_cands, key=lambda t: -t[0]):
            case.journal.append("review_candidate", a=a, b=b, score=s, actor="resolve")
        case._maybe_rebuild()
    return {"auto": auto, "review": sorted(review_cands, key=lambda t: -t[0])}


# ─────────────────────────── ingest / transforms ───────────────────────────

def verify_ingest_claim(case: Case, rec: dict) -> None:
    cites = rec.get("cites") or []
    if not cites:
        if rec.get("evidence") != "hypothesis" or not rec.get("basis"):
            raise HonestyViolation(
                "uncited claim must be evidence=hypothesis with a stated basis")
    else:
        for cite in cites:
            case.verify_span(cite["artifact"], cite["span"], cite["quote"])


def ingest_stream(case: Case, stream, actor: str) -> int:
    """Atomic ingest: parse and VERIFY everything first, then commit.

    A transform must not half-commit — an HonestyViolation on line 50 must
    not leave lines 1-49 in the journal. Bad JSON lines and unknown record
    types warn-and-skip (fail-soft); only honesty violations are fatal, and
    they abort before anything is appended.
    """
    records = []
    for lineno, line in enumerate(stream, 1):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            print(f"WARNING: ingest line {lineno} unparseable; skipped",
                  file=sys.stderr)
            continue
        t = rec.get("type")
        if t not in ("artifact", "entity", "claim"):
            print(f"WARNING: unknown record type {t!r} (line {lineno}) skipped",
                  file=sys.stderr)
            continue
        records.append(rec)

    # Pass 1 — verify artifacts (hash recompute) and stage them.
    staged = []  # (tmp_path, raw) to move into the store
    for rec in records:
        if rec["type"] != "artifact":
            continue
        f = Path(rec["file"])
        if not f.is_file():
            raise HonestyViolation(f"artifact file missing: {f}")
        raw = f.read_bytes()
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        if rec.get("hash") and rec["hash"] != digest:
            raise HonestyViolation(
                f"hash mismatch for {rec.get('uri')}: {rec['hash']} != {digest}")
        staged.append((f, raw))

    # Pass 2 — verify every claim (spans against the staged/known artifacts)
    # and every entity's shape BEFORE appending anything.
    for rec in records:
        if rec["type"] == "claim":
            verify_ingest_claim(case, rec)
            for field in ("subj", "pred", "obj", "evidence", "confidence"):
                if field not in rec:
                    raise HonestyViolation(f"claim record missing {field!r}: {rec}")
        elif rec["type"] == "entity":
            if not rec.get("id") or not rec.get("name"):
                raise HonestyViolation(f"entity record missing id/name: {rec}")

    # Pass 3 — commit: store artifacts, then append events.
    for f, raw in staged:
        case.store_bytes(raw, tmp=f)
    n = 0
    for rec in records:
        t = rec["type"]
        if t == "entity":
            case.journal.append("entity", id=rec["id"], name=rec.get("name", rec["id"]),
                                kind=rec.get("kind", "unknown"),
                                attrs=rec.get("attrs", {}),
                                external_ids=rec.get("external_ids", {}),
                                aliases=rec.get("aliases", []),
                                kind_authority=rec.get("kind_authority", "default"),
                                actor=actor)
        elif t == "claim":
            case.journal.append(
                "claim", claim_id=rec.get("claim_id") or f"c{case.journal.next_seq}",
                subj=rec["subj"], pred=rec["pred"], obj=rec["obj"],
                polarity=rec.get("polarity", "supports"),
                evidence=rec["evidence"], confidence=rec["confidence"],
                valid_from=rec.get("valid_from"), valid_to=rec.get("valid_to"),
                cites=rec.get("cites", []), basis=rec.get("basis"),
                actor=actor)
        n += 1
    case._maybe_rebuild()
    return n


def run_transform(case: Case, transform: str, entity_id: str) -> int:
    script_dir = Path(__file__).resolve().parent
    name = transform if transform.endswith(".py") else transform + ".py"
    cand = [Path(transform), script_dir / transform, script_dir / name]
    exe = next((c for c in cand if c.is_file()), None)
    if exe is None:
        die(f"transform not found: {transform}")
    row = case.db.execute("SELECT id,name FROM entities WHERE id=?", (entity_id,)).fetchone()
    if row is None:
        die(f"unknown entity id: {entity_id}")
    payload = json.dumps({
        "entity": {"id": row["id"], "name": row["name"]},
        "evidence_dir": str(case.evidence_dir),
        "config": case.config,
    })
    timeout = case.config["transform"].get("timeout", 120)
    try:
        proc = subprocess.run([str(exe)], input=payload, capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"WARNING: {transform} timed out after {timeout}s; nothing ingested",
              file=sys.stderr)
        return 0
    except OSError as e:
        print(f"WARNING: cannot execute {transform}: {e}", file=sys.stderr)
        return 0
    if proc.returncode != 0:
        print(f"WARNING: {transform} exited {proc.returncode}: "
              f"{proc.stderr[:500]}", file=sys.stderr)
    n = ingest_stream(case, io.StringIO(proc.stdout), actor=f"transform:{transform}")
    print(f"ingested {n} records from {transform}")
    return n


def cmd_run(case: Case, args) -> int:
    # --entity may be a node id or a display name; resolve either way.
    row = case.db.execute(
        "SELECT id, name FROM entities WHERE id=?", (args.entity,)).fetchone()
    if row is None:
        row = case.db.execute(
            "SELECT id, name FROM entities WHERE name=?", (args.entity,)).fetchone()
    if row is None:
        die(f"unknown entity id or name: {args.entity}")
    run_transform(case, args.transform, row["id"])
    return 0


# ─────────────────────────── commands ───────────────────────────

def cmd_new(args) -> int:
    path = Path(args.path)
    if (path / "journal.ndjson").exists():
        die(f"case already exists at {path}")
    path.mkdir(parents=True, exist_ok=True)
    (path / "evidence").mkdir(exist_ok=True)
    (path / "gi.toml").write_text(CONFIG_TEMPLATE)
    Journal(path / "journal.ndjson", path.name)
    print(f"created case at {path}")
    print(f"  export GI_CASE={path}")
    return 0


def cmd_entity(case: Case, args) -> int:
    eid = args.id or slugify(args.name)
    ext = dict(kv.split("=", 1) for kv in (args.ext_id or []))
    case.journal.append("entity", id=eid, name=args.name,
                        kind=args.kind, attrs={}, external_ids=ext,
                        aliases=args.alias or [], actor=args.actor)
    case._maybe_rebuild()
    print(f"entity {eid} ({args.kind})")
    return 0


def cmd_claim(case: Case, args) -> int:
    if args.cite and args.basis:
        die("use --cite OR --basis, not both")
    cites = []
    if args.cite:
        m = re.fullmatch(r"(sha256:[0-9a-f]{64}):(\d+):(\d+)", args.cite)
        if not m:
            die("--cite must be sha256:<hex>:START:END")
        cites = [{"artifact": m.group(1), "span": [int(m.group(2)), int(m.group(3))],
                  "quote": args.quote or ""}]
        if not args.quote:
            die("--cite requires --quote")
        try:
            case.verify_span(cites[0]["artifact"], cites[0]["span"], cites[0]["quote"])
        except HonestyViolation as e:
            die(str(e))
    if not cites and args.evidence != "hypothesis":
        die("claims need --cite (with --quote) or --evidence hypothesis --basis")
    if not cites and not args.basis:
        die("hypothesis claims need --basis")
    if not 0.0 <= args.confidence <= 1.0:
        die("--confidence must be in [0,1]")
    seq = case.journal.append(
        "claim", claim_id=f"c{case.journal.next_seq}", subj=args.subj,
        pred=args.pred, obj=args.obj, polarity=args.polarity,
        evidence=args.evidence, confidence=args.confidence,
        valid_from=args.valid_from, valid_to=args.valid_to,
        cites=cites, basis=args.basis, actor=args.actor)
    case._maybe_rebuild()
    print(f"claim c{seq} {args.subj} --{args.pred}-> {args.obj} "
          f"[{args.evidence}/{args.polarity}]")
    return 0


def cmd_retract(case: Case, args) -> int:
    if not args.reason:
        die("--reason required")
    row = case.db.execute("SELECT claim_id FROM claims WHERE claim_id=?",
                          (args.claim_id,)).fetchone()
    if row is None:
        die(f"unknown claim id: {args.claim_id}")
    case.journal.append("retract", claim_id=args.claim_id, reason=args.reason,
                        actor="analyst")
    case._maybe_rebuild()
    print(f"retracted {args.claim_id}")
    return 0


def cmd_merge(case: Case, args) -> int:
    seq = case.journal.append("merge", src=args.src, into=args.into,
                              reason=args.reason or "", actor="analyst")
    case.identity.add_merge(seq, args.src, args.into)
    case._maybe_rebuild()
    print(f"merged {args.src} into {args.into} (seq {seq})")
    return 0


def cmd_unmerge(case: Case, args) -> int:
    merge_seq = None
    for ev in case.journal.replay():
        if ev["op"] == "merge" and ev["src"] == args.src:
            merge_seq = ev["seq"]
    if merge_seq is None:
        die(f"no merge involving {args.src} found")
    case.journal.append("unmerge", merge_seq=merge_seq, src=args.src,
                        reason=args.reason or "", actor="analyst")
    case.identity.exclude(merge_seq)
    case._maybe_rebuild()
    print(f"unmerged {args.src} (undoing seq {merge_seq})")
    return 0


def cmd_resolve(case: Case, args) -> int:
    res = resolve(case, args.auto, args.review, args.threshold)
    print(f"auto-merge candidates: {res['auto']}"
          + (" (merged)" if args.auto else " (run with --auto to merge)"))
    if res["review"]:
        print("review queue (gi review):")
        for s, a, b in res["review"]:
            print(f"  {s:5.2f}  {a}  <->  {b}")
    return 0


def cmd_review(case: Case, args) -> int:
    if args.apply is not None:
        row = case.db.execute(
            "SELECT * FROM review_queue WHERE id=?", (args.apply,)).fetchone()
        if row is None:
            die(f"no review entry {args.apply}")
        case.journal.append("review_decision", cand_seq=args.apply,
                            decision="apply", actor="analyst")
        seq = case.journal.append("merge", src=row["a"], into=row["b"],
                                  reason=f"resolve review #{row['id']}",
                                  actor="analyst")
        case.identity.add_merge(seq, row["a"], row["b"])
        case._maybe_rebuild()
        print(f"merged {row['a']} into {row['b']} (review #{row['id']})")
        return 0
    if args.reject is not None:
        row = case.db.execute(
            "SELECT * FROM review_queue WHERE id=?", (args.reject,)).fetchone()
        if row is None:
            die(f"no review entry {args.reject}")
        case.journal.append("review_decision", cand_seq=args.reject,
                            decision="reject", actor="analyst")
        case._maybe_rebuild()
        print(f"rejected review #{args.reject}")
        return 0
    rows = case.db.execute("SELECT * FROM review_queue ORDER BY score DESC").fetchall()
    if not rows:
        print("review queue empty")
        return 0
    for r in rows:
        print(f"#{r['id']}  {r['score']:5.2f}  {r['a']}  <->  {r['b']}  ({r['ts']})")
    print("apply: gi review --apply N | reject: gi review --reject N "
          "(N is the candidate's journal seq)")
    return 0


# ─────────────────────────── traversal commands ───────────────────────────

def _resolve_entity_input(case: Case, raw: str) -> str:
    """Resolve user input (id, name, alias, or registry id) to canonical id."""
    eid = case.canonical(raw)
    # Direct entity id
    if case.db.execute("SELECT 1 FROM entities WHERE id=?", (eid,)).fetchone():
        return eid
    # Name match
    row = case.db.execute("SELECT id FROM entities WHERE name=?", (raw,)).fetchone()
    if row:
        return case.canonical(row["id"])
    # Alias match
    row = case.db.execute("SELECT canonical FROM aliases WHERE alias=?", (raw,)).fetchone()
    if row:
        return case.canonical(row["canonical"])
    # Registry id lookup (abn:123 -> entity with that external id)
    if ":" in raw:
        prefix, _, value = raw.partition(":")
        for r in case.db.execute("SELECT id, external_ids FROM entities"):
            try:
                ext = json.loads(r["external_ids"] or "{}")
            except json.JSONDecodeError:
                ext = {}
            if ext.get(prefix) == value:
                return case.canonical(r["id"])
    # Stub — entity appears only in claims
    for r in case.db.execute("SELECT subj, obj FROM claims"):
        if case.canonical(r["subj"]) == eid or case.canonical(r["obj"]) == eid:
            return eid
    die(f"unknown entity: {raw}")


def cmd_neighbors(case: Case, args) -> int:
    """1-hop neighbors ranked by belief — the core pivot primitive."""
    eid = _resolve_entity_input(case, args.entity)
    session = Session(case.path)
    session.visit(eid)

    nbrs = local_neighbors(case, eid, as_of=args.as_of,
                           min_belief=args.min_belief,
                           include_disputed=args.include_disputed,
                           before_seq=args.before_seq)

    # Apply filters
    if args.pred:
        pred_set = set(args.pred)
        nbrs = [n for n in nbrs if n["pred"] in pred_set]
    if args.max_degree is not None:
        nbrs = [n for n in nbrs if n["degree"] <= args.max_degree]
    if args.limit is not None:
        nbrs = nbrs[:args.limit]

    info = entity_info(case, eid)
    mark = session.get_mark(eid)
    mark_str = f" [{mark}]" if mark else ""

    print(f"\n{eid} ({info['kind']}) — {info['name']}{mark_str}")
    if info.get("external_ids"):
        print("  ids: " + ", ".join(f"{k}={v}" for k, v in info["external_ids"].items()))
    print(f"  {len(nbrs)} neighbor(s)" + (f"  ({args.limit} shown)" if args.limit and len(nbrs) == args.limit else ""))

    if not nbrs:
        print("  (no neighbors pass the current filters)")
        return 0

    print()
    for n in nbrs:
        ninfo = entity_info(case, n["neighbor_id"])
        visited = session.is_visited(n["neighbor_id"])
        nmark = session.get_mark(n["neighbor_id"])
        b = n["belief"]
        b_str = f"{b['strength']:.2f}"
        if b["state"] != "active":
            b_str += f"/{b['state']}"
        hub_tag = " [hub]" if n["degree"] >= 10 else ""
        vis_tag = ""
        if visited:
            vis_tag = f" [visited{f'/{nmark}' if nmark else ''}]"
        deg_str = f"deg {n['degree']}"
        claims_str = f"{n['claim_count']} claim{'s' if n['claim_count'] != 1 else ''}"
        print(f"  {n['neighbor_id']:<28} {ninfo['kind']:<12} "
              f"belief {b_str:<8} {deg_str:<8}{hub_tag}{vis_tag}")
        print(f"    {n['pred']}  ({claims_str})")
    print(f"\n  pivot: gi neighbors <ID> | mark: gi mark <ID> <label> "
          f"| why: gi why {eid} <ID>")
    return 0


def cmd_expand(case: Case, args) -> int:
    """Depth-limited BFS expansion — the multi-hop view."""
    eid = _resolve_entity_input(case, args.entity)
    session = Session(case.path)
    session.visit(eid)

    preds_filter = set(args.pred) if args.pred else None
    result = expand_bfs(case, eid, depth=args.depth, budget=args.budget,
                        as_of=args.as_of, min_belief=args.min_belief,
                        include_disputed=args.include_disputed,
                        preds_filter=preds_filter,
                        max_degree=args.max_degree,
                        before_seq=args.before_seq)

    info = entity_info(case, eid)
    print(f"\n{eid} ({info['kind']}) — {info['name']}")
    n_nodes = len(result["nodes"])
    n_edges = len(result["edges"])
    print(f"  expansion: depth {args.depth}, {n_nodes} nodes, {n_edges} edges"
          + (f"  [TRUNCATED at budget {args.budget}]" if result["truncated"] else ""))

    if result["truncated"] and result["frontier"]:
        print(f"  frontier ({len(result['frontier'])} more): "
              + ", ".join(result["frontier"][:10])
              + ("..." if len(result["frontier"]) > 10 else ""))

    # Group edges by depth for readable output
    by_depth = defaultdict(list)
    for e in result["edges"]:
        by_depth[e["depth"]].append(e)

    for d in sorted(by_depth):
        print(f"\n  ── hop {d} " + "─" * max(0, 58 - len(str(d))))
        for e in sorted(by_depth[d], key=lambda x: -x["belief"]["strength"]):
            from_info = entity_info(case, e["from"])
            to_info = entity_info(case, e["to"])
            visited = session.is_visited(e["to"])
            vis_tag = " [visited]" if visited else ""
            b = e["belief"]
            b_str = f"{b['strength']:.2f}"
            if b["state"] != "active":
                b_str += f"/{b['state']}"
            print(f"    {e['from']:<26} --[{e['pred']} {b_str}]--> "
                  f"{e['to']:<26}{vis_tag}")

    # Session summary
    print(f"\n  session: {len(session.data['visited'])} visited, "
          f"{len(session.data['trail'])} pivots")
    return 0


def cmd_why(case: Case, args) -> int:
    """Show the evidence behind a specific edge — the audit hook."""
    a = _resolve_entity_input(case, args.a)
    b = _resolve_entity_input(case, args.b)
    a_c = case.canonical(a)
    b_c = case.canonical(b)

    weights = case.config["belief"]["weights"]
    claims = case.db.execute("SELECT * FROM claims ORDER BY seq").fetchall()
    relevant = [c for c in claims
                if (case.canonical(c["subj"]) == a_c and case.canonical(c["obj"]) == b_c)
                or (case.canonical(c["subj"]) == b_c and case.canonical(c["obj"]) == a_c)]

    if not relevant:
        print(f"no claims connect {a} and {b}")
        return 0

    a_info = entity_info(case, a_c)
    b_info = entity_info(case, b_c)
    print(f"\n{a_c} ({a_info['name']}) ←→ {b_c} ({b_info['name']})")

    grouped = defaultdict(list)
    for c in relevant:
        s = case.canonical(c["subj"])
        o = case.canonical(c["obj"])
        grouped[(s, c["pred"], o)].append(c)

    for (s, p, o), cs in grouped.items():
        b_result = edge_belief([dict(c) for c in cs], weights)
        print(f"\n  {s} --[{p}]--> {o}")
        print(f"  belief: {b_result['state']} (strength {b_result['strength']:.2f})")
        for c in cs:
            cites = case.db.execute(
                "SELECT artifact,span_start,span_end,quote FROM citations "
                "WHERE claim_id=?", (c["claim_id"],)).fetchall()
            cite_txt = ""
            if cites:
                ct = cites[0]
                cite_txt = (f"\n      evidence: {ct['artifact']}:{ct['span_start']}:"
                            f"{ct['span_end']}\n      quote: {ct['quote']!r}")
            basis_txt = f"\n      basis: {c['basis']}" if c["basis"] else ""
            print(f"    {c['claim_id']} {c['polarity']}/{c['evidence']} "
                  f"conf={c['confidence']:.2f} status={c['status']}{cite_txt}{basis_txt}")
    return 0


def cmd_mark(case: Case, args) -> int:
    """Mark an entity with a label (interesting/cleared/suspicious)."""
    eid = _resolve_entity_input(case, args.entity)
    session = Session(case.path)
    session.mark(eid, args.label)
    info = entity_info(case, eid)
    print(f"marked {eid} ({info['name']}) as [{args.label}]")
    return 0


def cmd_session(case: Case, args) -> int:
    """Show or reset the traversal session."""
    session = Session(case.path)
    if args.reset:
        session.reset()
        print("session reset")
        return 0
    d = session.data
    if not d["visited"]:
        print("no active session (visit an entity with: gi neighbors <ID>)")
        return 0
    print(f"session: {len(d['visited'])} visited, {len(d['trail'])} pivots")
    if d["seed"]:
        print(f"  seed: {d['seed']}")
    if d["focus"]:
        print(f"  focus: {d['focus']}")
    print(f"  trail:")
    for i, step in enumerate(d["trail"]):
        info = entity_info(case, step["id"])
        mark = d["visited"].get(step["id"], {}).get("mark")
        mark_str = f" [{mark}]" if mark else ""
        arrow = " → " if i > 0 else "   "
        print(f"  {arrow}{step['id']} ({info['name']}){mark_str}  {step['ts']}")
    marked = [(eid, v["mark"]) for eid, v in d["visited"].items() if v.get("mark")]
    if marked:
        print(f"  marks:")
        for eid, label in marked:
            info = entity_info(case, eid)
            print(f"    [{label}] {eid} ({info['name']})")
    return 0


def cmd_search(case: Case, args) -> int:
    """Search entities by name, kind, or external id."""
    q = args.query.lower()
    results = []
    for r in case.db.execute("SELECT id,kind,name,external_ids FROM entities ORDER BY id"):
        if q in r["name"].lower() or q in r["id"].lower():
            results.append(dict(r))
            continue
        try:
            ext = json.loads(r["external_ids"] or "{}")
            if any(q in str(v).lower() for v in ext.values()):
                results.append(dict(r))
        except json.JSONDecodeError:
            pass
    if not results:
        print(f"no entities matching {args.query!r}")
        return 0
    for r in results:
        print(f"  {r['id']:<28} ({r['kind']:<12}) {r['name']}")
    print(f"\n{len(results)} match(es)")
    return 0


def cmd_query(case: Case, args) -> int:
    adj = adjacency(case, as_of=args.as_of, min_belief=args.min_belief,
                    include_disputed=args.include_disputed,
                    before_seq=args.before_seq)
    if args.kind == "components":
        comps = components(adj)
        print(f"{len(comps)} component(s):")
        for c in comps:
            print(f"  [{len(c)}] " + ", ".join(sorted(c)))
    elif args.kind == "hubs":
        top, arts = hubs(adj)
        print("hubs (degree):")
        for u in top:
            print(f"  {u}  ({sum(len(v) for v in adj[u].values())})")
        print("articulation points (single points of failure):")
        print("  " + (", ".join(arts) if arts else "(none)"))
    elif args.kind == "bridges":
        b, _ = tarjan(adj)
        print(f"{len(b)} bridge(s) — removing one splits the network:")
        for u, v in b:
            print(f"  {u} -- {v}")
    elif args.kind == "path":
        if not args.from_ or not args.to:
            die("path needs --from and --to")
        path = shortest_path(adj, case.canonical(args.from_), case.canonical(args.to))
        if path is None:
            print("no path (in different components)")
            return 0
        print(f"path ({len(path)} hop(s)):")
        for e in path:
            b = e.belief
            print(f"  {e.subj} --[{e.pred} {b['state']} {b['strength']:.2f}]--> {e.obj}")
    else:
        die(f"unknown query kind: {args.kind}")
    return 0


def cmd_fetch(case: Case, args) -> int:
    req = urllib.request.Request(
        args.url, headers={"User-Agent": case.config["http"]["user_agent"]})
    timeout = case.config["http"]["timeout"]
    max_bytes = case.config["http"].get("max_bytes", 20_000_000)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read(max_bytes + 1)
    if len(raw) > max_bytes:
        die(f"response exceeds {max_bytes} bytes (set [http] max_bytes in gi.toml)")
    digest = case.store_bytes(raw)
    print(f"{digest}  {args.url}")
    return 0


def cmd_ingest(case: Case, args) -> int:
    if args.file and args.file != "-":
        with open(args.file, encoding="utf-8") as f:
            n = ingest_stream(case, f, actor=args.actor)
    else:
        n = ingest_stream(case, io.StringIO(sys.stdin.read()), actor=args.actor)
    print(f"ingested {n} records")
    return 0


def cmd_log(case: Case, args) -> int:
    for ev in case.journal.replay():
        if args.since and ev["seq"] < args.since:
            continue
        if args.actor and ev.get("actor") != args.actor:
            continue
        op = ev["op"]
        if op == "entity":
            extra = f"{ev['id']} ({ev.get('kind')})"
        elif op == "claim":
            extra = f"{ev['subj']} --{ev['pred']}-> {ev['obj']} [{ev.get('evidence')}]"
        elif op == "merge":
            extra = f"{ev['src']} -> {ev['into']}"
        elif op == "unmerge":
            extra = f"{ev['src']} (undo seq {ev.get('merge_seq')})"
        elif op == "retract":
            extra = f"{ev['claim_id']}"
        else:
            extra = ""
        actor = ev.get("actor", "?")
        print(f"{ev['seq']:>4} {ev['ts']} {op:<8} {extra:60} by {actor}")
    return 0


def cmd_show(case: Case, args) -> int:
    if not args.id:
        rows = case.db.execute(
            "SELECT id,kind,name,external_ids,kind_authority FROM entities ORDER BY id"
        ).fetchall()
        for r in rows:
            print(f"{r['id']}  ({r['kind']})  {r['name']}")
        print(f"\n{len(rows)} entities; "
              f"{case.db.execute('SELECT COUNT(*) c FROM claims').fetchone()['c']} claims")
        return 0
    eid = case.canonical(args.id)
    r = case.db.execute(
        "SELECT * FROM entities WHERE id=?", (eid,)).fetchone()
    if r is None and ":" in args.id:
        # Registry-id lookup: abn:24639996393 -> entity with external_ids.abn == value
        prefix, _, value = args.id.partition(":")
        for row in case.db.execute("SELECT id, external_ids FROM entities"):
            try:
                ext = json.loads(row["external_ids"] or "{}")
            except json.JSONDecodeError:
                ext = {}
            if ext.get(prefix) == value:
                eid = case.canonical(row["id"])
                r = case.db.execute(
                    "SELECT * FROM entities WHERE id=?", (eid,)).fetchone()
                break
    if r is None:
        # Claim-only stub: the id appears in claims but has no entity row.
        # Show it honestly rather than pretending it doesn't exist.
        stub = next((c for c in case.db.execute(
            "SELECT subj,obj FROM claims")
            if case.canonical(c["subj"]) == eid or case.canonical(c["obj"]) == eid), None)
        if stub is None:
            die(f"unknown entity: {args.id}")
        print(f"{eid}  (claim-only stub — no entity record)")
    else:
        print(f"{r['id']}  ({r['kind']}, authority={r['kind_authority']})")
        print(f"  name: {r['name']}")
        try:
            ext = json.loads(r["external_ids"])
            if ext:
                print("  external_ids: " + ", ".join(f"{k}={v}" for k, v in ext.items()))
        except json.JSONDecodeError:
            pass
    al = [a["alias"] for a in case.db.execute(
        "SELECT alias FROM aliases WHERE canonical=?", (eid,))]
    if al:
        print("  aliases: " + ", ".join(sorted(al)))
    # Fetch ALL claims and canonicalize in Python — claims keep their recorded
    # ids (the journal is append-only), so a merge must surface pre-merge claims.
    claims = case.db.execute(
        "SELECT * FROM claims ORDER BY seq").fetchall()
    claims = [c for c in claims
              if case.canonical(c["subj"]) == eid or case.canonical(c["obj"]) == eid]
    if claims:
        print(f"  claims ({len(claims)}):")
        weights = case.config["belief"]["weights"]
        grouped = defaultdict(list)
        for c in claims:
            s, o = case.canonical(c["subj"]), case.canonical(c["obj"])
            grouped[(s, c["pred"], o)].append(c)
        for (s, p, o), cs in grouped.items():
            b = edge_belief([dict(c) for c in cs], weights)
            print(f"    {s} --[{p} {b['state']} {b['strength']:.2f}]--> {o}")
            for c in cs:
                cites = case.db.execute(
                    "SELECT artifact,span_start,span_end,quote FROM citations WHERE claim_id=?",
                    (c["claim_id"],)).fetchall()
                cite_txt = ""
                if cites:
                    ct = cites[0]
                    cite_txt = f"  cites {ct['artifact']}:{ct['span_start']}:{ct['span_end']} {ct['quote']!r}"
                print(f"      {c['claim_id']} {c['polarity']}/{c['evidence']} "
                      f"conf={c['confidence']:.2f} status={c['status']}{cite_txt}")
    return 0


def csv_safe(v: str) -> str:
    return "'" + v if v and v[0] in "=+-@" else v


def cmd_export(case: Case, args) -> int:
    # Build from adjacency() — the same canonical, belief+time-filtered view
    # as `query`/`check`, so the court record never contradicts the console.
    adj = adjacency(case, as_of=args.as_of, min_belief=args.min_belief,
                    before_seq=args.before_seq)
    if args.format == "json":
        out = {"format": FORMAT, "as_of": args.as_of or now_iso(),
               "entities": [], "edges": []}
        for r in case.db.execute("SELECT * FROM entities ORDER BY id"):
            out["entities"].append({"id": r["id"], "kind": r["kind"],
                                    "name": r["name"],
                                    "external_ids": json.loads(r["external_ids"] or "{}")})
        seen = set()
        for s, nbrs in adj.items():
            for o, edges in nbrs.items():
                for e in edges:
                    key = (e.subj, e.pred, e.obj)
                    if key in seen:
                        continue
                    seen.add(key)
                    out["edges"].append({"subj": e.subj, "pred": e.pred,
                                         "obj": e.obj, **e.belief})
        print(json.dumps(out, ensure_ascii=False, indent=2))
    elif args.format == "dot":
        names = {r["id"]: r["name"] for r in
                 case.db.execute("SELECT id,name FROM entities")}
        lines = ["digraph gi {"]
        for eid, name in names.items():
            lines.append(f'  "{eid}" [label="{name.replace(chr(34), chr(39))}"];')
        seen = set()
        for s, nbrs in adj.items():
            for o, edges in nbrs.items():
                for e in edges:
                    key = (e.subj, e.pred, e.obj)
                    if key in seen:
                        continue
                    seen.add(key)
                    style = {"active": "solid", "disputed": "dashed",
                             "refuted": "dotted"}.get(e.belief["state"], "invis")
                    lines.append(f'  "{e.subj}" -> "{e.obj}" '
                                 f'[label="{e.pred}" style="{style}"];')
        lines.append("}")
        print("\n".join(lines))
    else:  # csv
        import csv as csv_mod
        out = io.StringIO()
        w = csv_mod.writer(out)
        w.writerow(["subj", "pred", "obj", "polarity", "evidence", "confidence",
                    "status", "valid_from", "valid_to", "quote"])
        for r in case.db.execute(
                "SELECT * FROM claims ORDER BY seq"):
            cites = case.db.execute(
                "SELECT quote FROM citations WHERE claim_id=?", (r["claim_id"],)).fetchone()
            w.writerow([csv_safe(r["subj"]), csv_safe(r["pred"]), csv_safe(r["obj"]),
                        r["polarity"], r["evidence"], r["confidence"], r["status"],
                        r["valid_from"] or "", r["valid_to"] or "",
                        csv_safe(cites["quote"] if cites else "")])
        sys.stdout.write(out.getvalue())
    return 0


def cmd_check(case: Case, args) -> int:
    import tomllib as _t
    vocab = {}
    if VOCAB_PATH.is_file():
        with VOCAB_PATH.open("rb") as f:
            vocab = _t.load(f)
    errors, warns = [], []
    known_preds = set(vocab)
    for r in case.db.execute("SELECT DISTINCT pred FROM claims"):
        if r["pred"] not in known_preds:
            warns.append(f"pred {r['pred']!r} not in vocab.toml")
    # Claim-only stubs are legitimate (see `show`); nothing to lint there.
    # Stray files in evidence/ (a transform's tmp-* left after a crash) are
    # not content-addressed and should not linger.
    if case.evidence_dir.is_dir():
        for p in case.evidence_dir.rglob("*"):
            rel = p.relative_to(case.evidence_dir).as_posix()
            if p.is_file() and not re.fullmatch(r"[0-9a-f]{2}/[0-9a-f]{62}", rel):
                warns.append(f"stray file in evidence store (not content-addressed): {p.name}")
    for r in case.db.execute("SELECT artifact FROM citations"):
        if not case.evidence_path(r["artifact"]).is_file():
            errors.append(f"missing artifact in evidence store: {r['artifact']}")
    weights = case.config["belief"]["weights"]
    days = case.config["check"].get("disputed_max_age_days", 30)
    for (s, p, o), claims in _claim_groups(case).items():
        b = edge_belief(claims, weights)
        if b["state"] == "disputed":
            warns.append(f"disputed edge {s} --{p}-> {o} "
                         f"(resolve or retract; limit {days}d)")
    for e in errors:
        print(f"ERROR: {e}")
    for w in warns:
        print(f"WARN: {w}")
    if not errors and not warns:
        print("case is clean")
    return 1 if errors else 0


def _claim_groups(case: Case):
    groups = defaultdict(list)
    for r in case.db.execute(
            "SELECT subj,pred,obj,polarity,evidence,confidence,status,valid_from,"
            " valid_to FROM claims"):
        s, o = case.canonical(r["subj"]), case.canonical(r["obj"])
        groups[(s, r["pred"], o)].append(dict(r))
    return groups


def cmd_vocab(args) -> int:
    import tomllib as _t
    if not VOCAB_PATH.is_file():
        die(f"vocab not found at {VOCAB_PATH}")
    with VOCAB_PATH.open("rb") as f:
        vocab = _t.load(f)
    print(f"{'relation':<22}{'inverse':<18}{'symmetric':<11}{'domain':<14}{'range':<12}deprecated")
    for rel, meta in vocab.items():
        print(f"{rel:<22}{meta.get('inverse',''):<18}"
              f"{str(meta.get('symmetric', False)):<11}"
              f"{meta.get('domain',''):<14}{meta.get('range',''):<12}"
              f"{meta.get('deprecated', False)}")
    return 0


# ─────────────────────────── main ───────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="gi", description="Graph Investigator, journal edition.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("new", help="create a case")
    sp.add_argument("path")

    sp = sub.add_parser("entity", help="record an entity")
    sp.add_argument("name")
    sp.add_argument("--id")
    sp.add_argument("--kind", default="unknown")
    sp.add_argument("--ext-id", action="append", metavar="K=V")
    sp.add_argument("--alias", action="append")
    sp.add_argument("--actor", default="analyst")

    sp = sub.add_parser("claim", help="record a claim (must cite or be a hypothesis)")
    sp.add_argument("subj")
    sp.add_argument("pred")
    sp.add_argument("obj")
    sp.add_argument("--evidence", choices=["direct", "inferred", "hypothesis"],
                    default="direct")
    sp.add_argument("--confidence", type=float, default=0.8)
    sp.add_argument("--polarity", choices=["supports", "refutes"], default="supports")
    sp.add_argument("--cite", metavar="SHA:START:END")
    sp.add_argument("--quote")
    sp.add_argument("--basis")
    sp.add_argument("--valid-from")
    sp.add_argument("--valid-to")
    sp.add_argument("--actor", default="analyst")

    sp = sub.add_parser("retract", help="retract a claim")
    sp.add_argument("claim_id")
    sp.add_argument("--reason", required=True)

    sp = sub.add_parser("merge", help="merge src into into (reversible event)")
    sp.add_argument("src")
    sp.add_argument("into")
    sp.add_argument("--reason")

    sp = sub.add_parser("unmerge", help="undo a merge")
    sp.add_argument("src")
    sp.add_argument("--reason")

    sp = sub.add_parser("resolve", help="suggest/apply merges")
    sp.add_argument("--auto", action="store_true")
    sp.add_argument("--review", action="store_true")
    sp.add_argument("--threshold", type=float)
    sp = sub.add_parser("review", help="pending merge queue")
    sp.add_argument("--apply", type=int)
    sp.add_argument("--reject", type=int)

    sp = sub.add_parser("query", help="interrogate the network")
    sp.add_argument("kind", choices=["components", "hubs", "bridges", "path"])
    sp.add_argument("--from", dest="from_")
    sp.add_argument("--to")
    sp.add_argument("--as-of")
    sp.add_argument("--before-seq", type=int)
    sp.add_argument("--min-belief", type=float, default=0.0)
    sp.add_argument("--include-disputed", action="store_true")

    sp = sub.add_parser("run", help="run a transform (NDJSON contract)")
    sp.add_argument("transform")
    sp.add_argument("--entity", required=True)

    sp = sub.add_parser("fetch", help="fetch a URL into the evidence store")
    sp.add_argument("url")

    sp = sub.add_parser("ingest", help="ingest NDJSON (artifact/entity/claim)")
    sp.add_argument("file", nargs="?", default="-")
    sp.add_argument("--actor", default="pipeline")

    sp = sub.add_parser("log", help="the audit trail")
    sp.add_argument("--since", type=int)
    sp.add_argument("--actor")

    sp = sub.add_parser("show", help="inspect entities/claims")
    sp.add_argument("id", nargs="?")

    sp = sub.add_parser("export", help="DOT canvas / JSON / CSV record")
    sp.add_argument("format", choices=["dot", "json", "csv"])
    sp.add_argument("--as-of")
    sp.add_argument("--before-seq", type=int)
    sp.add_argument("--min-belief", type=float, default=0.0)

    sp = sub.add_parser("check", help="structural lint")

    # ── traversal commands ──
    sp = sub.add_parser("neighbors", help="1-hop neighbors ranked by belief")
    sp.add_argument("entity", help="entity id, name, alias, or registry id")
    sp.add_argument("--pred", action="append", metavar="PRED",
                    help="filter by relation type (repeatable)")
    sp.add_argument("--max-degree", type=int, metavar="N",
                    help="hide neighbors with more than N connections")
    sp.add_argument("--limit", type=int, default=30, metavar="N",
                    help="max neighbors to show (default 30)")
    sp.add_argument("--as-of")
    sp.add_argument("--min-belief", type=float, default=0.0)
    sp.add_argument("--include-disputed", action="store_true")
    sp.add_argument("--before-seq", type=int)

    sp = sub.add_parser("expand", help="depth-limited BFS expansion")
    sp.add_argument("entity", help="entity id, name, alias, or registry id")
    sp.add_argument("--depth", type=int, default=2, metavar="N",
                    help="max hops (default 2)")
    sp.add_argument("--budget", type=int, default=50, metavar="N",
                    help="max new nodes (default 50)")
    sp.add_argument("--pred", action="append", metavar="PRED",
                    help="only follow these relation types (repeatable)")
    sp.add_argument("--max-degree", type=int, metavar="N",
                    help="skip nodes with more than N connections")
    sp.add_argument("--as-of")
    sp.add_argument("--min-belief", type=float, default=0.0)
    sp.add_argument("--include-disputed", action="store_true")
    sp.add_argument("--before-seq", type=int)

    sp = sub.add_parser("why", help="evidence behind a specific edge")
    sp.add_argument("a", help="first entity")
    sp.add_argument("b", help="second entity")

    sp = sub.add_parser("mark", help="mark an entity (interesting/cleared/suspicious)")
    sp.add_argument("entity")
    sp.add_argument("label", help="mark text (e.g. interesting, cleared, suspicious)")

    sp = sub.add_parser("session", help="show or reset the traversal session")
    sp.add_argument("--reset", action="store_true")

    sp = sub.add_parser("search", help="search entities by name, kind, or id")
    sp.add_argument("query")

    sub.add_parser("vocab", help="print the relation vocabulary")
    return p


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    case_path = os.environ.get("GI_CASE", "case")
    # Handle both --case PATH and --case=PATH forms.
    for i, a in enumerate(argv):
        if a.startswith("--case="):
            case_path = a.split("=", 1)[1]
            del argv[i]
            break
    else:
        if "--case" in argv:
            i = argv.index("--case")
            if i + 1 >= len(argv):
                die("--case needs a PATH")
            case_path = argv[i + 1]
            del argv[i:i + 2]
    args = build_parser().parse_args(argv)

    if args.cmd == "vocab":
        return cmd_vocab(args)
    if args.cmd == "new":
        return cmd_new(args)

    case = Case(case_path)
    try:
        if args.cmd == "entity":
            return cmd_entity(case, args)
        if args.cmd == "claim":
            return cmd_claim(case, args)
        if args.cmd == "retract":
            return cmd_retract(case, args)
        if args.cmd == "merge":
            return cmd_merge(case, args)
        if args.cmd == "unmerge":
            return cmd_unmerge(case, args)
        if args.cmd == "resolve":
            return cmd_resolve(case, args)
        if args.cmd == "review":
            return cmd_review(case, args)
        if args.cmd == "query":
            return cmd_query(case, args)
        if args.cmd == "run":
            return cmd_run(case, args)
        if args.cmd == "fetch":
            return cmd_fetch(case, args)
        if args.cmd == "ingest":
            return cmd_ingest(case, args)
        if args.cmd == "log":
            return cmd_log(case, args)
        if args.cmd == "show":
            return cmd_show(case, args)
        if args.cmd == "export":
            return cmd_export(case, args)
        if args.cmd == "check":
            return cmd_check(case, args)
        if args.cmd == "neighbors":
            return cmd_neighbors(case, args)
        if args.cmd == "expand":
            return cmd_expand(case, args)
        if args.cmd == "why":
            return cmd_why(case, args)
        if args.cmd == "mark":
            return cmd_mark(case, args)
        if args.cmd == "session":
            return cmd_session(case, args)
        if args.cmd == "search":
            return cmd_search(case, args)
    except HonestyViolation as e:
        die(str(e))
    finally:
        case.close()
    die(f"unknown command: {args.cmd}")
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except BrokenPipeError:
        # Downstream closed the pipe (e.g. `gi show | head`) — exit quietly.
        try:
            sys.stdout.close()
        except Exception:
            pass
        sys.exit(0)
