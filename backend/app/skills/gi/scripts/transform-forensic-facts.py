#!/usr/bin/env python3
"""transform-forensic-facts — GI ingest transform: raw forensic facts from SEC EDGAR.

PURE INGEST. This transform fetches SEC submissions + XBRL companyfacts and
emits FACT claims only. It does NOT compute any risk score — scoring lives in
forensic_analytics.py, which reads these facts back out of the graph and
applies versioned weights. Separation of concerns:

  - This file: network I/O, parsing, dedup, normalization, coverage accounting.
  - forensic_analytics.py: weights, thresholds, scoring, score-claim emission.

Fact predicates emitted (subj = company slug):
  forensic_fact/form4_count                    int    Form 4 filings in recent window
  forensic_fact/auditor_change_count           int    8-K Item 4.01 count
  forensic_fact/high_severity_8k_count         int    high-severity 8-K items
  forensic_fact/late_filing_count              int    NT-filed 10-K/Q count
  forensic_fact/other_events_8k_count          int    8-K Item 8.01 count
  forensic_fact/net_income_latest              float  latest FY net income (USD)
  forensic_fact/net_income_prior               float  prior FY net income (USD)
  forensic_fact/swung_to_loss                  0/1    prior>0 and latest<0
  forensic_fact/losses_deepening               0/1    latest<prior<0
  forensic_fact/revenue_decline_pct            float  YoY revenue decline, if any
  forensic_fact/going_concern_flag             0/1    XBRL GoingConcernFlag
  forensic_fact/accumulated_deficit_negative   0/1    retained earnings < 0
  forensic_fact/accumulated_deficit_growing    0/1    deficit deepening YoY
  forensic_fact/negative_equity                0/1    stockholders' equity < 0

Coverage claims emitted (so analytics can refuse to score a failed fetch):
  forensic_coverage/sources                    json   {"submissions": "...", "facts": "..."}
  forensic_coverage/<component>                status populated|empty|unavailable

Bug fixes vs. the old transform-forensic-score.py:
  1. Late filings: SEC form names are "NT 10-K" (space), not "NT-10-K".
     Matched via regex allowing space, hyphen, or nothing.
  2. RetainedEarnings fallback was dead code: the real us-gaap concept is
     RetainedEarningsAccumulatedDeficit. Fixed.
  3. GoingConcernFlag never fired because companyfacts may store booleans
     under non-USD unit keys. Now iterates ALL unit keys.
  4. Restated annuals double-counted: companyfacts returns every historical
     restatement for the same period. Facts are now deduped by `end` date,
     keeping the entry with the latest `filed` timestamp.
  5. A failed fetch previously produced score=0 — indistinguishable from a
     clean company. Fetch failures now emit coverage=unavailable claims and
     NO facts, so the analytics layer refuses to score.

Usage:
  gi run transform-forensic-facts --entity "Marqeta"

Written by Kimi K3 (Synthetic) as part of the crew's Top 5 build.
Refactored to separate ingest (facts) from analytics (scoring).
"""
import hashlib
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

UA = "gi-transform-forensic-facts/1.0 (+research; sample@example.com)"
TICKER_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"

# High-severity 8-K items: bankruptcy-adjacent, delisting notice, auditor
# change, leadership turnover.
HIGH_SEV_ITEMS = {"1.02", "1.03", "1.05", "2.04", "3.01", "4.01", "5.01"}

# Late-filing form names as they ACTUALLY appear in SEC submissions data:
# "NT 10-K", "NT 10-Q", "NT 11-K", occasionally "NT-10-K". Allow any separator.
LATE_FORM_RE = re.compile(r"^NT[\s-]*(10|11)[\s-]*[KQ]\b", re.IGNORECASE)


def normalize_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def slugify(name: str) -> str:
    s = name.casefold()
    s = "".join(c if c.isalnum() else "-" for c in s)
    return re.sub(r"-+", "-", s).strip("-")


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")


