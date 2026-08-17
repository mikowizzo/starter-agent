#!/usr/bin/env python3
"""Slice 6 — counterfactual discrediting.

`whatif` answers "what breaks if this source is wrong?" without touching
the journal. `loadbearing` answers "what is this conclusion resting on?".

Both are pure QUERY over the projection: the belief kernel (Slice 2) is
re-run with a mask (a set of claim_ids to exclude or floor). Nothing is
appended, mutated, or retracted. A what-if leaves no fingerprints; if you
want the real thing, use `retract-run` (Slice 5) — the surgery, not the
rehearsal.

Mask semantics
--------------
exclude  the masked claims are removed from fusion entirely (a fabricated
         source should not even count as uncertainty).
floor    the masked claims stay, but every multiplier floors at
         REPUTATION_FLOOR (an unreliable-but-not-lying source hedges
         everything it said).
"""

from __future__ import annotations

from typing import Mapping

from belief import belief_from_claims, verdict  # noqa: F401 (verdict re-export)
from reputation import REPUTATION_FLOOR

# Classification of an edge's change between two belief computations.
FLIPS = "FLIPS"
WEAKENED = "WEAKENED"
STRENGTHENED = "STRENGTHENED"
SURVIVES = "SURVIVES"
UNTOUCHED = "UNTOUCHED"
COLLAPSES = "COLLAPSES"


def _row_to_claim(r) -> dict:
    d = dict(r)
    # claims VIEW exposes resolved subj/obj plus filed_subj/filed_obj;
    # the kernel only needs the standard claim fields.
    d.pop("filed_subj", None)
    d.pop("filed_obj", None)
    return d


def _edges(conn) -> list[tuple[str, str, str]]:
    return [tuple(r) for r in conn.execute(
        "SELECT DISTINCT subj, pred, obj FROM claims ORDER BY subj, pred, obj")]


def _claims_for_edge(conn, edge) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM claims WHERE subj = ? AND pred = ? AND obj = ? ORDER BY claim_id",
        edge).fetchall()
    return [_row_to_claim(r) for r in rows]


def classify(before: Mapping, after: Mapping) -> str:
    """Classify the change of one edge from `before` to `after` belief."""
    bv, av = before["verdict"], after["verdict"]
    bb, bd, ba = before["b"], before["d"], after["b"]
    if bv == av and abs(before["b"] - after["b"]) < 1e-9 \
            and abs(before["d"] - after["d"]) < 1e-9:
        return UNTOUCHED
    if bv != av:
        # A verdict change is the headline event: FLIPS (any direction).
        return FLIPS
    # same verdict, mass moved
    if after["b"] < before["b"] - 1e-9 or after["d"] < before["d"] - 1e-9:
        return WEAKENED
    if after["b"] > before["b"] + 1e-9 or after["d"] > before["d"] + 1e-9:
        return STRENGTHENED
    return SURVIVES


def build_mask(conn, source: str) -> set[str]:
    """claim_ids belonging to a source.

    `source` may be:
      run:<run_id>       every claim produced by that transform run
      sha256:<hex>       every claim citing that artifact (any run)
      c_<hex>            one claim
      claim:<claim_id>   manual-claim source key (reputation's view of a
                         hand-filed claim) — resolves to that claim.
    Unknown sources raise KeyError with the known alternatives listed.
    """
    if source.startswith("run:"):
        rid = source[4:]
        rows = conn.execute(
            "SELECT claim_id FROM claims WHERE via_run = ?", (rid,)).fetchall()
        if not rows:
            known = [r[0] for r in conn.execute(
                "SELECT DISTINCT via_run FROM claims WHERE via_run IS NOT NULL")]
            if rid in known:
                return set()          # run existed; all its claims retracted
            raise KeyError(
                f"no claims from run {rid!r} "
                f"(known runs with active claims: {known})")
        return {r[0] for r in rows}
    if source.startswith("sha256:"):
        rows = conn.execute(
            "SELECT claim_id FROM claims WHERE artifact = ?", (source,)).fetchall()
        if not rows:
            known = [r[0] for r in conn.execute(
                "SELECT DISTINCT artifact FROM claims WHERE artifact IS NOT NULL")]
            raise KeyError(
                f"no claims cite artifact {source!r} "
                f"(artifacts cited by active claims: {known})")
        return {r[0] for r in rows}
    if source.startswith("claim:"):
        source = source[6:]
    rows = conn.execute("SELECT claim_id FROM claims").fetchall()
    if source in {r[0] for r in rows}:
        return {source}
    raise KeyError(f"unknown claim id {source!r}")


