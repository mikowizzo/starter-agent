#!/usr/bin/env python3
"""
reputation.py — GI v2 Slice 5: Source Reputation (pure module, stdlib only).

Reputation is DERIVED, never stored: computed at query time from journal
events (via the projection) exactly like belief. No new journal op is
required for scoring itself.

Model
-----
Beta reputation (Jøsang & Ismail): each SOURCE earns a Beta(α, β) score.

  source := transform run (run_id) for claims with via_run,
            else the claim itself (unattributed, manually filed).

  +α (good)  a claim that CORROBORATES independent evidence: its edge has
             at least one other cluster (different artifact) agreeing in
             polarity. Independent agreement is the only thing that earns
             credit — being lone-witness earns nothing.
  +β (bad)   a claim that is RETRACTED with scored=true (an admission the
             claim was wrong: e.g. gate catch, hallucination discovered).
             Retract with scored=false (superseded/typo/cleanup) is neutral.
  +β (bad)   a claim whose edge verdict, computed WITHOUT its own cluster,
             is the OPPOSITE of its polarity (contra-majority). Only
             applies when the remaining evidence is strong (b or d ≥ 0.7)
             — weak edges do not punish minorities.

Reliability  r = α / (α + β)            (Beta expectation)
Multiplier   m = floor + (r - floor)    … reputation can only DISCOUNT.

Guards
------
  * m ∈ [floor, 1.0]; m = 1.0 when the source has no scored history
    (α = β = 0: uniform prior, treated as neutral — no cold-start smear,
    no cold-start privilege).
  * floor = REPUTATION_FLOOR (default 0.10). Even a maximally bad source
    keeps a sliver of voice; the user can always see its raw claims.
  * A source can never push ANOTHER source's claims up: multipliers only
    multiply stated confidence by ≤ 1.
  * Self-corroboration is impossible by construction: corroboration
    requires a DIFFERENT artifact (correlation clusters already dedupe
    same-artifact agreement).

Determinism: iteration is over sorted run_ids / claim_ids; the score
depends only on projection content, so replay is stable.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

REPUTATION_FLOOR = 0.10
CONTRA_MAJORITY_MIN = 0.7   # remaining-evidence strength needed to punish

_ALPHA_PRIOR = 0.0
_BETA_PRIOR = 0.0


@dataclass(frozen=True)
class SourceScore:
    source: str            # run_id or "claim:<claim_id>"
    kind: str              # "run" | "manual"
    alpha: float           # corroborations
    beta: float            # scored retractions + contra-majority
    n_claims: int          # active claims attributed to this source

    @property
    def reliability(self) -> float:
        denom = (self.alpha + _ALPHA_PRIOR) + (self.beta + _BETA_PRIOR)
        if denom == 0.0:
            return 1.0     # no history -> neutral, not 0.5: see Guards
        return (self.alpha + _ALPHA_PRIOR) / denom

    @property
    def multiplier(self) -> float:
        if self.alpha == 0.0 and self.beta == 0.0:
            return 1.0     # cold start: full voice
        return REPUTATION_FLOOR + (1.0 - REPUTATION_FLOOR) * self.reliability


# --------------------------------------------------------------------------
# SQL glue over the projection
# --------------------------------------------------------------------------

def _edge_clusters(db: sqlite3.Connection, subj: str, pred: str, obj: str):
    """Active claims of one edge, grouped by artifact-key, sorted."""
    rows = db.execute(
        "SELECT claim_id, subj, pred, obj, polarity, evidence, confidence, "
        "artifact, span_start, span_end, quote, via_run "
        "FROM claims WHERE subj = ? AND pred = ? AND obj = ? ORDER BY claim_id",
        (subj, pred, obj),
    ).fetchall()
    groups: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        key = r["artifact"] if r["artifact"] else f"claim:{r['claim_id']}"
        groups.setdefault(key, []).append(r)
    return rows, groups


def _source_of(claim_row) -> str:
    vr = claim_row["via_run"]
    if vr:
        return vr
    return f"claim:{claim_row['claim_id']}"


def _retraction_scores(db: sqlite3.Connection) -> dict[str, float]:
    """run_id -> count of scored retractions, from the retract log.

    The projection deletes retracted claims, so scored-retraction facts
    are read from journal replay state persisted by gi2 in the
    `retractions` table (Slice 5). Falls back to empty (neutral) when the
    table is absent (older projections) — reputation never crashes on old
    cases.
    """
    try:
        rows = db.execute(
            "SELECT via_run, COUNT(*) FROM retractions "
            "WHERE scored = 1 GROUP BY via_run"
        ).fetchall()
    except sqlite3.Error:
        return {}
    out: dict[str, float] = {}
    for via_run, n in rows:
        if via_run:
            out[via_run] = float(n)
    return out


def score_sources(db: sqlite3.Connection) -> dict[str, SourceScore]:
    """Compute reputation for every source with ≥1 active claim.

    Reads only the projection (claims view + retractions table). The
    belief computation for contra-majority uses the SAME kernel the UI
    uses, imported lazily to avoid a circular import at module load.
    """
    import belief as _b

    retractions = _retraction_scores(db)

    per_source: dict[str, dict] = {}

    def _bucket(src: str, kind: str):
        return per_source.setdefault(
            src, {"kind": kind, "alpha": 0.0, "beta": 0.0, "n": 0})

    edges = db.execute(
        "SELECT DISTINCT subj, pred, obj FROM claims ORDER BY subj, pred, obj"
    ).fetchall()

    def _punish(members):
        rep = _representative(members)
        if rep is not None:
            src = _source_of(rep)
            b = _bucket(src, "run" if rep["via_run"] else "manual")
            b["beta"] += 1.0

    for subj, pred, obj in edges:
        rows, groups = _edge_clusters(db, subj, pred, obj)
        keys = sorted(groups)
        for key in keys:
            members = groups[key]
            # every member of this cluster is attributed to its source
            for r in members:
                src = _source_of(r)
                b = _bucket(src, "run" if r["via_run"] else "manual")
                b["n"] += 1

            # corroboration: cluster's majority polarity agrees with at
            # least one OTHER cluster's majority polarity (different key)
            if len(keys) > 1:
                others = [groups[k] for k in keys if k != key]
                pol = _cluster_polarity(members)
                if pol is not None:
                    for other in others:
                        if _cluster_polarity(other) == pol:
                            # credit the REPRESENTATIVE's source (one credit
                            # per corroborating cluster pair)
                            rep = _representative(members)
                            if rep is not None:
                                src = _source_of(rep)
                                b = _bucket(src, "run" if rep["via_run"] else "manual")
                                b["alpha"] += 1.0
                            break

            # contra-majority: remove this cluster, recompute; if the
            # remaining verdict is strong and opposite, punish members
            if len(keys) > 1:
                rest = [r for k in keys if k != key for r in groups[k]]
                if rest:
                    res = _b.belief_from_claims([dict(r) for r in rest])
                    pol = _cluster_polarity(members)
                    if pol == "supports" and res["d"] >= CONTRA_MAJORITY_MIN:
                        _punish(members)
                    elif pol == "refutes" and res["b"] >= CONTRA_MAJORITY_MIN:
                        _punish(members)

    # scored retractions (journal replay state)
    for run_id, n in retractions.items():
        if run_id in per_source:
            per_source[run_id]["beta"] += n

    return {
        src: SourceScore(
            source=src,
            kind=v["kind"],
            alpha=v["alpha"],
            beta=v["beta"],
            n_claims=v["n"],
        )
        for src, v in sorted(per_source.items())
    }


def _cluster_polarity(members) -> str | None:
    """Majority polarity of a cluster by summed effective confidence."""
    sup, ref = 0.0, 0.0
    for r in members:
        eff = _effective(r)
        if r["polarity"] == "supports":
            sup += eff
        else:
            ref += eff
    if sup == ref:
        return None
    return "supports" if sup > ref else "refutes"


def _effective(r) -> float:
    conf = r["confidence"]
    if conf is None:
        conf = 1.0
    if r["evidence"] == "hypothesis":
        conf = min(conf, _b_hypothesis_cap())
    return float(conf)


def _b_hypothesis_cap() -> float:
    import belief as _b
    return _b.HYPOTHESIS_CONF_CAP


def _representative(members):
    """Same rule as the belief kernel: max effective confidence, min id."""
    best = None
    best_rank = None
    for r in members:
        eff = _effective(r)
        rank = (-eff, r["claim_id"])
        if best_rank is None or rank < best_rank:
            best_rank, best = rank, r
    return best


def discount_from_scores(scores: dict) -> "callable":
    """Build the per-claim discount callable for belief_from_claims.

    Takes a precomputed {source: SourceScore} mapping (as returned by
    score_sources) so callers can reuse one scoring pass across many
    edge queries.
    """
    def _discount(claim: dict) -> float:
        vr = claim.get("via_run")
        src = vr if vr else f"claim:{claim['claim_id']}"
        s = scores.get(src)
        if s is None:
            return 1.0
        return s.multiplier

    return _discount


def discount_for(db: sqlite3.Connection) -> "callable":
    """Convenience: score then build the discount in one call."""
    return discount_from_scores(score_sources(db))
