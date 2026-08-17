#!/usr/bin/env python3
"""
belief.py — GI v2 Slice 2: Belief Kernel (pure module, stdlib only).

Subjective-logic belief computation over projection claims:
  claim_opinion        one claim mapping -> (b, d, u)
  cluster_claims       correlation clustering by artifact hash
  fuse                 Josang cumulative fusion (commutative)
  verdict              DISPUTED / SUPPORTED / REFUTED / UNKNOWN
  belief_from_claims   full deterministic pipeline over claim mappings
  compute_edge_belief  SQL glue for one (subj, pred, obj) edge of case.db

Design contracts
----------------
Nothing here is ever persisted. Beliefs are derived at query time from the
journal-derived projection; the journal is unchanged and remains the only
source of truth. Retracted claims are already absent from the `claims`
table (replay DELETEs them), so they are excluded by construction.

Determinism: clusters fold strictly left-to-right in ASCENDING cluster-key
order; representative selection is totally ordered; no wall clock, no
unsorted iteration in the numeric path.

Spec gaps resolved deliberately (and loudly):
  * confidence NULL -> treated as 1.0. An assertion with no stated
    confidence is a categorical assertion. The kernel is total and
    deterministic; the CLI warns.
  * 'hypothesis' evidence caps effective confidence at 0.5 HERE (clamp in
    the kernel), with the warning emitted at the CLI layer.
  * Same-artifact claims with MIXED polarity: the strongest claim's opinion
    represents the cluster's FUSION value (spec G13), but the buried
    disagreement is EXPOSED via max_supports_conf / max_refutes_conf so no
    UI can silently hide a dissenter inside one document (heretic fix).
  * Clustering is by artifact hash alone (not artifact+span): quotes from
    one document share one author's biases; different passages are still
    one witness. Conflict surfacing keeps the buried voices visible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

HYPOTHESIS_CONF_CAP = 0.5
_DISPUTED_MIN = 0.3
_MAJORITY_MIN = 0.5

VERDICT_DISPUTED = "DISPUTED"
VERDICT_SUPPORTED = "SUPPORTED"
VERDICT_REFUTED = "REFUTED"
VERDICT_UNKNOWN = "UNKNOWN"

Opinion = tuple[float, float, float]

# The vacuous opinion: total uncertainty. EXACT identity element of
# cumulative fusion, so the fold can start from it uniformly.
VACUOUS: Opinion = (0.0, 0.0, 1.0)


class BeliefError(ValueError):
    """Kernel-local input failure."""


# --------------------------------------------------------------------------
# 1. Single-claim opinion
# --------------------------------------------------------------------------

def claim_opinion(claim: Mapping) -> Opinion:
    """Map one projection claim row to a binomial opinion (b, d, u).

    Polarity fixes the axis; confidence is the mass; the remainder decays
    to UNCERTAINTY, never to belief in the negation — a weak support is
    not evidence against.
    """
    pol = claim["polarity"]
    conf = claim.get("confidence")
    if conf is None:
        # Documented policy: unquantified assertion == categorical.
        conf = 1.0
    if isinstance(conf, bool) or not isinstance(conf, (int, float)):
        raise BeliefError(f"confidence must be numeric or None (got {conf!r})")
    conf = float(conf)
    if claim.get("evidence") == "hypothesis":
        conf = min(conf, HYPOTHESIS_CONF_CAP)  # clamp HERE; CLI warns
    if not (0.0 <= conf <= 1.0):
        raise BeliefError(f"confidence out of range [0, 1]: {conf!r}")
    if pol == "supports":
        return (conf, 0.0, 1.0 - conf)
    if pol == "refutes":
        return (0.0, conf, 1.0 - conf)
    raise BeliefError(f"unknown polarity {pol!r}")


def _effective_confidence(claim: Mapping) -> float:
    """Post-clamp effective confidence (b + d of the claim's opinion)."""
    b, d, _ = claim_opinion(claim)
    return b + d


# --------------------------------------------------------------------------
# 2. Correlation clustering
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Cluster:
    key: str                       # artifact hash, else "claim:<claim_id>"
    artifact: str | None           # None => singleton uncited claim
    claim_ids: tuple[str, ...]     # sorted member ids
    representative: str            # claim_id whose opinion stands for the cluster
    opinion: Opinion
    # Observed (but NOT fused) intra-cluster disagreement, exposed so the
    # UI cannot silently bury a same-artifact refute:
    max_supports_conf: float       # 0.0 if no supports member
    max_refutes_conf: float        # 0.0 if no refutes member

    @property
    def has_internal_conflict(self) -> bool:
        return self.max_supports_conf > 0.0 and self.max_refutes_conf > 0.0


def _cluster_key(claim: Mapping) -> str:
    art = claim.get("artifact")
    # Singleton claims key on claim_id. The "claim:" prefix keeps the two
    # key namespaces disjoint and the ASCII sort order stable.
    return art if art else f"claim:{claim['claim_id']}"


def cluster_claims(claims: Iterable[Mapping]) -> list[Cluster]:
    """Group ACTIVE claims of ONE edge by artifact hash.

    Representative: highest EFFECTIVE (post-clamp) confidence;
    tie -> lexicographically smallest claim_id. Fully deterministic.
    Returned sorted ascending by cluster key (the fold order).
    """
    groups: dict[str, list[Mapping]] = {}
    for c in claims:
        groups.setdefault(_cluster_key(c), []).append(c)

    clusters: list[Cluster] = []
    for key in sorted(groups):
        members = sorted(groups[key], key=lambda c: c["claim_id"])
        best_rank: tuple[float, str] | None = None
        rep_op: Opinion | None = None
        rep_id: str | None = None
        max_sup = 0.0
        max_ref = 0.0
        for c in members:
            eff = _effective_confidence(c)
            if c["polarity"] == "supports":
                max_sup = max(max_sup, eff)
            else:
                max_ref = max(max_ref, eff)
            rank = (-eff, c["claim_id"])   # minimise => max conf, then min id
            if best_rank is None or rank < best_rank:
                best_rank, rep_op, rep_id = rank, claim_opinion(c), c["claim_id"]
        clusters.append(Cluster(
            key=key,
            artifact=members[0].get("artifact"),
            claim_ids=tuple(c["claim_id"] for c in members),
            representative=rep_id,          # type: ignore[arg-type]
            opinion=rep_op,                 # type: ignore[arg-type]
            max_supports_conf=max_sup,
            max_refutes_conf=max_ref,
        ))
    return clusters


# --------------------------------------------------------------------------
# 3. Josang cumulative fusion
# --------------------------------------------------------------------------

def _validate_opinion(o: Opinion, where: str) -> None:
    """Opinions must be non-negative and mass-1; garbage in explodes through
    small-kappa division, so the kernel refuses it loudly."""
    b, d, u = o
    if not (b >= 0.0 and d >= 0.0 and u >= 0.0):
        raise BeliefError(f"{where}: negative opinion component in {o!r}")
    if abs((b + d + u) - 1.0) > 1e-9:
        raise BeliefError(f"{where}: opinion mass != 1 in {o!r} (b+d+u={b + d + u!r})")


def fuse(o1: Opinion, o2: Opinion) -> Opinion:
    """Cumulative fusion of two independent opinions. Commutative.

    IEEE multiplication and addition are exactly commutative, so
    fuse(a, b) == fuse(b, a) bit-for-bit. Associativity holds only to
    float rounding, which is why the fold order is pinned by cluster key.
    """
    _validate_opinion(o1, "fuse(o1)")
    _validate_opinion(o2, "fuse(o2)")
    b1, d1, u1 = o1
    b2, d2, u2 = o2
    kappa = u1 + u2 - u1 * u2
    if kappa <= 1e-15:
        # Both dogmatic: component-wise arithmetic mean, u = 0.
        # (Encodes total contradiction as zero-uncertainty ambivalence;
        #  spec-mandated; verdict() still reports it as DISPUTED.)
        return ((b1 + b2) / 2.0, (d1 + d2) / 2.0, 0.0)
    b = (b1 * u2 + b2 * u1) / kappa
    d = (d1 * u2 + d2 * u1) / kappa
    u = (u1 * u2) / kappa
    # Renormalize only genuine float drift, never exact cases.
    total = b + d + u
    if total != 1.0 and abs(total - 1.0) > 1e-12 and total > 0.0:
        b /= total
        d /= total
        u /= total
    return (b, d, u)


def verdict(b: float, d: float, u: float) -> str:
    """DISPUTED dominates majority rules: conflict is reported before agreement."""
    if b >= _DISPUTED_MIN and d >= _DISPUTED_MIN:
        return VERDICT_DISPUTED
    if b >= _MAJORITY_MIN and b > d:
        return VERDICT_SUPPORTED
    if d >= _MAJORITY_MIN and d > b:
        return VERDICT_REFUTED
    return VERDICT_UNKNOWN


# --------------------------------------------------------------------------
# 4. Edge-level pipeline
# --------------------------------------------------------------------------

def belief_from_claims(claims: Iterable[Mapping], discount=None) -> dict:
    """Pure end-to-end belief over an iterable of ACTIVE claim mappings.

    `discount`, when given, is a callable mapping a claim to a multiplier
    in [0.0, 1.0]. It is applied to the STATED confidence (a NULL
    confidence is categorical 1.0) BEFORE the hypothesis clamp and
    clustering — so a discounted claim can also lose representative
    status inside its cluster. Source reputation (Slice 5) uses this:
    reputation can only ever DISCOUNT, never inflate, so no source can
    talk its own claims above what they stated.
    """
    if discount is not None:
        adjusted = []
        for c in claims:
            c = dict(c)
            m = float(discount(c))
            if not (0.0 <= m <= 1.0):
                raise BeliefError(f"discount multiplier out of [0, 1]: {m!r}")
            conf = c.get("confidence")
            base = 1.0 if conf is None else float(conf)
            c["confidence"] = base * m
            adjusted.append(c)
        claims = adjusted
    clusters = cluster_claims(claims)
    b, d, u = VACUOUS                        # exact fusion identity
    for cl in clusters:                      # ascending cluster-key order
        b, d, u = fuse((b, d, u), cl.opinion)
    return {
        "b": b, "d": d, "u": u,
        "verdict": verdict(b, d, u),
        "clusters": [
            {
                "key": cl.key,
                "artifact": cl.artifact,
                "claim_ids": list(cl.claim_ids),
                "representative": cl.representative,
                "opinion": cl.opinion,
                "max_supports_conf": cl.max_supports_conf,
                "max_refutes_conf": cl.max_refutes_conf,
                "has_internal_conflict": cl.has_internal_conflict,
            }
            for cl in clusters
        ],
    }


def compute_edge_belief(db, subj: str, pred: str, obj: str, discount=None,
                        extra_where: str = "", extra_params=()) -> dict:
    """SQL glue: belief for one projection edge.

    `db` is an open case.db connection (gi2.open_projection). Retracted
    claims are already DELETEd from `claims` by journal replay, so the
    exclusion rule requires zero extra logic here. sqlite3.Row lacks
    .get(), so rows are converted to plain dicts first.

    `extra_where` / `extra_params` implement SELECT-THEN-FUSE temporal
    selection (SQL:2011 application time): the caller restricts which
    claims enter the fusion bag (e.g. only those whose valid interval
    covers a queried world time T) BEFORE any subjective-logic combining
    happens. Non-overlapping eras therefore become distinct timeline
    states rather than fused contradictions, and superseded claims
    (excluded by the claims view) never enter the bag.
    """
    sql = ("SELECT * FROM claims WHERE subj = ? AND pred = ? AND obj = ? "
           "AND superseded = 0")
    if extra_where:
        sql += " AND " + extra_where
    rows = db.execute(sql, (subj, pred, obj, *extra_params)).fetchall()
    return belief_from_claims([dict(r) for r in rows], discount=discount)
