"""Pivot — the investigator's walk (Slice 7).

Ranked neighbours, budgeted expansion, breadcrumb sessions. Pure query +
one ephemeral dotfile: navigation is not evidence, so NOTHING here touches
the journal. (Analyst *judgment* — `mark` — is a journal event, but it
lives in gi2.py; this module only walks.)

Ranking is deliberately explainable, not clever:
  interest  = max(b, d) + 0.10 if DISPUTED   (contested leads get eyes)
  hub       = max(1.0, degree / 8)           (mega-hubs are where
                                              investigations go to die)
  kind      = 2.0 for attr:/literal: nodes   (context, not leads)
  score     = interest / (hub * kind)

The clever ranking fight (belief vs lead-score vs information-gain) is
Slice 8's business. This is the honest baseline.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from belief import compute_edge_belief

LITERAL_PREFIXES = ("attr:", "literal:")
LITERAL_KINDS = ("attribute", "literal")
HUB_DIVISOR = 8.0          # degree at which hub penalty starts
DISPUTED_BUMP = 0.10       # contested edges rank up


# --------------------------------------------------------------------------
# session (ephemeral breadcrumbs — a dotfile, never the journal)
# --------------------------------------------------------------------------

def session_path(case_root: Path) -> Path:
    return case_root / ".session.json"


def load_session(case_root: Path) -> list[dict]:
    p = session_path(case_root)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        trail = data.get("trail", [])
        return trail if isinstance(trail, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def save_session(case_root: Path, trail: list[dict]) -> None:
    p = session_path(case_root)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"trail": trail}, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def touch_trail(case_root: Path, entity_id: str, ts: str) -> list[dict]:
    """Move entity_id to the head of the trail (most-recent-first)."""
    trail = [t for t in load_session(case_root) if t.get("id") != entity_id]
    trail.insert(0, {"id": entity_id, "ts": ts})
    save_session(case_root, trail)
    return trail


def visited_ids(case_root: Path) -> set[str]:
    return {t.get("id") for t in load_session(case_root)}


# --------------------------------------------------------------------------
# graph walking
# --------------------------------------------------------------------------

def _kind_of(conn: sqlite3.Connection, eid: str) -> str:
    row = conn.execute("SELECT kind FROM entities WHERE id = ?", (eid,)).fetchone()
    return (row["kind"] if row is not None and row["kind"] is not None else "") or ""


def _is_context_node(eid: str, kind: str) -> bool:
    return eid.startswith(LITERAL_PREFIXES) or kind in LITERAL_KINDS


def degree_of(conn: sqlite3.Connection, eid: str) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) FROM (
          SELECT DISTINCT obj AS other FROM claims WHERE subj = :e AND obj != :e
          UNION
          SELECT DISTINCT subj AS other FROM claims WHERE obj = :e AND subj != :e
        )
        """,
        {"e": eid},
    ).fetchone()
    return int(row[0])


def neighbor_edges(conn: sqlite3.Connection, eid: str) -> list[dict]:
    """Distinct active edges touching eid (either direction)."""
    rows = conn.execute(
        "SELECT DISTINCT subj, pred, obj FROM claims WHERE subj = :e OR obj = :e",
        {"e": eid},
    ).fetchall()
    out = []
    for r in rows:
        if r["subj"] == eid:
            out.append({"other": r["obj"], "pred": r["pred"], "dir": "out"})
        else:
            out.append({"other": r["subj"], "pred": r["pred"], "dir": "in"})
    return out


def rank_neighbors(conn: sqlite3.Connection, eid: str, discount=None,
                   limit: int | None = None) -> list[dict]:
    """Neighbours of eid, ranked by the explainable score above."""
    scored = []
    for e in neighbor_edges(conn, eid):
        bel = compute_edge_belief(
            conn,
            eid if e["dir"] == "out" else e["other"],
            e["pred"],
            e["other"] if e["dir"] == "out" else eid,
            discount=discount)
        kind = _kind_of(conn, e["other"])
        deg = degree_of(conn, e["other"])
        interest = max(bel["b"], bel["d"])
        if bel["verdict"] == "DISPUTED":
            interest += DISPUTED_BUMP
        hub = max(1.0, deg / HUB_DIVISOR)
        kf = 2.0 if _is_context_node(e["other"], kind) else 1.0
        scored.append({
            "other": e["other"], "pred": e["pred"], "dir": e["dir"],
            "kind": kind, "degree": deg,
            "b": bel["b"], "d": bel["d"], "verdict": bel["verdict"],
            "interest": interest, "hub": hub,
            "score": interest / (hub * kf),
        })
    scored.sort(key=lambda n: (-n["score"], n["other"]))
    if limit is not None:
        scored = scored[:limit]
    return scored


def expand_rings(conn: sqlite3.Connection, root: str, depth: int = 2,
                 budget: int = 40, discount=None) -> dict:
    """Breadth-first rings, each ring ranked so the budget cut keeps the
    interesting nodes. Cycles are impossible (visited set). Nodes cut by
    the budget are not re-offered at deeper rings (documented trade)."""
    admitted = [{"id": root, "depth": 0}]
    admitted_ids = {root}
    seen = {root}
    edges: list[dict] = []
    cut = 0
    by_ring: dict[int, list[dict]] = {0: [{"id": root, "score": 1.0,
                                            "degree": degree_of(conn, root)}]}
    frontier = [root]
    for d in range(1, depth + 1):
        ring: dict[str, dict] = {}
        for f in frontier:
            for e in neighbor_edges(conn, f):
                other = e["other"]
                if other in seen:
                    continue
                seen.add(other)
                if other in ring:
                    ring[other]["parents"].append(f)
                    continue
                ring[other] = {"id": other, "pred": e["pred"], "dir": e["dir"],
                               "parent": f, "parents": [f], "kind": _kind_of(conn, other)}
        if not ring:
            break
        # rank ring nodes by their connecting edge score (parent → node)
        scored = []
        for nid, info in ring.items():
            bel = compute_edge_belief(
                conn,
                info["parent"] if info["dir"] == "out" else nid,
                info["pred"],
                nid if info["dir"] == "out" else info["parent"],
                discount=discount)
            interest = max(bel["b"], bel["d"])
            if bel["verdict"] == "DISPUTED":
                interest += DISPUTED_BUMP
            deg = degree_of(conn, nid)
            hub = max(1.0, deg / HUB_DIVISOR)
            kf = 2.0 if _is_context_node(nid, info["kind"]) else 1.0
            info.update({"b": bel["b"], "d": bel["d"], "verdict": bel["verdict"],
                         "degree": deg, "score": interest / (hub * kf)})
            scored.append(info)
        scored.sort(key=lambda n: (-n["score"], n["id"]))
        ring_admitted = []
        for info in scored:
            if len(admitted) >= budget:
                cut += 1
                continue
            admitted.append({"id": info["id"], "depth": d})
            admitted_ids.add(info["id"])
            ring_admitted.append(info["id"])
            edges.append({"from": info["parent"], "to": info["id"],
                          "pred": info["pred"], "dir": info["dir"],
                          "score": info["score"]})
        by_ring[d] = [{"id": i["id"], "score": i["score"], "degree": i["degree"]}
                      for i in scored if i["id"] in set(ring_admitted)]
        frontier = ring_admitted
        if len(admitted) >= budget:
            break
    return {"nodes": admitted, "edges": edges, "cut": cut, "by_ring": by_ring}