def http_get_raw(url: str) -> bytes:
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def lookup_cik(name_or_ticker: str) -> int | None:
    try:
        ticker_map = json.loads(http_get_raw(TICKER_URL))
    except Exception as e:
        print(f"WARNING: failed to fetch ticker map: {e}", file=sys.stderr)
        return None
    query = normalize_ws(name_or_ticker)
    for _, info in ticker_map.items():
        if info["ticker"].upper() == query.upper():
            return info["cik_str"]
    for _, info in ticker_map.items():
        if info["title"].lower() == query.lower():
            return info["cik_str"]
    best_cik = None
    best_score = 0
    for _, info in ticker_map.items():
        title = info["title"].lower()
        if query.lower() in title or title in query.lower():
            score = len(query) / max(len(title), 1)
            if score > best_score:
                best_score = score
                best_cik = info["cik_str"]
    return best_cik


# ── XBRL helpers ───────────────────────────────────────────────────────────

def fact_units(us_gaap: dict, concept: str) -> list[dict]:
    """Return ALL entries for a concept across EVERY unit key.

    Fix: the old code only looked under units["USD"] (and one "" fallback),
    so boolean concepts like GoingConcernFlag — which companyfacts stores
    under arbitrary unit keys — never matched.
    """
    out = []
    for entries in us_gaap.get(concept, {}).get("units", {}).values():
        out.extend(entries)
    return out


def annual_entries(us_gaap: dict, concepts: list[str],
                   require_start: bool = True) -> list[dict]:
    """Latest-filed-wins annual facts, deduped by period end date.

    Fix: companyfacts returns one row per restatement, so the same fiscal
    year can appear 3+ times. The old code sorted by `end` and took [0]/[1],
    silently mixing restated and original numbers or comparing a year to
    itself. We dedupe by (end) keeping the row with the newest `filed`.
    Balance-sheet/instant concepts have no `start`; flow concepts do.
    """
    for concept in concepts:
        entries = fact_units(us_gaap, concept)
        annual = [e for e in entries
                  if e.get("form") in ("10-K", "10-K/A")
                  and e.get("fp") == "FY"
                  and isinstance(e.get("val"), (int, float))
                  and e.get("end")
                  and (e.get("start") or not require_start)]
        if annual:
            best = {}
            for e in annual:
                prev = best.get(e["end"])
                if prev is None or e.get("filed", "") >= prev.get("filed", ""):
                    best[e["end"]] = e
            return sorted(best.values(), key=lambda e: e["end"], reverse=True)
    return []


# ── Claim emission ─────────────────────────────────────────────────────────

def emit_claim(subj: str, pred: str, obj: str, basis: str = "",
               confidence: float = 0.95) -> None:
    emit({"type": "claim", "subj": subj, "pred": pred, "obj": obj,
          "polarity": "supports", "evidence": "documented",
          "confidence": confidence, "basis": basis})


def emit_fact(subj: str, name: str, value, basis: str,
              artifact_hash: str, detail: dict | None = None) -> None:
    """Emit one forensic_fact claim with a machine-resolvable basis citing
    the artifact hash, so analytics-layer claims can trace provenance."""
    basis_payload = {
        "fact": name,
        "source_hash": artifact_hash,
        "detail": detail or {},
        "summary": basis,
    }
    emit_claim(subj, f"forensic_fact/{name}", str(value),
               json.dumps(basis_payload, ensure_ascii=False, sort_keys=True))


def emit_coverage(subj: str, component: str, status: str,
                  artifact_hash: str | None, signal_count: int = 0) -> None:
    emit_claim(subj, f"forensic_coverage/{component}", status,
               json.dumps({"component": component,
                           "artifact": artifact_hash,
                           "signal_count": signal_count},
                          sort_keys=True))


def store_artifact(raw: bytes, url: str, evidence_dir: Path,
                   tag: str) -> str:
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    tmp = evidence_dir / f"tmp-forensic-{tag}-{os.getpid()}-{int(time.time())}.json"
    tmp.write_bytes(raw)
    emit({"type": "artifact", "uri": url,
          "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
          "file": str(tmp), "hash": digest})
    return digest


