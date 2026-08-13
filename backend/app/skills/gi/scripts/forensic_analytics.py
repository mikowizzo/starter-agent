#!/usr/bin/env python3
"""forensic_analytics — versioned forensic scoring over graph facts.

Reads NDJSON from stdin (e.g. `gi query --pred 'forensic_*' | forensic_analytics.py`),
picks up forensic_fact/* and forensic_coverage/* claims emitted by
transform-forensic-facts.py, applies versioned weights from a TOML config
(inline default below; override with --weights FILE), and emits ONE derived
score claim plus its score entity.

Guarantees:
  - Weights are the ONLY place thresholds/points live. Sum of component
    maxima = 100 by construction (validated at load; script refuses to run
    otherwise — this replaces the old RAW_MAX=100-vs-actual-90 bug).
  - Failed upstream fetches can never score as "clean": if the submissions
    source is unavailable, or coverage < min_coverage, NO score claim is
    emitted — only a coverage diagnostic.
  - Provenance is machine-resolvable: the claim `basis` is JSON containing
    the sorted input claim IDs, the weights version, and the weights hash.
    The score entity ID is content-addressed:
        sha256(sorted(input_claim_ids) + weights_version)[:12]
    so identical inputs + weights always yield the same node (idempotent
    re-ingest, dedup-friendly).

Usage:
  gi query --pred 'forensic_*' | python3 forensic_analytics.py
  python3 forensic_analytics.py --weights forensic-weights.toml < facts.ndjson
  python3 forensic_analytics.py --dump-default-weights > forensic-weights.toml

Written by Kimi K3 (Synthetic) as part of the crew's Top 5 build.
Refactored to separate analytics (scoring) from ingest (facts).
"""
import argparse
import hashlib
import json
import sys

# ── Default weights (copied verbatim from forensic-weights.toml) ───────────
# Edit this TOML, bump `version`, and every downstream score claim becomes
# attributable to the new weights via weights_version + weights_hash.
DEFAULT_WEIGHTS_TOML = """\
# forensic-weights.toml — versioned scoring config for forensic_analytics.py.
# Data, not code. Component maxima MUST sum to 100 (validated at load).
# Rules: per fact name, a list of tiers; highest SATISFIED tier wins per fact;
# tier points within a component are summed and capped at the component max.
# Operators per tier: gt, gte, lt, lte, eq (numeric compare) or truthy (!= 0).

version = "1.0.0"
# Fraction of components that must have coverage="populated" to emit a score.
min_coverage = 0.6

[components.insider_activity]
max = 20
[components.insider_activity.rules]
form4_count = [ { gt = 100, pts = 20 }, { gt = 50, pts = 10 }, { gt = 20, pts = 4 } ]

[components.auditor_changes]
max = 15
[components.auditor_changes.rules]
auditor_change_count = [ { gte = 2, pts = 15 }, { gte = 1, pts = 10 } ]

[components.material_events]
max = 15
[components.material_events.rules]
high_severity_8k_count = [ { gte = 3, pts = 15 }, { gte = 2, pts = 10 }, { gte = 1, pts = 5 } ]

[components.going_concern]
max = 15
[components.going_concern.rules]
going_concern_flag           = [ { truthy = true, pts = 15 } ]
accumulated_deficit_negative = [ { truthy = true, pts = 5 } ]
accumulated_deficit_growing  = [ { truthy = true, pts = 3 } ]
negative_equity              = [ { truthy = true, pts = 7 } ]

[components.financial_deterioration]
max = 15
[components.financial_deterioration.rules]
net_income_latest    = [ { lt = 0, pts = 5 } ]
swung_to_loss        = [ { truthy = true, pts = 5 } ]
losses_deepening     = [ { truthy = true, pts = 3 } ]
revenue_decline_pct  = [ { gt = 25, pts = 7 }, { gt = 10, pts = 5 } ]

[components.filing_anomalies]
max = 10
[components.filing_anomalies.rules]
late_filing_count = [ { gte = 2, pts = 10 }, { gte = 1, pts = 10 } ]

[components.litigation_signals]
max = 10
[components.litigation_signals.rules]
other_events_8k_count = [ { gt = 20, pts = 10 }, { gt = 10, pts = 5 } ]
"""


