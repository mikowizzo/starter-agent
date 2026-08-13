#!/usr/bin/env python3
"""analytics_forensic_score.py — FILE 2 of the forensic analytics pipeline.

Consumes NDJSON fact claims on stdin (piped from transform-forensic-facts.py),
computes a deterministic, versioned 0-100 forensic risk score, and emits:

  1. A score report ARTIFACT (hash-verified JSON: input claim IDs, weights
     hash, per-component breakdown, computed_at).
  2. A score claim: entity kind=forensic_score, evidence=derived,
     confidence=1.0, with a machine-resolvable basis pointing at the artifact.
  3. Individual red_flag claims (kind=forensic_red_flag) with stable
     hash-based IDs, one per triggered component.

Score IDs are content-addressed:

    f'{slug}-score-{sha256(input_claim_ids + weights_version)[:12]}'

so the same input claims + weights version always yield the same score ID,
regardless of when the pipeline is re-run. Weights live in VERSIONED_WEIGHTS
and can be revised (new version key) without re-ingesting facts.

GI contract:
    stdin   — NDJSON fact claims from transform-forensic-facts.py
    stdout  — NDJSON claims (artifact record + forensic_score + red_flags)
    stderr  — diagnostics / warnings
    exit    — 0 ok, 1 fatal, 2 no usable claims

Author: Kimi K3 (Synthetic), reviewed and saved by Nami
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional

log = logging.getLogger("analytics_forensic_score")

GENERATOR = "analytics_forensic_score.py/1.0.0"
SCORE_SCALE = 100  # final score is normalized to 0..SCORE_SCALE

# ---------------------------------------------------------------------------
# Versioned scoring weights.
#
# Bump by adding a NEW key — never mutate a released version. Old scores stay
# reproducible because each score claim records its weights_version, and old
# fact claims can be re-scored against any version still present here.
# ---------------------------------------------------------------------------

VERSIONED_WEIGHTS: dict[str, dict[str, float]] = {
    "v1.0.0": {
        "going_concern_flag": 25.0,
        "auditor_change": 15.0,
        "high_severity_8k": 15.0,
        "late_filing": 10.0,
        "form4_volume": 10.0,
        "net_income": 10.0,
        "accumulated_deficit": 10.0,
        "stockholders_equity": 5.0,
    },
    # "v1.1.0": { ... }  # future revisions go here; v1.0.0 stays frozen.
}

DEFAULT_WEIGHTS_VERSION = "v1.0.0"

# Canonical fact names this analyzer consumes. Must match the fact names
# emitted in claim.basis by transform-forensic-facts.py.
EXPECTED_FACTS = (
    "form4_volume",
    "auditor_change",
    "high_severity_8k",
    "late_filing",
    "net_income",
    "accumulated_deficit",
    "stockholders_equity",
    "going_concern_flag",
)

# Caps used to normalize unbounded count-type facts into [0, weight].
FORM4_VOLUME_CAP = 10        # >= 10 insider transactions in 1y saturates
HIGH_SEVERITY_8K_CAP = 5     # >= 5 high-severity 8-K items saturates
LATE_FILING_CAP = 3          # >= 3 late-filing notices saturates

# Trigger thresholds: at/above these, a red_flag entity is emitted.
FORM4_VOLUME_TRIGGER = 5
HIGH_SEVERITY_8K_TRIGGER = 1
LATE_FILING_TRIGGER = 1


# ---------------------------------------------------------------------------
# Canonical serialization + hashing helpers
# ---------------------------------------------------------------------------

def canonical_json(obj: Any) -> str:
    """Deterministic JSON serialization — same object, same bytes, always."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_hex(data: str | bytes) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Input claim normalization
# ---------------------------------------------------------------------------