def whatif(conn, source: str, mode: str = "exclude",
           discount=None, edges: list | None = None) -> list[dict]:
    """Re-fold every edge with `source` discredited. Read-only.

    Returns one row per affected-or-all edge (see `edges`): edge,
    before/after belief dicts, and a classification. The journal and the
    projection are never written.
    """
    if mode not in ("exclude", "floor"):
        raise ValueError(f"mode must be 'exclude' or 'floor', got {mode!r}")
    mask = build_mask(conn, source)

    def folded(claims):
        if mode == "exclude":
            kept = [c for c in claims if c["claim_id"] not in mask]
            return belief_from_claims(kept, discount=discount)
        # floor: wrap the outer discount so masked claims are floored
        def d2(c, _inner=discount, _mask=mask):
            m = REPUTATION_FLOOR if c["claim_id"] in _mask else 1.0
            if _inner is not None and c["claim_id"] not in _mask:
                m *= float(_inner(c))
            return min(1.0, m)
        return belief_from_claims(claims, discount=d2)

    out = []
    for edge in (edges if edges is not None else _edges(conn)):
        claims = _claims_for_edge(conn, edge)
        before = belief_from_claims(claims, discount=discount)
        after = folded(claims)
        if not mask & {c["claim_id"] for c in claims}:
            cls = UNTOUCHED          # this source never touched the edge
            after = before
        else:
            cls = classify(before, after)
        out.append({
            "edge": edge,
            "before": before,
            "after": after,
            "classification": cls,
            "masked_here": len(mask & {c["claim_id"] for c in claims}),
        })
    return out


def loadbearing(conn, subj: str, pred: str, obj: str, threshold: float = 0.5,
                discount=None) -> dict:
    """Greedy marginal-contribution analysis of one edge.

    For each artifact cited on the edge, its standalone marginal is how
    much belief mass its removal costs. Then a greedy pass repeatedly
    removes the highest-marginal artifact and recomputes until the edge's
    verdict collapses below `threshold` — the approximate minimum cut.
    Exact minimum cut is NP-hard (hitting set); greedy is the documented
    approximation (crew consensus, plan Slice 6).
    """
    edge = (subj, pred, obj)
    claims = _claims_for_edge(conn, edge)
    base = belief_from_claims(claims, discount=discount)

    artifacts = sorted({c["artifact"] for c in claims if c.get("artifact")})
    marginals = []
    for a in artifacts:
        kept = [c for c in claims if c.get("artifact") != a]
        b2 = belief_from_claims(kept, discount=discount)
        marginals.append({
            "artifact": a,
            "marginal_b": max(0.0, base["b"] - b2["b"]),
            "marginal_d": max(0.0, base["d"] - b2["d"]),
            "verdict_without": b2["verdict"],
        })
    marginals.sort(key=lambda m: -(m["marginal_b"] + m["marginal_d"]))

    # greedy minimum cut toward b < threshold (a SUPPORTED edge's collapse)
    cut: list[str] = []
    kept_claims = list(claims)
    cur = base
    while artifacts and len(cut) < len(artifacts):
        # recompute marginals for remaining artifacts (correlated crutches!)
        best, best_gain = None, -1.0
        remaining = [a for a in artifacts if a not in cut]
        for a in remaining:
            trial = [c for c in kept_claims if c.get("artifact") != a]
            b2 = belief_from_claims(trial, discount=discount)
            gain = (cur["b"] - b2["b"]) + (cur["d"] - b2["d"])
            if gain > best_gain:
                best, best_gain = a, gain
        if best is None:
            break
        # would removing `best` actually move us toward collapse?
        trial = [c for c in kept_claims if c.get("artifact") != best]
        b2 = belief_from_claims(trial, discount=discount)
        collapsed = (cur["verdict"] != "UNKNOWN" and b2["verdict"] == "UNKNOWN") or (
            b2["b"] < threshold and cur["b"] >= threshold)
        cut.append(best)
        kept_claims = trial
        cur = b2
        if collapsed or b2["b"] < threshold:
            break

    return {
        "edge": edge,
        "base": base,
        "marginals": marginals,
        "cut": cut,
        "after_cut": cur,
        "threshold": threshold,
    }
