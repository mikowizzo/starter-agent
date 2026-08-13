#!/usr/bin/env python3
"""transform-sec — GI transform: SEC EDGAR filings & financials → NDJSON.

Contract (transforms are standalone executables; they NEVER touch the case
file): read one entity JSON on stdin, write NDJSON to stdout, drop fetched
artifacts where the runner tells you to.

stdin:  {"entity": {"id": ..., "name": ...}, "evidence_dir": "...", "config": {...}}
stdout: {"type":"artifact", "uri":..., "fetched_at":..., "file":<tmp>, "hash":"sha256:.."}
        {"type":"entity", "id":..., "name":..., "kind":..., ...}
        {"type":"claim", "subj":..., "pred":..., "obj":..., "evidence":"direct",
         "confidence":0.95, "cites":[{"artifact":"sha256:..","span":[s,e],"quote":"..."}]}

Sources (all free, public, no API key required):
  - company_tickers.json  — ticker/name → CIK lookup
  - submissions/CIK{10}.json — recent filings, company metadata
  - companyfacts/CIK{10}.json — XBRL financial facts (revenue, assets, etc.)

Evidence: SEC EDGAR IS the authoritative primary source → "direct", 0.95.
When a quote cannot be located in the artifact text, falls back gracefully
to "hypothesis" with a basis (per the transform contract — uncited claims
must be hypotheses).

SEC requirements:
  - User-Agent header with contact info (update UA constant below).
  - Rate limit: 10 requests/second (we make ≤3 per entity — fine).

Usage:
  gi run transform-sec --entity "Marqeta"
  gi run transform-sec --entity "Apple Inc."
  gi run transform-sec --entity "AAPL"
"""
import hashlib
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

# ── Configuration ───────────────────────────────────────────────────────────

# SEC requires a descriptive User-Agent with contact info.  UPDATE THIS.
UA = "gi-transform-sec/0.1 (+research; sample@example.com)"

TICKER_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"

# XBRL concepts to extract, in priority order (first match wins).
# Maps our metric key → list of possible us-gaap concept names.
FINANCIAL_CONCEPTS = {
    "revenue": [
        "RevenueFromContractWithCustomerExcludingAssessableTax",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
    ],
    "net_income": ["NetIncomeLoss"],
    "total_assets": ["Assets"],
    "total_liabilities": ["Liabilities"],
    "stockholders_equity": ["StockholdersEquity"],
    "cash": ["CashAndCashEquivalentsAtCarryingValue"],
    "long_term_debt": ["LongTermDebt", "LongTermDebtNoncurrent"],
    "operating_income": ["OperatingIncomeLoss"],
}

# Maps metric key → GI predicate name (must exist in vocab.toml).
PRED_FOR_METRIC = {
    "revenue": "reported_revenue",
    "net_income": "reported_net_income",
    "total_assets": "reported_total_assets",
    "total_liabilities": "reported_total_liabilities",
    "stockholders_equity": "reported_equity",
    "cash": "reported_cash",
    "long_term_debt": "reported_debt",
    "operating_income": "reported_operating_income",
}

# SEC form types we care about (skip the noise: Form 4, POS AM, etc.).
INTERESTING_FORMS = {
    "10-K", "10-K/A", "10-Q", "10-Q/A", "8-K",
    "S-1", "S-1/A", "20-F", "DEF 14A",
}
MAX_FILINGS = 15  # cap per run to keep the graph manageable


# ── Utilities (must match gi.py's contract) ─────────────────────────────────

def normalize_ws(s: str) -> str:
    """Must match gi.py's normalize_ws exactly — spans are in this space."""
    return re.sub(r"\s+", " ", s).strip()


def slugify(name: str) -> str:
    s = name.casefold()
    s = "".join(c if c.isalnum() else "-" for c in s)
    return re.sub(r"-+", "-", s).strip("-")


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")


# ── HTTP ────────────────────────────────────────────────────────────────────

def http_get_raw(url: str) -> bytes:
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


# ── CIK lookup ──────────────────────────────────────────────────────────────