def as_bool(value: Any) -> bool:
    """Tolerant boolean coercion for fact values crossing the NDJSON boundary."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1", "y", "t"}
    return bool(value)


def as_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", "").strip())
        except ValueError:
            return None
    return None


def coerce_count(value: Any) -> int:
    """Counts may arrive as an int, or as a list of item dicts (e.g. 8-K items)."""
    if isinstance(value, list):
        return len(value)
    n = as_number(value)
    return max(0, int(n)) if n is not None else 0


class FactClaim:
    """Normalized view of one incoming fact claim line."""

    __slots__ = ("claim_id", "slug", "fact_name", "value", "raw")

    # Basis keys we accept for the fact name, in priority order. The GI
    # contract fixes one of these in FILE 1; we stay tolerant here so a
    # minor naming drift upstream is a warning, not a pipeline halt.
    FACT_NAME_KEYS = ("fact_name", "fact", "metric", "indicator", "signal")
    VALUE_KEYS = ("value", "fact_value", "observed", "result")

    def __init__(self, raw: dict[str, Any], line_no: int) -> None:
        self.raw = raw
        self.claim_id: str = str(raw.get("id") or raw.get("claim_id") or f"line-{line_no}")
        basis = raw.get("basis")
        basis_dict = basis if isinstance(basis, dict) else {}
        entity = raw.get("entity") or {}

        self.fact_name: Optional[str] = None
        self.value: Any = None
        self.slug: Optional[str] = entity.get("slug") or raw.get("slug")

        # --- Bridge: when basis is a plain string (transform-forensic-facts
        # emits pred=fact_name, obj=value, basis=description), infer from
        # the claim's pred field directly.
        if not basis_dict and isinstance(basis, str):
            pred = raw.get("pred", "")
            if pred in EXPECTED_FACTS:
                self.fact_name = pred
                self.value = raw.get("obj")
                self.slug = raw.get("subj")
            # Non-EXPECTED_FACTS preds fall through and get filtered by
            # the COMPONENT_SCORERS check in run().
            return

        # Normal path: basis is a dict with fact_name/value keys.
        for key in self.FACT_NAME_KEYS:
            if isinstance(basis_dict.get(key), str):
                self.fact_name = basis_dict[key].strip().lower()
                break
        if self.fact_name is None:
            # Last resort: recognize a known fact name embedded in the claim ID,
            # e.g. "acme-corp:form4-volume-1y".
            lowered = self.claim_id.lower()
            for expected in EXPECTED_FACTS:
                if expected.replace("_", "-") in lowered or expected in lowered:
                    self.fact_name = expected
                    break

        for key in self.VALUE_KEYS:
            if key in basis_dict:
                self.value = basis_dict[key]
                break
        else:
            self.value = basis_dict.get("value", raw.get("value"))

    @property
    def usable(self) -> bool:
        return self.fact_name is not None and self.slug is not None


# ---------------------------------------------------------------------------
# Component scorers
# ---------------------------------------------------------------------------

class ComponentResult:
    __slots__ = ("name", "fact_name", "weight", "points", "triggered",
                 "detail", "claim_id", "fact_present")

    def __init__(self, name: str, fact_name: str, weight: float) -> None:
        self.name = name
        self.fact_name = fact_name
        self.weight = weight
        self.points = 0.0
        self.triggered = False
        self.detail = ""
        self.claim_id: Optional[str] = None
        self.fact_present = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "component": self.name,
            "fact_name": self.fact_name,
            "weight": self.weight,
            "points": round(self.points, 6),
            "triggered": self.triggered,
            "detail": self.detail,
            "claim_id": self.claim_id,
            "fact_present": self.fact_present,
        }


# Each scorer: (value, result) -> None, mutating result in place.
# Contract: set points within [0, weight], set triggered + detail when the
# component warrants a red flag.

def score_form4_volume(value: Any, r: ComponentResult) -> None:
    n = coerce_count(value)
    r.points = r.weight * min(n, FORM4_VOLUME_CAP) / FORM4_VOLUME_CAP
    r.triggered = n >= FORM4_VOLUME_TRIGGER
    r.detail = (
        f"{n} Form 4 insider transaction(s) in trailing 12 months "
        f"(cap {FORM4_VOLUME_CAP}, trigger {FORM4_VOLUME_TRIGGER})"
    )


def score_auditor_change(value: Any, r: ComponentResult) -> None:
    changed = as_bool(value)
    r.points = r.weight if changed else 0.0
    r.triggered = changed
    r.detail = "Auditor change disclosed" if changed else "No auditor change disclosed"


def score_high_severity_8k(value: Any, r: ComponentResult) -> None:
    n = coerce_count(value)
    r.points = r.weight * min(n, HIGH_SEVERITY_8K_CAP) / HIGH_SEVERITY_8K_CAP
    r.triggered = n >= HIGH_SEVERITY_8K_TRIGGER
    r.detail = (
        f"{n} high-severity 8-K item(s) (e.g. 4.02 non-reliance, "
        f"4.01 auditor change) in trailing 12 months "
        f"(cap {HIGH_SEVERITY_8K_CAP})"
    )


def score_late_filing(value: Any, r: ComponentResult) -> None:
    n = coerce_count(value)
    r.points = r.weight * min(n, LATE_FILING_CAP) / LATE_FILING_CAP
    r.triggered = n >= LATE_FILING_TRIGGER
    r.detail = f"{n} NT (late filing) notice(s) in trailing 12 months (cap {LATE_FILING_CAP})"


def score_net_income(value: Any, r: ComponentResult) -> None:
    ni = as_number(value)
    if ni is None:
        r.detail = f"net income value not numeric: {value!r}"
        return
    loss = ni < 0
    r.points = r.weight if loss else 0.0
    r.triggered = loss
    r.detail = f"Net income {ni:,.0f} ({'net loss' if loss else 'profitable'})"


def score_accumulated_deficit(value: Any, r: ComponentResult) -> None:
    # Accept either a boolean flag or retained earnings < 0.
    if isinstance(value, (bool, str)) and not _looks_numeric(value):
        deficit = as_bool(value)
    else:
        re_ = as_number(value)
        deficit = re_ is not None and re_ < 0
    r.points = r.weight if deficit else 0.0
    r.triggered = deficit
    r.detail = (
        f"Accumulated deficit / negative retained earnings (value={value!r})"
        if deficit else f"Retained earnings non-negative (value={value!r})"
    )


def score_stockholders_equity(value: Any, r: ComponentResult) -> None:
    eq = as_number(value)
    if eq is None:
        r.detail = f"stockholders' equity value not numeric: {value!r}"
        return
    negative = eq < 0
    r.points = r.weight if negative else 0.0
    r.triggered = negative
    r.detail = (
        f"Stockholders' equity {eq:,.0f} "
        f"({'NEGATIVE — balance-sheet insolvency' if negative else 'positive'})"
    )


def score_going_concern_flag(value: Any, r: ComponentResult) -> None:
    flagged = as_bool(value)
    r.points = r.weight if flagged else 0.0
    r.triggered = flagged
    r.detail = (
        "Going-concern qualification in auditor opinion"
        if flagged else "No going-concern qualification"
    )


def _looks_numeric(value: Any) -> bool:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return True
    if isinstance(value, str):
        try:
            float(value.replace(",", "").strip())
            return True
        except ValueError:
            return False
    return False


COMPONENT_SCORERS: dict[str, Callable[[Any, ComponentResult], None]] = {
    "form4_volume": score_form4_volume,
    "auditor_change": score_auditor_change,
    "high_severity_8k": score_high_severity_8k,
    "late_filing": score_late_filing,
    "net_income": score_net_income,
    "accumulated_deficit": score_accumulated_deficit,
    "stockholders_equity": score_stockholders_equity,
    "going_concern_flag": score_going_concern_flag,
}

assert set(COMPONENT_SCORERS) == set(EXPECTED_FACTS), (
    "scorer registry drifted from EXPECTED_FACTS"
)


# ---------------------------------------------------------------------------
# Score computation
# ---------------------------------------------------------------------------

class CompanyScore:
    def __init__(self, slug: str, weights_version: str) -> None:
        self.slug = slug
        self.weights_version = weights_version
        self.weights = VERSIONED_WEIGHTS[weights_version]
        # RAW_MAX — derived, never hardcoded. Every component cannot exceed
        # its weight by construction of the scorers above.
        self.raw_max: float = sum(self.weights.values())
        self.components: list[ComponentResult] = []
        self.input_claim_ids: list[str] = []
        self.missing_facts: list[str] = []
        self.score_id: str = ""
        self.raw_score: float = 0.0
        self.score: float = 0.0
        self.score_artifact_id: str = ""
        self.report: dict[str, Any] = {}


def compute_company_score(
    slug: str,
    claims: list[FactClaim],
    weights_version: str,
    computed_at: str,
) -> CompanyScore:
    cs = CompanyScore(slug, weights_version)

    # Latest claim per fact name wins (facts are idempotent; a re-emitted
    # corrected fact supersedes an earlier one in the same stream).
    by_fact: dict[str, FactClaim] = {}
    for claim in claims:
        by_fact[claim.fact_name] = claim  # type: ignore[index]

    cs.input_claim_ids = sorted(c.claim_id for c in claims if c.claim_id)

    for fact_name in EXPECTED_FACTS:
        weight = cs.weights.get(fact_name)
        if weight is None:
            # Fact expected by the analyzer but not weighted in this version —
            # record it for the report but contribute nothing.
            continue
        r = ComponentResult(name=fact_name, fact_name=fact_name, weight=weight)
        claim = by_fact.get(fact_name)
        if claim is None:
            cs.missing_facts.append(fact_name)
            r.detail = "fact absent from input stream; contributing 0"
        else:
            r.fact_present = True
            r.claim_id = claim.claim_id
            try:
                COMPONENT_SCORERS[fact_name](claim.value, r)
            except Exception as exc:  # never let one bad fact sink the run
                log.warning("scorer %s failed on claim %s: %s",
                            fact_name, claim.claim_id, exc)
                r.points = 0.0
                r.triggered = False
                r.detail = f"scorer error: {exc}"
        # Hard invariant: no component may exceed its weight.
        r.points = max(0.0, min(r.points, r.weight))
        cs.components.append(r)

    cs.raw_score = sum(r.points for r in cs.components)
    cs.score = 0.0 if cs.raw_max <= 0 else round(
        cs.raw_score / cs.raw_max * SCORE_SCALE, 4
    )

    # Content-addressed score ID: stable for identical inputs + weights.
    id_digest = sha256_hex(canonical_json({
        "input_claim_ids": cs.input_claim_ids,
        "weights_version": weights_version,
    }))[:12]
    cs.score_id = f"{slug}-score-{id_digest}"

    weights_hash = sha256_hex(canonical_json({
        "version": weights_version,
        "weights": cs.weights,
    }))

    red_flags = [r for r in cs.components if r.triggered]

    cs.report = {
        "report_type": "forensic_score_report/v1",
        "generator": GENERATOR,
        "score_id": cs.score_id,
        "company_slug": slug,
        "score": cs.score,
        "scale": SCORE_SCALE,
        "raw_score": round(cs.raw_score, 6),
        "raw_max": cs.raw_max,
        "weights_version": weights_version,
        "weights_hash": weights_hash,
        "weights": cs.weights,
        "components": [r.to_dict() for r in cs.components],
        "red_flags": [
            {"component": r.name, "points": round(r.points, 6), "detail": r.detail}
            for r in red_flags
        ],
        "input_claims": cs.input_claim_ids,
        "input_claim_count": len(cs.input_claim_ids),
        "missing_facts": cs.missing_facts,
        "computed_at": computed_at,
    }

    # Hash-verified artifact: content bytes -> sha256 -> artifact ID.
    # NOTE: serialize a COPY without _artifact to avoid circular ref.
    report_for_hash = {k: v for k, v in cs.report.items() if k != "_artifact"}
    report_bytes = canonical_json(report_for_hash).encode("utf-8")
    report_sha = sha256_hex(report_bytes)
    cs.score_artifact_id = f"sha256:{report_sha}"
    cs.report["_artifact"] = {
        "id": cs.score_artifact_id,
        "kind": "score_report",
        "media_type": "application/json",
        "sha256": report_sha,
        "byte_count": len(report_bytes),
    }
    return cs


# ---------------------------------------------------------------------------
# Output emission
# ---------------------------------------------------------------------------

class NdjsonEmitter:
    def __init__(self, out) -> None:
        self.out = out
        self.count = 0

    def emit(self, record: dict[str, Any]) -> None:
        self.out.write(canonical_json(record) + "\n")
        self.count += 1

    def flush(self) -> None:
        self.out.flush()


def emit_company(emitter: NdjsonEmitter, cs: CompanyScore) -> None:
    # 1) Score report ARTIFACT — hash-addressable, verification is
    #    sha256(content bytes) == artifact.id suffix.
    emitter.emit({"artifact": cs.report["_artifact"]})

    # 2) forensic_score entity — evidence=derived, confidence=1.0
    #    (fully deterministic transform of the input claims). Basis is
    #    machine-resolvable: artifact ID + input claim IDs + weights version.
    score_entity = {
        "id": cs.score_id,
        "kind": "forensic_score",
        "slug": f"{cs.slug}-forensic-score",
        "name": f"Forensic risk score — {cs.slug}",
        "attributes": {
            "company_slug": cs.slug,
            "score": cs.score,
            "scale": SCORE_SCALE,
            "raw_score": round(cs.raw_score, 6),
            "raw_max": cs.raw_max,
            "weights_version": cs.weights_version,
            "red_flag_count": len(cs.report["red_flags"]),
            "missing_facts": cs.missing_facts,
        },
    }
    score_basis = {
        "score_artifact": cs.score_artifact_id,
        "input_claims": cs.input_claim_ids,
        "weights_version": cs.weights_version,
        "weights_hash": cs.report["weights_hash"],
    }
    emitter.emit({
        "id": cs.score_id,
        "entity": score_entity,
        "evidence": "derived",
        "confidence": 1.0,
        "basis": score_basis,
    })

    # 3) red_flag entities — one per triggered component, stable hash ID.
    for r in cs.components:
        if not r.triggered:
            continue
        digest = sha256_hex(canonical_json({
            "slug": cs.slug,
            "component": r.name,
            "claim_id": r.claim_id,
            "weights_version": cs.weights_version,
        }))[:12]
        flag_id = f"{cs.slug}-flag-{r.name.replace('_', '-')}-{digest}"
        flag_entity = {
            "id": flag_id,
            "kind": "forensic_red_flag",
            "slug": f"{cs.slug}-flag-{r.name.replace('_', '-')}",
            "name": f"Red flag: {r.name} — {cs.slug}",
            "attributes": {
                "company_slug": cs.slug,
                "component": r.name,
                "points": round(r.points, 6),
                "weight": r.weight,
                "detail": r.detail,
                "score_id": cs.score_id,
            },
        }
        emitter.emit({
            "id": flag_id,
            "entity": flag_entity,
            "evidence": "derived",
            "confidence": 1.0,
            "basis": {
                "score_id": cs.score_id,
                "score_artifact": cs.score_artifact_id,
                "source_claim": r.claim_id,
                "component": r.name,
                "weights_version": cs.weights_version,
            },
        })


# ---------------------------------------------------------------------------
# Pipeline driver
# ---------------------------------------------------------------------------

def run(in_stream: Iterable[str], emitter: NdjsonEmitter, weights_version: str) -> int:
    if weights_version not in VERSIONED_WEIGHTS:
        log.error("unknown weights version %r; available: %s",
                  weights_version, sorted(VERSIONED_WEIGHTS))
        return 1

    by_slug: dict[str, list[FactClaim]] = {}
    line_no = 0
    skipped = 0

    for line in in_stream:
        line_no += 1
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            log.warning("line %d: not valid JSON (%s); skipped", line_no, exc)
            skipped += 1
            continue
        if not isinstance(raw, dict) or ("entity" not in raw and "basis" not in raw):
            # Artifact records or anything claimless in a mixed stream pass
            # through this analyzer untouched by design.
            skipped += 1
            continue
        claim = FactClaim(raw, line_no)
        if not claim.usable:
            log.warning("line %d: claim lacks resolvable fact name or slug "
                        "(id=%s); skipped", line_no, claim.claim_id)
            skipped += 1
            continue
        if claim.fact_name not in COMPONENT_SCORERS:
            log.info("line %d: unmodeled fact %r ignored (claim %s)",
                     line_no, claim.fact_name, claim.claim_id)
            skipped += 1
            continue
        by_slug.setdefault(claim.slug, []).append(claim)  # type: ignore[arg-type]

    if not by_slug:
        log.error("no usable fact claims on stdin (%d lines read, %d skipped)",
                  line_no, skipped)
        return 2

    # One timestamp for the whole run keeps same-run artifacts comparable.
    # (The SCORE is deterministic; computed_at is metadata, not an input —
    # score IDs and score values never depend on it.)
    computed_at = now_utc()

    for slug in sorted(by_slug):
        claims = by_slug[slug]
        cs = compute_company_score(slug, claims, weights_version, computed_at)
        emit_company(emitter, cs)
        log.info(
            "%s: score=%.2f/%d raw=%.2f/%.0f flags=%d missing=%s claims=%d",
            slug, cs.score, SCORE_SCALE, cs.raw_score, cs.raw_max,
            len(cs.report["red_flags"]), cs.missing_facts or "[]",
            len(claims),
        )

    emitter.flush()
    log.info("emitted %d NDJSON records (%d skipped input lines)",
             emitter.count, skipped)
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compute versioned forensic risk scores from NDJSON fact claims.",
    )
    parser.add_argument(
        "--weights-version",
        default=DEFAULT_WEIGHTS_VERSION,
        choices=sorted(VERSIONED_WEIGHTS),
        help="scoring weights version (default: %(default)s)",
    )
    parser.add_argument(
        "--list-weights", action="store_true",
        help="print all registered weight versions as JSON and exit",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="debug logging on stderr",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        stream=sys.stderr,
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(name)s %(levelname)s: %(message)s",
    )

    if args.list_weights:
        print(json.dumps(VERSIONED_WEIGHTS, indent=2, sort_keys=True))
        return 0

    emitter = NdjsonEmitter(sys.stdout)
    try:
        return run(sys.stdin, emitter, args.weights_version)
    except BrokenPipeError:
        # Consumer closed the pipe (e.g. `| head`); exit quietly.
        try:
            sys.stdout.close()
        finally:
            return 0
    except Exception:
        log.exception("fatal error")
        return 1


if __name__ == "__main__":
    sys.exit(main())