# ── Fact extraction: submissions-derived ───────────────────────────────────

def recent_filings(recent: dict) -> list[dict]:
    """Zip the columnar `recent` arrays into row dicts, tolerating ragged
    lengths (the old code silently dropped/misaligned rows via index guards)."""
    cols = {k: recent.get(k, []) for k in
            ("form", "items", "filingDate", "accessionNumber")}
    n = len(cols["form"])
    rows = []
    for i in range(n):
        rows.append({k: (v[i] if i < len(v) else "") for k, v in cols.items()})
    return rows


def extract_submissions_facts(subj: str, recent: dict,
                              artifact_hash: str) -> dict[str, str]:
    coverage = {}
    rows = recent_filings(recent)
    base = f"SEC submissions artifact {artifact_hash}"

    # Insider activity (component: insider_activity)
    form4_count = sum(1 for r in rows if r["form"] == "4")
    emit_fact(subj, "form4_count", form4_count,
              f"{form4_count} Form 4 filings in recent window",
              artifact_hash)
    coverage["insider_activity"] = ("populated", form4_count)

    # Auditor changes (component: auditor_changes)
    auditor_items = [r for r in rows if r["form"] == "8-K" and "4.01" in r["items"]]
    emit_fact(subj, "auditor_change_count", len(auditor_items),
              f"{len(auditor_items)} 8-K Item 4.01 (Change in Accountant) filings",
              artifact_hash,
              {"accessions": [r["accessionNumber"] for r in auditor_items]})
    coverage["auditor_changes"] = ("populated", len(auditor_items))

    # Material events (component: material_events)
    high_sev = [r for r in rows if r["form"] == "8-K" and any(
        it.strip() in HIGH_SEV_ITEMS for it in r["items"].split(","))]
    emit_fact(subj, "high_severity_8k_count", len(high_sev),
              f"{len(high_sev)} high-severity 8-K events",
              artifact_hash,
              {"accessions": [r["accessionNumber"] for r in high_sev],
               "high_sev_items": sorted(HIGH_SEV_ITEMS)})
    coverage["material_events"] = ("populated", len(high_sev))

    # Filing anomalies (component: filing_anomalies)
    # FIX: match "NT 10-K" (with space) — the old startswith("NT-") never matched.
    late = [r for r in rows if LATE_FORM_RE.match(r["form"] or "")]
    emit_fact(subj, "late_filing_count", len(late),
              f"{len(late)} non-timely filings (NT 10-K/Q)",
              artifact_hash,
              {"forms": [r["form"] for r in late]})
    coverage["filing_anomalies"] = ("populated", len(late))

    # Litigation/other-events proxy (component: litigation_signals)
    other = [r for r in rows if r["form"] == "8-K" and "8.01" in r["items"]]
    emit_fact(subj, "other_events_8k_count", len(other),
              f"{len(other)} 8-K Item 8.01 'other events' filings",
              artifact_hash)
    coverage["litigation_signals"] = ("populated", len(other))

    for comp, (status, n) in coverage.items():
        emit_coverage(subj, comp, status, artifact_hash, n)
    print(f"Forensic facts: submissions-derived facts emitted from {len(rows)} filings",
          file=sys.stderr)
    return {}


# ── Fact extraction: companyfacts-derived ─────────────────────────────────

