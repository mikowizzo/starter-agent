#!/usr/bin/env python3
"""test_belief.py — Slice-2 golden tests (G1-G13) + property tests.
Run: python3 test_belief.py   (exit 0 = all pass)
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from belief import (VACUOUS, belief_from_claims, claim_opinion, cluster_claims,
                    fuse, verdict)

TOL = 1e-9


def approx(a: float, b: float, tol: float = TOL) -> bool:
    return abs(a - b) <= tol


def assert_triple(name: str, got, want) -> None:
    assert all(approx(g, w) for g, w in zip(got, want)), \
        f"{name}: got {got}, want {want}"


def claim(cid, pol, conf, artifact=None, evidence="direct"):
    return {"claim_id": cid, "subj": "S", "pred": "P", "obj": "O",
            "polarity": pol, "evidence": evidence, "confidence": conf,
            "artifact": artifact}


A1, A2, A3 = (f"sha256:{'ab' * 32}", f"sha256:{'cd' * 32}", f"sha256:{'ef' * 32}")


def run_golden() -> None:
    # G1 single cited supports 0.8
    r = belief_from_claims([claim("c_a", "supports", 0.8, A1)])
    assert_triple("G1", (r["b"], r["d"], r["u"]), (0.8, 0.0, 0.2))
    assert r["verdict"] == "SUPPORTED", "G1 verdict"

    # G2 two independent clusters, both supports 0.8
    r = belief_from_claims([claim("c_a", "supports", 0.8, A1),
                            claim("c_b", "supports", 0.8, A2)])
    assert_triple("G2", (r["b"], r["d"], r["u"]),
                  (0.888888888888889, 0.0, 0.111111111111111))
    assert r["verdict"] == "SUPPORTED", "G2 verdict"

    # G3 echo chamber: 3 claims, SAME artifact, 0.9 each -> (0.9, 0, 1-0.9) EXACTLY
    # (no fusion inflation: u is IEEE 1.0-0.9 = 0.09999999999999998, so assert
    #  against the representative's own opinion bit-for-bit, plus tolerance)
    r = belief_from_claims([claim("c_a", "supports", 0.9, A1),
                            claim("c_b", "supports", 0.9, A1),
                            claim("c_c", "supports", 0.9, A1)])
    assert (r["b"], r["d"], r["u"]) == claim_opinion(claim("c_a", "supports", 0.9, A1)), \
        f"G3 cap must hold exactly, got {(r['b'], r['d'], r['u'])}"
    assert_triple("G3", (r["b"], r["d"], r["u"]), (0.9, 0.0, 0.1))
    assert r["verdict"] == "SUPPORTED", "G3 verdict"
    assert len(r["clusters"]) == 1, "G3 one cluster"

    # G4 lone dissenter (2 same-artifact supports + 1 independent refute)
    r = belief_from_claims([claim("c_a", "supports", 0.9, A1),
                            claim("c_b", "supports", 0.9, A1),
                            claim("c_d", "refutes", 0.6, A2)])
    assert_triple("G4", (r["b"], r["d"], r["u"]),
                  (0.782608695652174, 0.130434782608696, 0.086956521739130))
    assert r["verdict"] == "SUPPORTED", "G4 verdict"

    # G5 symmetric contradiction
    r = belief_from_claims([claim("c_a", "supports", 0.8, A1),
                            claim("c_d", "refutes", 0.8, A2)])
    assert_triple("G5", (r["b"], r["d"], r["u"]),
                  (0.444444444444444, 0.444444444444444, 0.111111111111111))
    assert r["verdict"] == "DISPUTED", "G5 verdict"

    # G6 strong refute
    r = belief_from_claims([claim("c_a", "supports", 0.6, A1),
                            claim("c_d", "refutes", 0.95, A2)])
    assert_triple("G6", (r["b"], r["d"], r["u"]),
                  (0.069767441860465, 0.883720930232558, 0.046511627906977))
    assert r["verdict"] == "REFUTED", "G6 verdict"

    # G7 weak single
    r = belief_from_claims([claim("c_a", "supports", 0.4, A1)])
    assert_triple("G7", (r["b"], r["d"], r["u"]), (0.4, 0.0, 0.6))
    assert r["verdict"] == "UNKNOWN", "G7 verdict"

    # G8 retraction symmetry: G2 minus one claim == G1
    r = belief_from_claims([claim("c_a", "supports", 0.8, A1)])
    assert_triple("G8", (r["b"], r["d"], r["u"]), (0.8, 0.0, 0.2))

    # G9 order independence, pairwise, BIT-EXACT (IEEE commutativity)
    o1, o2 = (0.9, 0.0, 0.1), (0.0, 0.6, 0.4)
    assert fuse(o1, o2) == fuse(o2, o1), "G9: fusion must be bit-exact commutative"
    assert fuse(VACUOUS, o1) == o1 and fuse(o1, VACUOUS) == o1, "G9: vacuous identity"

    # G10 monotonicity is covered by prop_monotonicity below

    # G11 dogmatic fusion
    assert fuse((1.0, 0.0, 0.0), (1.0, 0.0, 0.0)) == (1.0, 0.0, 0.0), "G11a"
    m = fuse((1.0, 0.0, 0.0), (0.0, 1.0, 0.0))
    assert m == (0.5, 0.5, 0.0), f"G11b: got {m}"
    assert verdict(*m) == "DISPUTED", "G11b verdict"

    # G12 hypothesis clamp binds both polarities
    assert claim_opinion(claim("c_a", "supports", 0.9, A1, evidence="hypothesis")) \
        == (0.5, 0.0, 0.5), "G12"
    assert claim_opinion(claim("c_a", "refutes", 0.9, A1, evidence="hypothesis")) \
        == (0.0, 0.5, 0.5), "G12 refutes"

    # G13 mixed same-artifact: strongest claim represents the cluster
    cls = cluster_claims([claim("c_a", "supports", 0.9, A1),
                          claim("c_d", "refutes", 0.6, A1)])
    assert len(cls) == 1 and cls[0].opinion == claim_opinion(claim("c_a", "supports", 0.9, A1)), "G13"
    assert cls[0].representative == "c_a", "G13 representative"
    # HERETIC FIX: the buried refute must stay VISIBLE
    assert cls[0].max_refutes_conf == 0.6 and cls[0].max_supports_conf == 0.9, \
        "G13 exposure: internal conflict must be surfaced"
    assert cls[0].has_internal_conflict, "G13 conflict flag"

    # Representative tie-break: equal confidence -> smallest claim_id
    cls = cluster_claims([claim("c_zz", "supports", 0.7, A1),
                          claim("c_aa", "supports", 0.7, A1)])
    assert cls[0].representative == "c_aa", "tie-break determinism"

    # Uncited claims are singletons even with identical content
    cls = cluster_claims([claim("c_a", "supports", 0.8, None),
                          claim("c_b", "supports", 0.8, None)])
    assert len(cls) == 2, "uncited claims must not cluster together"

    # NULL confidence -> categorical 1.0 (documented kernel policy)
    assert claim_opinion(claim("c_n", "supports", None, A1)) == (1.0, 0.0, 0.0), \
        "NULL confidence policy"

    # Empty input -> vacuous
    r = belief_from_claims([])
    assert_triple("empty", (r["b"], r["d"], r["u"]), (0.0, 0.0, 1.0))
    assert r["verdict"] == "UNKNOWN"


def prop_order_independence(trials: int = 300, seed: int = 20240) -> None:
    rng = random.Random(seed)
    for t in range(trials):
        n = rng.randint(1, 6)
        claims = [claim(f"c_{i:03d}", rng.choice(["supports", "refutes"]),
                        round(rng.random(), 6), rng.choice([A1, A2, A3, None]))
                  for i in range(n)]
        ref = belief_from_claims(claims)
        for _ in range(8):
            shuffled = claims[:]
            rng.shuffle(shuffled)
            got = belief_from_claims(shuffled)
            assert_triple(f"order t={t}", (got["b"], got["d"], got["u"]),
                          (ref["b"], ref["d"], ref["u"]))
            assert got["verdict"] == ref["verdict"], f"order verdict t={t}"


def prop_monotonicity(trials: int = 300, seed: int = 91331) -> None:
    """Adding a SUPPORTS claim never decreases b (beyond 1e-12 rounding)."""
    rng = random.Random(seed)
    for t in range(trials):
        base = [claim(f"c_{i:03d}", rng.choice(["supports", "refutes"]),
                      round(rng.random(), 6), rng.choice([A1, A2, None]))
                for i in range(rng.randint(0, 4))]
        before = belief_from_claims(base)["b"]
        # extra supporter: a NEW artifact (independent cluster) or uncited
        extra = claim("c_extra", "supports", round(rng.random(), 6),
                      rng.choice([A3, None]))
        after = belief_from_claims(base + [extra])["b"]
        assert after + 1e-12 >= before, \
            f"monotonicity violated t={t}: {before} -> {after}"


def prop_mass_conservation(trials: int = 300, seed: int = 777) -> None:
    """b + d + u == 1 within float tolerance after any fusion chain.
    Opinions are constructed u-first so components are exactly non-negative
    and mass-1 (u = 1-b-d can round negative — invalid input, refused)."""
    rng = random.Random(seed)
    for t in range(trials):
        opin = VACUOUS
        for _ in range(rng.randint(1, 10)):
            u_ = rng.random()
            b_ = rng.random() * (1.0 - u_)
            d_ = 1.0 - u_ - b_
            opin = fuse(opin, (b_, d_, u_))
        assert approx(sum(opin), 1.0, 1e-9), f"mass t={t}: {opin} sums to {sum(opin)}"
        assert all(x >= 0.0 for x in opin), f"negative component t={t}: {opin}"


if __name__ == "__main__":
    run_golden()
    prop_order_independence()
    prop_monotonicity()
    prop_mass_conservation()
    print("ALL PASS: G1-G13 golden tests + order-independence, "
          "monotonicity, mass-conservation properties (exit 0)")