def lookup_cik(name_or_ticker: str) -> int | None:
    """Look up SEC CIK by ticker (exact), company name (exact), or fuzzy."""
    try:
        ticker_map = json.loads(http_get_raw(TICKER_URL))
    except Exception as e:
        print(f"WARNING: failed to fetch ticker map: {e}", file=sys.stderr)
        return None

    query = name_or_ticker.strip()
    q_upper = query.upper()
    q_lower = query.lower()

    # 1. Exact ticker match ("AAPL", "aapl" → Apple)
    for _, info in ticker_map.items():
        if info["ticker"].upper() == q_upper:
            return info["cik_str"]

    # 2. Exact company-name match
    for _, info in ticker_map.items():
        if info["title"].lower() == q_lower:
            return info["cik_str"]

    # 3. Fuzzy: best contains-match (longer match = higher score)
    best_cik = None
    best_score = 0
    for _, info in ticker_map.items():
        title = info["title"].lower()
        if q_lower in title or title in q_lower:
            score = len(q_lower) / max(len(title), 1)
            if score > best_score:
                best_score = score
                best_cik = info["cik_str"]

    return best_cik


# ── Artifact storage ────────────────────────────────────────────────────────

def fetch_and_store(url: str, evidence_dir: Path, label: str):
    """Fetch URL, store raw bytes as artifact. Returns (data, digest, norm_text).

    The hash is computed from the raw HTTP response bytes — NOT from
    re-serialised JSON — so it matches exactly what the server sent.
    """
    raw = http_get_raw(url)
    tmp = evidence_dir / f"tmp-sec-{label}-{os.getpid()}-{int(time.time())}.json"
    tmp.write_bytes(raw)
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    emit({"type": "artifact", "uri": url,
          "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
          "file": str(tmp), "hash": digest})
    text = raw.decode("utf-8")
    return json.loads(text), digest, normalize_ws(text)


# ── Citation builder ────────────────────────────────────────────────────────

def make_cite(digest: str, norm_text: str, needle: str,
              context: int = 100) -> dict | None:
    """Find needle in normalized artifact text; return a cite dict or None.

    The quote is a window around the needle.  The span [start, end] covers
    the quote exactly, so the quote trivially exists "near" the span (the
    GI ingest check allows ±64 chars slack).
    """
    pos = norm_text.find(needle)
    if pos == -1:
        return None
    start = max(0, pos - context)
    end = min(len(norm_text), pos + len(needle) + context)
    return {"artifact": digest, "span": [start, end],
            "quote": norm_text[start:end]}


def emit_claim(subj: str, pred: str, obj: str, cite: dict | None,
               basis: str = "") -> None:
    """Emit a claim with proper evidence handling.

    If we have a verified cite → evidence "direct", confidence 0.95.
    If the quote couldn't be located → evidence "hypothesis", confidence 0.8,
    with a free-text basis (required by the contract for uncited claims).
    """
    claim: dict = {"type": "claim", "subj": subj, "pred": pred,
                   "obj": obj, "polarity": "supports"}
    if cite:
        claim["evidence"] = "direct"
        claim["confidence"] = 0.95
        claim["cites"] = [cite]
    else:
        claim["evidence"] = "hypothesis"
        claim["confidence"] = 0.8
        claim["basis"] = basis or f"Derived from SEC EDGAR data for {subj}"
    emit(claim)


# ── XBRL extraction ─────────────────────────────────────────────────────────

def extract_latest_annual(facts: dict, concept_names: list) -> dict | None:
    """Get the most recent annual (FY, 10-K) XBRL entry across ALL concepts.

    Companies sometimes switch XBRL concepts across years (e.g. Apple moved
    from ``Revenues`` to ``RevenueFromContractWithCustomerExcludingAssessableTax``
    around 2018).  Instead of returning the first concept that has data — which
    may be a stale concept with an old entry — we search ALL concept names,
    collect every qualifying annual entry, and return the single newest one.
    """
    us_gaap = facts.get("facts", {}).get("us-gaap", {})
    all_annual: list[dict] = []
    for concept in concept_names:
        data = us_gaap.get(concept)
        if not data:
            continue
        entries = data.get("units", {}).get("USD", [])
        # Prefer entries tagged FY from a 10-K
        annual = [
            e for e in entries
            if e.get("form") in ("10-K", "10-K/A")
            and "start" in e and "end" in e
            and e.get("fp") == "FY"
        ]
        # Widen: 10-K entries regardless of fp tag
        if not annual:
            annual = [
                e for e in entries
                if e.get("form") in ("10-K", "10-K/A")
                and "start" in e and "end" in e
            ]
        all_annual.extend(annual)
    if all_annual:
        all_annual.sort(key=lambda e: e["end"], reverse=True)
        return all_annual[0]
    return None