def load_weights(path: str | None) -> tuple[dict, str, str]:
    import tomllib  # Python 3.11+
    text = open(path, "rb").read().decode() if path else DEFAULT_WEIGHTS_TOML
    cfg = tomllib.loads(text)
    # Weights hash over the CANONICAL parsed structure, not raw bytes — so
    # comment/formatting edits don't churn downstream entity IDs.
    canonical = json.dumps(cfg, sort_keys=True).encode()
    weights_hash = "sha256:" + hashlib.sha256(canonical).hexdigest()[:12]

    total = sum(c["max"] for c in cfg["components"].values())
    if total != 100:
        raise SystemExit(
            f"FATAL: component maxima sum to {total}, not 100. "
            f"Fix forensic-weights.toml (this validation replaces the old "
            f"RAW_MAX=100 bug where actual max was 90).")
    return cfg, cfg["version"], weights_hash


def normalize_ws(s: str) -> str:
    import re
    return re.sub(r"\s+", " ", s).strip()


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")


def claim_id(claim: dict) -> str:
    """Stable ID: explicit id if present, else content hash of the claim."""
    if claim.get("id"):
        return claim["id"]
    canonical = json.dumps(
        {k: claim[k] for k in sorted(claim)},
        ensure_ascii=False, sort_keys=True).encode()
    return "sha256:" + hashlib.sha256(canonical).hexdigest()[:16]


def tier_match(value: float, tier: dict) -> bool:
    if tier.get("truthy"):
        return value != 0
    if "gt" in tier and not value > tier["gt"]:
        return False
    if "gte" in tier and not value >= tier["gte"]:
        return False
    if "lt" in tier and not value < tier["lt"]:
        return False
    if "lte" in tier and not value <= tier["lte"]:
        return False
    if "eq" in tier and not value == tier["eq"]:
        return False
    return True


def score_component(comp_cfg: dict, facts: dict[str, float],
                    fact_claim_ids: dict[str, str]) -> tuple[int, list[str], set[str]]:
    pts, hits, used_ids = 0, [], set()
    for fact_name, tiers in comp_cfg.get("rules", {}).items():
        if fact_name not in facts:
            continue  # fact missing (ingest found nothing or fetch failed)
        val = facts[fact_name]
        best = 0
        for tier in tiers:
            if tier_match(val, tier):
                best = max(best, tier["pts"])
        if best > 0:
            pts += best
            hits.append(f"{fact_name}={val:g} (+{best})")
            used_ids.add(fact_claim_ids[fact_name])
    return min(pts, comp_cfg["max"]), hits, used_ids