def extract_financial_facts(subj: str, facts: dict,
                            artifact_hash: str) -> None:
    us_gaap = facts.get("facts", {}).get("us-gaap", {})
    base = f"SEC companyfacts artifact {artifact_hash}"
    fin_signals = 0
    gc_signals = 0

    # ── Net income trend (component: financial_deterioration) ──
    ni = annual_entries(us_gaap, ["NetIncomeLoss", "ProfitLoss"])
    if len(ni) >= 2:
        latest, prior = ni[0]["val"], ni[1]["val"]
        emit_fact(subj, "net_income_latest", latest,
                  f"Net income FY ending {ni[0]['end']}: ${latest/1e6:.1f}M "
                  f"(filed {ni[0].get('filed', '?')}, latest restatement)",
                  artifact_hash)
        emit_fact(subj, "net_income_prior", prior,
                  f"Net income FY ending {ni[1]['end']}: ${prior/1e6:.1f}M",
                  artifact_hash)
        emit_fact(subj, "swung_to_loss", int(prior > 0 > latest),
                  f"Swung to loss: {'yes' if prior > 0 > latest else 'no'}",
                  artifact_hash)
        emit_fact(subj, "losses_deepening", int(latest < prior < 0),
                  f"Losses deepening YoY: {'yes' if latest < prior < 0 else 'no'}",
                  artifact_hash)
        fin_signals += 4
    elif ni:
        emit_fact(subj, "net_income_latest", ni[0]["val"],
                  f"Net income FY ending {ni[0]['end']} (single year available)",
                  artifact_hash)
        fin_signals += 1

    # ── Revenue trend ──
    rev = annual_entries(us_gaap,
                         ["RevenueFromContractWithCustomerExcludingAssessedTax",
                          "RevenueFromContractWithCustomerExcludingAssessableTax",
                          "Revenues", "SalesRevenueNet"])
    if len(rev) >= 2 and rev[1]["val"] > 0:
        pct = (1 - rev[0]["val"] / rev[1]["val"]) * 100
        emit_fact(subj, "revenue_decline_pct", round(max(pct, 0.0), 2),
                  f"Revenue {'declined' if pct > 0 else 'grew'} {abs(pct):.1f}% YoY "
                  f"(FY{rev[1]['end']} → FY{rev[0]['end']})",
                  artifact_hash)
        fin_signals += 1

    # ── Going-concern flag (component: going_concern) ──
    # FIX: search ALL unit keys; old code only tried units["USD"] and units[""],
    # so boolean GoingConcernFlag facts effectively never fired.
    gc_entries = fact_units(us_gaap, "GoingConcernFlag")
    gc_flag = 0
    if gc_entries:
        latest_gc = max(gc_entries, key=lambda e: (e.get("end", ""), e.get("filed", "")))
        val = latest_gc.get("val")
        gc_flag = 1 if val in (1, True, "1", "true") else 0
    emit_fact(subj, "going_concern_flag", gc_flag,
              "XBRL GoingConcernFlag asserted" if gc_flag else
              ("XBRL GoingConcernFlag present but not asserted" if gc_entries else
               "No GoingConcernFlag concept in companyfacts (common — GC opinions "
               "live in the audit report text, not XBRL numeric facts; rely on "
               "equity/deficit proxies below)"),
              artifact_hash)
    gc_signals += 1

    # ── Accumulated deficit ──
    # FIX: correct us-gaap concept is RetainedEarningsAccumulatedDeficit.
    # The old fallback looked up "RetainedEarnings" — not a companyfacts concept
    # under that bare name — so the fallback branch was dead code.
    deficit = annual_entries(us_gaap,
                             ["RetainedEarningsAccumulatedDeficit"],
                             require_start=False)
    if deficit:
        latest_d = deficit[0]["val"]
        emit_fact(subj, "accumulated_deficit_negative", int(latest_d < 0),
                  f"Retained earnings/accumulated deficit FY{deficit[0]['end']}: "
                  f"${latest_d/1e6:.1f}M",
                  artifact_hash)
        gc_signals += 1
        if len(deficit) >= 2 and latest_d < 0:
            growing = latest_d < deficit[1]["val"]
            emit_fact(subj, "accumulated_deficit_growing", int(growing),
                      f"Accumulated deficit {'growing' if growing else 'shrinking'} "
                      f"YoY (${deficit[1]['val']/1e6:.1f}M → ${latest_d/1e6:.1f}M)",
                      artifact_hash)
            gc_signals += 1

    # ── Stockholders' equity (instant concept — no start date) ──
    eq = annual_entries(us_gaap,
                        ["StockholdersEquity",
                         "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
                        require_start=False)
    if eq:
        emit_fact(subj, "negative_equity", int(eq[0]["val"] < 0),
                  f"Stockholders' equity FY{eq[0]['end']}: ${eq[0]['val']/1e6:.1f}M",
                  artifact_hash)
        gc_signals += 1

    emit_coverage(subj, "financial_deterioration",
                  "populated" if fin_signals else "empty", artifact_hash, fin_signals)
    emit_coverage(subj, "going_concern",
                  "populated" if gc_signals else "empty", artifact_hash, gc_signals)
    print(f"Forensic facts: XBRL-derived facts emitted "
          f"(fin={fin_signals}, gc={gc_signals})", file=sys.stderr)


def emit_component_coverage_unavailable(subj: str, components: list[str],
                                        reason: str) -> None:
    """A failed fetch must NEVER be readable as a clean company."""
    for comp in components:
        emit_coverage(subj, comp, "unavailable: " + reason, None, 0)


# ── Main ────────────────────────────────────────────────────────────────────

SUBMISSIONS_COMPONENTS = ["insider_activity", "auditor_changes",
                          "material_events", "filing_anomalies",
                          "litigation_signals"]
FACTS_COMPONENTS = ["financial_deterioration", "going_concern"]


def main() -> int:
    payload = json.load(sys.stdin)
    ent = payload["entity"]
    evidence_dir = Path(payload["evidence_dir"])
    name = normalize_ws(ent.get("name") or ent.get("id", ""))

    print(f"Forensic facts: looking up '{name}'...", file=sys.stderr)
    cik = lookup_cik(name)
    if not cik:
        print(f"WARNING: could not find '{name}' on SEC EDGAR", file=sys.stderr)
        return 0
    cik_padded = str(cik).zfill(10)
    print(f"Forensic facts: CIK {cik_padded}", file=sys.stderr)

    # ── Submissions (required) ──
    sub_url = SUBMISSIONS_URL.format(cik=cik_padded)
    try:
        sub_raw = http_get_raw(sub_url)
        sub_json = json.loads(sub_raw)
        sub_hash = store_artifact(sub_raw, sub_url, evidence_dir, "sub")
        submissions_status = "ok"
    except Exception as e:
        sub_raw, sub_json, sub_hash = None, None, None
        submissions_status = f"unavailable: {e}"
        print(f"WARNING: submissions fetch failed: {e}", file=sys.stderr)

    company_name = (sub_json or {}).get("name", name)
    company_slug = slugify(company_name)

    # ── Companyfacts (optional, but tracked) ──
    facts_url = FACTS_URL.format(cik=cik_padded)
    try:
        print("Forensic facts: fetching companyfacts...", file=sys.stderr)
        facts_raw = http_get_raw(facts_url)
        facts = json.loads(facts_raw)
        facts_hash = store_artifact(facts_raw, facts_url, evidence_dir, "facts")
        facts_status = "ok"
    except Exception as e:
        facts_raw, facts, facts_hash = None, None, None
        facts_status = f"unavailable: {e}"
        print(f"WARNING: facts fetch failed: {e}", file=sys.stderr)

    # Source-level coverage claim FIRST, so analytics can gate on it even if
    # component-level claims are lost downstream.
    emit_claim(company_slug, "forensic_coverage/sources",
               json.dumps({"submissions": submissions_status,
                           "facts": facts_status}, sort_keys=True),
               json.dumps({"submissions_url": sub_url, "facts_url": facts_url,
                           "submissions_hash": sub_hash, "facts_hash": facts_hash},
                          sort_keys=True))

    if sub_json is not None:
        recent = sub_json.get("filings", {}).get("recent", {})
        extract_submissions_facts(company_slug, recent, sub_hash)
    else:
        emit_component_coverage_unavailable(
            company_slug, SUBMISSIONS_COMPONENTS, submissions_status)

    if facts is not None:
        extract_financial_facts(company_slug, facts, facts_hash)
    else:
        emit_component_coverage_unavailable(
            company_slug, FACTS_COMPONENTS, facts_status)

    print("Forensic facts: done (ingest only — run forensic_analytics.py to score).",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