# ── Processors ──────────────────────────────────────────────────────────────

def process_company(sub: dict, company_slug: str, sub_digest: str,
                    sub_norm: str, cik_padded: str,
                    original_name: str) -> None:
    """Emit the company entity with SEC metadata, exchange + SIC claims."""
    name = sub.get("name", original_name)
    tickers = sub.get("tickers", [])
    exchanges = sub.get("exchanges", [])
    sic = sub.get("sic", "")
    sic_desc = sub.get("sicDescription", "")

    # External IDs — registry-keyed so GI can auto-resolve across transforms.
    ext_ids = {"cik": cik_padded}
    if tickers:
        ext_ids["ticker"] = tickers[0]
    if sic:
        ext_ids["sic"] = sic

    aliases = []
    if original_name and original_name.lower() != name.lower():
        aliases.append(original_name)
    for t in tickers:
        if t not in aliases:
            aliases.append(t)

    emit({"type": "entity", "id": company_slug, "name": name,
          "kind": "organization",
          "attrs": {
              "state_of_incorporation": sub.get("stateOfIncorporation", ""),
              "fiscal_year_end": sub.get("fiscalYearEnd", ""),
              "sic_description": sic_desc,
          },
          "external_ids": ext_ids,
          "aliases": aliases})

    # Exchange listing claim
    for exch in exchanges[:1]:  # primary exchange only
        exch_slug = slugify(exch)
        emit({"type": "entity", "id": exch_slug, "name": exch,
              "kind": "exchange", "attrs": {},
              "external_ids": {}, "aliases": []})
        cite = make_cite(sub_digest, sub_norm, exch)
        emit_claim(company_slug, "listed_on", exch_slug, cite,
                   f"Exchange listing from SEC EDGAR for {company_slug}")

    # SIC industry classification claim
    if sic:
        sic_id = f"sic-{sic}"
        emit({"type": "entity", "id": sic_id,
              "name": f"SIC {sic}: {sic_desc}",
              "kind": "industry_classification",
              "attrs": {"code": sic, "description": sic_desc},
              "external_ids": {"sic": sic}, "aliases": []})
        cite = make_cite(sub_digest, sub_norm, sic)
        emit_claim(company_slug, "has_sic_code", sic_id, cite,
                   f"SIC classification from SEC EDGAR for {company_slug}")


def process_filings(sub: dict, company_slug: str, sub_digest: str,
                    sub_norm: str) -> None:
    """Emit entities + claims for recent SEC filings (10-K, 10-Q, 8-K, etc.)."""
    recent = sub.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accns = recent.get("accessionNumber", [])
    docs = recent.get("primaryDocument", [])
    items = recent.get("items", [])  # 8-K item types (e.g. "2.02", "8.01")

    count = 0
    for i, form in enumerate(forms):
        if count >= MAX_FILINGS:
            break
        if form not in INTERESTING_FORMS:
            continue

        date = dates[i] if i < len(dates) else ""
        accn = accns[i] if i < len(accns) else ""
        doc = docs[i] if i < len(docs) else ""
        item = items[i] if i < len(items) else ""

        form_clean = form.lower().replace("/", "-").replace(" ", "-")
        filing_id = f"{company_slug}-{form_clean}-{date}"

        filing_attrs = {"form": form, "date": date, "document": doc}
        if accn:
            filing_attrs["accession"] = accn
        if item:
            filing_attrs["items"] = item

        emit({"type": "entity", "id": filing_id,
              "name": f"{form} filed {date}",
              "kind": "sec_filing",
              "attrs": filing_attrs,
              "external_ids": {"accession": accn} if accn else {},
              "aliases": [accn] if accn else []})

        # Cite the accession number (unique, stable, appears verbatim in JSON)
        needle = accn or date
        cite = make_cite(sub_digest, sub_norm, needle) if needle else None
        emit_claim(company_slug, "filed_form", filing_id, cite,
                   f"Filing from SEC EDGAR submissions for {company_slug}")
        count += 1

    if count == 0:
        print(f"  (no {INTERESTING_FORMS} filings found in recent batch)",
              file=sys.stderr)