def risk_level(score: int) -> str:
    return ("CRITICAL" if score >= 60
            else "HIGH" if score >= 40
            else "MODERATE" if score >= 20
            else "LOW")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--weights", help="Path to forensic-weights.toml "
                    "(default: inline v1.0.0)")
    ap.add_argument("--dump-default-weights", action="store_true",
                    help="Print the inline weights TOML and exit")
    args = ap.parse_args()

    if args.dump_default_weights:
        sys.stdout.write(DEFAULT_WEIGHTS_TOML)
        return 0

    cfg, w_version, w_hash = load_weights(args.weights)

    claims = []
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") == "claim":
            claims.append(obj)

    # Group facts + coverage by subject company.
    by_subj: dict[str, dict] = {}
    for c in claims:
        pred = c.get("pred", "")
        if not pred.startswith(("forensic_fact/", "forensic_coverage/")):
            continue
        bucket = by_subj.setdefault(c["subj"], {
            "facts": {}, "fact_claim_ids": {}, "coverage": {},
            "sources": {}, "coverage_claim_ids": set(),
        })
        short = pred.split("/", 1)[1]
        cid = claim_id(c)
        if pred.startswith("forensic_fact/"):
            try:
                val = float(c["obj"])
            except (TypeError, ValueError):
                continue
            bucket["facts"][short] = val          # last line wins
            bucket["fact_claim_ids"][short] = cid
        elif short == "sources":
            bucket["sources"] = json.loads(c["obj"])
            bucket["coverage_claim_ids"].add(cid)
        else:
            bucket["coverage"][short] = c["obj"]
            bucket["coverage_claim_ids"].add(cid)

    if not by_subj:
        print("No forensic_* claims on stdin — nothing to score.", file=sys.stderr)
        return 0

    min_cov = cfg.get("min_coverage", 0.6)
    exit_rc = 0

    for subj, b in sorted(by_subj.items()):
        components = cfg["components"]

        # ── Gating: a failed upstream fetch must never read as clean ──
        if str(b["sources"].get("submissions", "")).startswith("unavailable"):
            emit({"type": "claim", "subj": subj,
                  "pred": "forensic_score_unavailable",
                  "obj": "submissions_fetch_failed",
                  "polarity": "supports", "evidence": "derived",
                  "confidence": 1.0,
                  "basis": json.dumps(
                      {"reason": "SEC submissions fetch failed upstream; "
                       "no score emitted (refusing to score silence as clean)",
                       "sources": b["sources"]}, sort_keys=True)})
            print(f"SKIP {subj}: submissions unavailable — no score.",
                  file=sys.stderr)
            exit_rc = 1
            continue

        populated = sum(1 for comp in components
                        if b["coverage"].get(comp, "").startswith("populated"))
        coverage_ratio = populated / max(len(components), 1)
        if coverage_ratio < min_cov:
            emit({"type": "claim", "subj": subj,
                  "pred": "forensic_score_unavailable",
                  "obj": "insufficient_coverage",
                  "polarity": "supports", "evidence": "derived",
                  "confidence": 1.0,
                  "basis": json.dumps(
                      {"coverage_ratio": round(coverage_ratio, 3),
                       "min_coverage": min_cov,
                       "components": {comp: b["coverage"].get(comp, "missing")
                                      for comp in components}},
                      sort_keys=True)})
            print(f"SKIP {subj}: coverage {coverage_ratio:.0%} < {min_cov:.0%}.",
                  file=sys.stderr)
            exit_rc = 1
            continue

        # ── Score ──
        breakdown, all_hits, used_ids = {}, [], set(b["coverage_claim_ids"])
        total = 0
        for comp, comp_cfg in components.items():
            pts, hits, ids = score_component(comp_cfg, b["facts"], b["fact_claim_ids"])
            breakdown[comp] = pts
            total += pts
            used_ids |= ids
            for h in hits:
                all_hits.append(f"{comp}: {h}")
        level = risk_level(total)
        input_ids = sorted(used_ids)

        # ── Content-addressed score entity ID ──
        addr_src = ("\n".join(input_ids) + "\n" + w_version).encode()
        score_id = "forensic-score-" + hashlib.sha256(addr_src).hexdigest()[:12]

        basis = {
            "scorer": "forensic_analytics/1.0",
            "weights_version": w_version,
            "weights_hash": w_hash,
            "weights_file": args.weights or "<inline>",
            "input_claim_ids": input_ids,
            "coverage_ratio": round(coverage_ratio, 3),
            "breakdown": breakdown,
            "rule_hits": all_hits,
        }

        emit({"type": "entity", "id": score_id,
              "name": (f"Forensic Score: {subj} — {total}/100 ({level}) "
                       f"[weights v{w_version}]"),
              "kind": "forensic_score",
              "attrs": {
                  "company": subj,
                  "score": total,
                  "risk_level": level,
                  "weights_version": w_version,
                  "weights_hash": w_hash,
                  "content_addressed": True,
                  "breakdown": breakdown,
                  "flag_count": len(all_hits),
              },
              "external_ids": {}, "aliases": []})

        # Single derived claim — evidence='derived', confidence=1.0
        emit({"type": "claim", "subj": subj, "pred": "has_forensic_score",
              "obj": score_id,
              "polarity": "supports", "evidence": "derived",
              "confidence": 1.0,
              "basis": json.dumps(basis, ensure_ascii=False, sort_keys=True)})

        print(f"Scored {subj}: {total}/100 ({level}) → {score_id} "
              f"[weights v{w_version} {w_hash[:19]}]", file=sys.stderr)

    return exit_rc


if __name__ == "__main__":
    sys.exit(main())