def process_financials(facts: dict, company_slug: str, company_name: str,
                       facts_digest: str, facts_norm: str) -> None:
    """Emit entities + claims for key financial metrics from XBRL data.

    Extracts the most recent annual (10-K) value for each metric.
    TODO: also extract quarterly (10-Q) for momentum tracking.
    """
    metrics_found = 0

    for metric_key, concept_names in FINANCIAL_CONCEPTS.items():
        entry = extract_latest_annual(facts, concept_names)
        if not entry:
            continue

        val = entry.get("val")
        if val is None:
            continue

        end_date = entry.get("end", "unknown")
        fy = entry.get("fy") or (end_date[:4] if end_date else "?")
        accn = entry.get("accn", "")

        # Create the metric entity (becomes a traversable graph node)
        metric_id = f"{company_slug}-{metric_key}-fy{fy}"
        metric_label = metric_key.replace("_", " ").title()
        emit({"type": "entity", "id": metric_id,
              "name": f"{company_name} {metric_label} FY{fy}",
              "kind": "financial_metric",
              "attrs": {
                  "value": val,
                  "currency": "USD",
                  "period_start": entry.get("start", ""),
                  "period_end": end_date,
                  "form": entry.get("form", "10-K"),
                  "accession": accn,
              },
              "external_ids": {}, "aliases": []})

        # Cite: search for the value first, fall back to accession number.
        # The value proves the number; the accession proves which filing.
        cite = make_cite(facts_digest, facts_norm, str(val))
        if not cite and accn:
            cite = make_cite(facts_digest, facts_norm, accn)

        pred = PRED_FOR_METRIC[metric_key]
        emit_claim(company_slug, pred, metric_id, cite,
                   f"XBRL data from SEC EDGAR companyfacts for {company_slug}")
        metrics_found += 1

    if metrics_found == 0:
        print("  (no annual financial metrics found in companyfacts)",
              file=sys.stderr)
    else:
        print(f"  extracted {metrics_found} financial metrics", file=sys.stderr)


# ── Main ────────────────────────────────────────────────────────────────────

def main() -> int:
    payload = json.load(sys.stdin)
    ent = payload["entity"]
    evidence_dir = Path(payload["evidence_dir"])
    name = ent.get("name") or ent.get("id", "")

    # 1. CIK lookup (ticker → CIK or name → CIK)
    print(f"SEC: looking up '{name}'...", file=sys.stderr)
    cik = lookup_cik(name)
    if not cik:
        print(f"WARNING: could not find '{name}' on SEC EDGAR", file=sys.stderr)
        return 0
    cik_padded = str(cik).zfill(10)
    print(f"SEC: CIK {cik_padded}", file=sys.stderr)

    # 2. Fetch submissions (required — has filings + company metadata)
    try:
        print("SEC: fetching submissions...", file=sys.stderr)
        sub_data, sub_digest, sub_norm = fetch_and_store(
            SUBMISSIONS_URL.format(cik=cik_padded), evidence_dir, "submissions")
    except Exception as e:
        print(f"WARNING: SEC submissions fetch failed for CIK {cik_padded}: {e}",
              file=sys.stderr)
        return 0

    company_name = sub_data.get("name", name)
    company_slug = slugify(company_name)
    print(f"SEC: {company_name} ({company_slug})", file=sys.stderr)

    # 3. Emit company entity + exchange/SIC claims + filing claims
    process_company(sub_data, company_slug, sub_digest, sub_norm,
                    cik_padded, name)
    process_filings(sub_data, company_slug, sub_digest, sub_norm)

    # 4. Fetch company facts (optional — newer IPOs may lack XBRL data)
    try:
        print("SEC: fetching companyfacts (XBRL)...", file=sys.stderr)
        facts_data, facts_digest, facts_norm = fetch_and_store(
            FACTS_URL.format(cik=cik_padded), evidence_dir, "facts")
    except Exception as e:
        print(f"WARNING: SEC companyfacts fetch failed for CIK {cik_padded}: {e}",
              file=sys.stderr)
        facts_data = None

    # 5. Extract and emit financial metric claims
    if facts_data:
        process_financials(facts_data, company_slug, company_name,
                           facts_digest, facts_norm)

    print("SEC: done.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
