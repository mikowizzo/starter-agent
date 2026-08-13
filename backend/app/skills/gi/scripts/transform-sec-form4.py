#!/usr/bin/env python3
"""transform-sec-form4 — GI transform: SEC Form 4 insider transactions → NDJSON.

Contract (transforms are standalone executables; they NEVER touch the case
file): read one entity JSON on stdin, write NDJSON to stdout, drop fetched
artifacts where the runner tells you to.

stdin:  {"entity": {"id": ..., "name": ...}, "evidence_dir": "...", "config": {...}}
stdout: {"type":"artifact", ...}
        {"type":"entity", "id":..., "name":..., "kind":"person", ...}
        {"type":"claim", "subj":..., "pred":"insider_bought", "obj":...,
         "evidence":"direct", "confidence":0.95,
         "cites":[{"artifact":"sha256:..","span":[s,e],"quote":"..."}]}

Form 4 is filed when an insider (director, officer, or 10%+ holder) buys
or sells company stock.  This transform:

  1. Finds the issuer's CIK (same lookup as transform-sec)
  2. Fetches recent Form 4 filings from the submissions JSON
  3. Fetches and parses each Form 4 XML
  4. Emits: person entities, insider relationship claims, transaction claims,
     and insider "cluster buy" detection (3+ insiders buying within 30 days)

Evidence: Form 4 XML IS the primary source → "direct", 0.95.

SEC requirements:
  - User-Agent header with contact info (update UA constant below).
  - Rate limit: 10 requests/second.

Usage:
  gi run transform-sec-form4 --entity "Marqeta"
  gi run transform-sec-form4 --entity "AAPL"
"""
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

# ── Configuration ───────────────────────────────────────────────────────────

UA = "gi-transform-sec-form4/0.1 (+research; sample@example.com)"

TICKER_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
FILING_BASE = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{accn}/{doc}"

MAX_FORM4 = 50  # cap filings per run

# Transaction codes (SEC Form 4 taxonomy)
TXN_CODES = {
    "P": "open_market_purchase",
    "S": "open_market_sale",
    "A": "award",
    "M": "option_exercise",
    "C": "conversion",
    "F": "tax_withholding",
    "G": "gift",
    "X": "option_exercise",
    "D": "disposition_to_issuer",
}

# Transactions that represent genuine open-market conviction
# (as opposed to awards, option exercises, tax withholding, etc.)
OPEN_MARKET_BUY = {"P"}
OPEN_MARKET_SELL = {"S"}


# ── Utilities (must match gi.py's contract) ─────────────────────────────────

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
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


# ── CIK lookup (same as transform-sec) ──────────────────────────────────────

def lookup_cik(name_or_ticker: str) -> int | None:
    try:
        ticker_map = json.loads(http_get_raw(TICKER_URL))
    except Exception as e:
        print(f"WARNING: failed to fetch ticker map: {e}", file=sys.stderr)
        return None

    query = name_or_ticker.strip()
    q_upper = query.upper()
    q_lower = query.lower()

    for _, info in ticker_map.items():
        if info["ticker"].upper() == q_upper:
            return info["cik_str"]
    for _, info in ticker_map.items():
        if info["title"].lower() == q_lower:
            return info["cik_str"]
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


# ── Citation builder ────────────────────────────────────────────────────────

def make_cite(digest: str, norm_text: str, needle: str,
              context: int = 100) -> dict | None:
    pos = norm_text.find(needle)
    if pos == -1:
        return None
    start = max(0, pos - context)
    end = min(len(norm_text), pos + len(needle) + context)
    return {"artifact": digest, "span": [start, end],
            "quote": norm_text[start:end]}


def emit_claim(subj: str, pred: str, obj: str, cite: dict | None,
               basis: str = "") -> None:
    claim: dict = {"type": "claim", "subj": subj, "pred": pred,
                   "obj": obj, "polarity": "supports"}
    if cite:
        claim["evidence"] = "direct"
        claim["confidence"] = 0.95
        claim["cites"] = [cite]
    else:
        claim["evidence"] = "hypothesis"
        claim["confidence"] = 0.8
        claim["basis"] = basis or f"Derived from SEC Form 4 data for {subj}"
    emit(claim)


# ── Form 4 XML parsing ──────────────────────────────────────────────────────

def text_or_none(elem: ET.Element | None) -> str | None:
    """Get stripped text from an XML element, or None if missing/empty."""
    if elem is None:
        return None
    val = elem.find("value")
    if val is not None and val.text:
        return val.text.strip()
    if elem.text:
        return elem.text.strip()
    return None


def parse_form4_xml(xml_bytes: bytes) -> dict | None:
    """Parse a Form 4 XML document into a structured dict."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        print(f"WARNING: XML parse error: {e}", file=sys.stderr)
        return None

    # Strip XML namespaces for clean path access
    for elem in root.iter():
        if "}" in elem.tag:
            elem.tag = elem.tag.split("}", 1)[1]

    doc_type = text_or_none(root.find("documentType"))
    if doc_type not in ("3", "4", "5"):
        return None

    # Issuer
    issuer = root.find("issuer")
    issuer_name = text_or_none(issuer.find("issuerName")) if issuer is not None else None
    issuer_cik = text_or_none(issuer.find("issuerCik")) if issuer is not None else None
    ticker = text_or_none(issuer.find("issuerTradingSymbol")) if issuer is not None else None

    # Reporting owner
    owner = root.find("reportingOwner")
    owner_id = owner.find("reportingOwnerId") if owner is not None else None
    owner_name = text_or_none(owner_id.find("rptOwnerName")) if owner_id is not None else None
    owner_cik = text_or_none(owner_id.find("rptOwnerCik")) if owner_id is not None else None

    rel = owner.find("reportingOwnerRelationship") if owner is not None else None
    is_director = False
    is_officer = False
    is_ten_pct = False
    officer_title = ""
    if rel is not None:
        is_director = text_or_none(rel.find("isDirector")) == "1"
        is_officer = text_or_none(rel.find("isOfficer")) == "1"
        is_ten_pct = text_or_none(rel.find("isTenPercentOwner")) == "1"
        title_elem = rel.find("officerTitle")
        if title_elem is not None:
            officer_title = text_or_none(title_elem) or ""

    period = text_or_none(root.find("periodOfReport"))

    # Non-derivative transactions (the actual stock buys/sells)
    transactions = []
    nd_table = root.find("nonDerivativeTable")
    if nd_table is not None:
        for txn in nd_table.findall("nonDerivativeTransaction"):
            security = text_or_none(txn.find("securityTitle"))
            txn_date = text_or_none(txn.find("transactionDate"))
            coding = txn.find("transactionCoding")
            txn_code = text_or_none(coding.find("transactionCode")) if coding is not None else ""

            amounts = txn.find("transactionAmounts")
            shares = None
            price = None
            acq_disp = None
            if amounts is not None:
                shares = text_or_none(amounts.find("transactionShares"))
                price = text_or_none(amounts.find("transactionPricePerShare"))
                acd = amounts.find("transactionAcquiredDisposedCode")
                acq_disp = text_or_none(acd) if acd is not None else None

            post = txn.find("postTransactionAmounts")
            shares_owned = None
            if post is not None:
                sof = post.find("sharesOwnedFollowingTransaction")
                shares_owned = text_or_none(sof) if sof is not None else None

            transactions.append({
                "security": security or "",
                "date": txn_date or "",
                "code": txn_code or "",
                "shares": int(float(shares)) if shares else 0,
                "price": float(price) if price else 0.0,
                "acquired": acq_disp == "A",
                "disposed": acq_disp == "D",
                "shares_owned_after": int(float(shares_owned)) if shares_owned else 0,
                "derivative": False,
            })

    # Derivative transactions (options, RSUs — exercised or disposed)
    d_table = root.find("derivativeTable")
    if d_table is not None:
        for txn in d_table.findall("derivativeTransaction"):
            security = text_or_none(txn.find("securityTitle"))
            txn_date = text_or_none(txn.find("transactionDate"))
            coding = txn.find("transactionCoding")
            txn_code = text_or_none(coding.find("transactionCode")) if coding is not None else ""

            amounts = txn.find("transactionAmounts")
            shares = None
            price = None
            acq_disp = None
            if amounts is not None:
                shares = text_or_none(amounts.find("transactionShares"))
                price = text_or_none(amounts.find("transactionPricePerShare"))
                acd = amounts.find("transactionAcquiredDisposedCode")
                acq_disp = text_or_none(acd) if acd is not None else None

            transactions.append({
                "security": security or "",
                "date": txn_date or "",
                "code": txn_code or "",
                "shares": int(float(shares)) if shares else 0,
                "price": float(price) if price else 0.0,
                "acquired": acq_disp == "A",
                "disposed": acq_disp == "D",
                "derivative": True,
            })

    return {
        "form_type": doc_type,
        "period": period,
        "issuer_name": issuer_name,
        "issuer_cik": issuer_cik,
        "ticker": ticker,
        "owner_name": owner_name,
        "owner_cik": owner_cik,
        "is_director": is_director,
        "is_officer": is_officer,
        "is_ten_pct": is_ten_pct,
        "officer_title": officer_title,
        "transactions": transactions,
    }


# ── Cluster detection ───────────────────────────────────────────────────────

def detect_clusters(buy_events: list[dict], window_days: int = 30,
                    min_insiders: int = 3) -> list[dict]:
    """Detect insider cluster buys: 3+ different insiders buying within
    a rolling window.  This is the Lakonishok & Lee alpha signal.

    Returns a list of cluster objects: {start_date, end_date, insiders: [...]}.
    """
    if len(buy_events) < min_insiders:
        return []

    # Sort by date
    sorted_buys = sorted(buy_events, key=lambda e: e["date"])

    clusters = []
    window = timedelta(days=window_days)

    for i, anchor in enumerate(sorted_buys):
        anchor_date = datetime.fromisoformat(anchor["date"])
        window_end = anchor_date + window
        # Collect unique insiders who bought in [anchor_date, window_end]
        insiders_in_window: dict[str, dict] = {}
        for evt in sorted_buys[i:]:
            evt_date = datetime.fromisoformat(evt["date"])
            if evt_date > window_end:
                break
            if evt["insider_id"] not in insiders_in_window:
                insiders_in_window[evt["insider_id"]] = {
                    "name": evt["insider_name"],
                    "date": evt["date"],
                    "shares": evt["shares"],
                    "value": evt["value"],
                }

        if len(insiders_in_window) >= min_insiders:
            # Check this cluster isn't a subset of an already-found one
            insider_set = frozenset(insiders_in_window.keys())
            is_subset = False
            for c in clusters:
                if insider_set.issubset(c["insider_set"]):
                    is_subset = True
                    break
            if not is_subset:
                clusters.append({
                    "start_date": anchor["date"],
                    "end_date": max(e["date"] for e in sorted_buys[i:]
                                    if datetime.fromisoformat(e["date"]) <= window_end),
                    "insiders": list(insiders_in_window.values()),
                    "insider_set": insider_set,
                    "count": len(insiders_in_window),
                })

    return clusters


# ── Main pipeline ───────────────────────────────────────────────────────────

def main() -> int:
    payload = json.load(sys.stdin)
    ent = payload["entity"]
    evidence_dir = Path(payload["evidence_dir"])
    name = ent.get("name") or ent.get("id", "")

    # 1. CIK lookup
    print(f"Form4: looking up '{name}'...", file=sys.stderr)
    cik = lookup_cik(name)
    if not cik:
        print(f"WARNING: could not find '{name}' on SEC EDGAR", file=sys.stderr)
        return 0
    cik_padded = str(cik).zfill(10)
    print(f"Form4: CIK {cik_padded}", file=sys.stderr)

    # 2. Fetch submissions to find Form 4 filing URLs
    try:
        sub_url = SUBMISSIONS_URL.format(cik=cik_padded)
        raw = http_get_raw(sub_url)
    except Exception as e:
        print(f"WARNING: submissions fetch failed: {e}", file=sys.stderr)
        return 0

    sub_digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    sub_json = json.loads(raw)
    sub_norm = normalize_ws(raw.decode("utf-8"))
    emit({"type": "artifact", "uri": sub_url,
          "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
          "file": str(evidence_dir / f"tmp-form4-sub-{os.getpid()}-{int(time.time())}.json"),
          "hash": sub_digest})
    # Write the actual bytes
    (evidence_dir / f"tmp-form4-sub-{os.getpid()}-{int(time.time())}.json").write_bytes(raw)

    company_name = sub_json.get("name", name)
    company_slug = slugify(company_name)

    # 3. Find Form 4 filings
    recent = sub_json.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accns = recent.get("accessionNumber", [])
    docs = recent.get("primaryDocument", [])

    form4_indices = [i for i, f in enumerate(forms) if f in ("4", "4/A")]
    if not form4_indices:
        print(f"Form4: no Form 4 filings found in recent batch for {company_name}",
              file=sys.stderr)
        return 0

    print(f"Form4: found {len(form4_indices)} Form 4 filings "
          f"(processing up to {MAX_FORM4})", file=sys.stderr)

    # 4. Process each Form 4
    buy_events = []  # for cluster detection
    sell_events = []  # for cluster sell detection
    filings_processed = 0

    for idx in form4_indices[:MAX_FORM4]:
        accn = accns[idx]
        doc = docs[idx]
        date = dates[idx]
        accn_clean = accn.replace("-", "")

        # The primaryDocument for Form 4 often has an xsl prefix for rendering.
        # We need the raw XML — strip any xsl prefix.
        if "/" in doc:
            doc = doc.split("/")[-1]

        xml_url = FILING_BASE.format(cik_int=cik, accn=accn_clean, doc=doc)

        try:
            xml_raw = http_get_raw(xml_url)
        except Exception as e:
            print(f"  WARNING: fetch failed for {accn}: {e}", file=sys.stderr)
            continue

        parsed = parse_form4_xml(xml_raw)
        if not parsed:
            continue

        filings_processed += 1

        # Store the Form 4 XML as an artifact
        xml_digest = "sha256:" + hashlib.sha256(xml_raw).hexdigest()
        xml_norm = normalize_ws(xml_raw.decode("utf-8"))
        xml_tmp = evidence_dir / f"tmp-form4-{accn_clean}-{os.getpid()}-{int(time.time())}.xml"
        xml_tmp.write_bytes(xml_raw)
        emit({"type": "artifact", "uri": xml_url,
              "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "file": str(xml_tmp), "hash": xml_digest})

        # Emit the insider as a person entity
        owner_name = parsed["owner_name"] or "Unknown"
        owner_slug = slugify(owner_name)
        emit({"type": "entity", "id": owner_slug, "name": owner_name,
              "kind": "person",
              "attrs": {"cik": parsed["owner_cik"] or ""},
              "external_ids": {"cik": parsed["owner_cik"]} if parsed["owner_cik"] else {},
              "aliases": []})

        # Emit relationship claims (director/officer/10% holder)
        if parsed["is_director"]:
            cite = make_cite(xml_digest, xml_norm, owner_name)
            emit_claim(owner_slug, "serves_as_director_of", company_slug, cite,
                       f"Form 4 filed {date} lists {owner_name} as director of {company_name}")
        if parsed["is_officer"]:
            cite = make_cite(xml_digest, xml_norm, owner_name)
            emit_claim(owner_slug, "serves_as_officer_of", company_slug, cite,
                       f"Form 4 filed {date} lists {owner_name} as officer "
                       f"({parsed['officer_title']}) of {company_name}")
        if parsed["is_ten_pct"]:
            cite = make_cite(xml_digest, xml_norm, owner_name)
            emit_claim(owner_slug, "serves_as_ten_pct_holder_of", company_slug, cite,
                       f"Form 4 filed {date} lists {owner_name} as 10% holder of {company_name}")

        # Emit the Form 4 filing entity
        filing_id = f"{company_slug}-form4-{parsed['period']}-{owner_slug}"
        emit({"type": "entity", "id": filing_id,
              "name": f"Form 4 by {owner_name} ({parsed['period']})",
              "kind": "sec_filing",
              "attrs": {
                  "form": "Form 4",
                  "period": parsed["period"],
                  "filed_date": date,
                  "accession": accn,
                  "insider": owner_name,
                  "insider_role": ("director" if parsed["is_director"]
                                   else "officer" if parsed["is_officer"]
                                   else "10% holder" if parsed["is_ten_pct"]
                                   else "other"),
              },
              "external_ids": {"accession": accn},
              "aliases": [accn]})

        cite_filing = make_cite(xml_digest, xml_norm, parsed["period"] or date)
        emit_claim(owner_slug, "filed_insider_form", filing_id, cite_filing,
                   f"Form 4 filing by {owner_name} for {company_name}")

        # Process each transaction
        for txn in parsed["transactions"]:
            if txn["shares"] == 0:
                continue

            txn_value = txn["shares"] * txn["price"] if txn["price"] else 0
            txn_desc = TXN_CODES.get(txn["code"], txn["code"])

            # Determine the claim predicate
            pred = None
            if txn["code"] in OPEN_MARKET_BUY and txn["acquired"]:
                pred = "insider_bought"
            elif txn["code"] in OPEN_MARKET_SELL and txn["disposed"]:
                pred = "insider_sold"
            elif txn["code"] == "A":
                pred = "insider_awarded"
            elif txn["code"] == "M":
                pred = "insider_exercised_options"

            if pred is None:
                continue  # Skip non-meaningful transactions (gifts, tax, etc.)

            # Create a transaction entity
            txn_id = (f"{company_slug}-txn-{txn['date']}-{owner_slug}-"
                      f"{txn['code']}-{txn['shares']}")
            txn_entity = {
                "type": "entity", "id": txn_id,
                "name": (f"{owner_name} {txn_desc} "
                         f"{txn['shares']:,} shares ({txn['code']}) "
                         f"on {txn['date']}"),
                "kind": "insider_transaction",
                "attrs": {
                    "insider": owner_name,
                    "action": txn_desc,
                    "code": txn["code"],
                    "shares": txn["shares"],
                    "price": txn["price"],
                    "value": txn_value,
                    "date": txn["date"],
                    "security": txn["security"],
                    "acquired": txn["acquired"],
                    "derivative": txn["derivative"],
                    "shares_owned_after": txn.get("shares_owned_after", 0),
                },
                "external_ids": {}, "aliases": []}
            emit(txn_entity)

            # Cite the transaction shares in the XML
            cite = make_cite(xml_digest, xml_norm, str(txn["shares"]))
            if not cite:
                cite = make_cite(xml_digest, xml_norm, txn["date"])

            emit_claim(owner_slug, pred, company_slug, cite,
                       f"Form 4: {owner_name} {txn_desc} {txn['shares']} "
                       f"shares of {company_name} on {txn['date']}")

            # Track open-market buys AND sells for cluster detection
            if pred == "insider_bought":
                buy_events.append({
                    "date": txn["date"],
                    "insider_id": owner_slug,
                    "insider_name": owner_name,
                    "shares": txn["shares"],
                    "value": txn_value,
                })
            elif pred == "insider_sold":
                sell_events.append({
                    "date": txn["date"],
                    "insider_id": owner_slug,
                    "insider_name": owner_name,
                    "shares": txn["shares"],
                    "value": txn_value,
                })

    print(f"Form4: processed {filings_processed} filings, "
          f"{len(buy_events)} open-market buys, "
          f"{len(sell_events)} open-market sells detected", file=sys.stderr)

    # 5. Cluster detection — BUYS (bullish signal) and SELLS (red flag)
    if len(buy_events) >= 3:
        print("Form4: running cluster buy detection...", file=sys.stderr)
        clusters = detect_clusters(buy_events)
        if clusters:
            print(f"Form4: found {len(clusters)} cluster buy(s)!", file=sys.stderr)
            for cluster in clusters:
                cluster_id = f"{company_slug}-cluster-buy-{cluster['start_date']}"
                insiders_str = ", ".join(
                    f"{i['name']} ({i['shares']:,} sh)" for i in cluster["insiders"])
                emit({"type": "entity", "id": cluster_id,
                      "name": (f"Insider cluster buy: {cluster['count']} insiders "
                               f"bought {company_name} within 30 days "
                               f"({cluster['start_date']} to {cluster['end_date']})"),
                      "kind": "insider_cluster",
                      "attrs": {
                          "company": company_name,
                          "start_date": cluster["start_date"],
                          "end_date": cluster["end_date"],
                          "insider_count": cluster["count"],
                          "insiders": [i["name"] for i in cluster["insiders"]],
                          "total_shares": sum(i["shares"] for i in cluster["insiders"]),
                          "total_value": sum(i["value"] for i in cluster["insiders"]),
                          "direction": "buy",
                      },
                      "external_ids": {}, "aliases": []})

                # Cluster buys are BULLISH per Lakonishok & Lee (2001)
                emit({"type": "claim", "subj": company_slug,
                      "pred": "has_bullish_signal",
                      "obj": cluster_id,
                      "polarity": "supports",
                      "evidence": "direct",
                      "confidence": 0.9,
                      "cites": [],
                      "basis": (f"Cluster buy signal: {cluster['count']} unique insiders "
                                f"bought {company_name} shares on the open market "
                                f"within 30 days ({cluster['start_date']} to "
                                f"{cluster['end_date']}). Insiders: {insiders_str}. "
                                "Per Lakonishok & Lee (2001), insider cluster buys "
                                "predict positive excess returns.")})

    # Cluster SELL detection — bearish red flag
    if len(sell_events) >= 3:
        print("Form4: running cluster sell detection...", file=sys.stderr)
        sell_clusters = detect_clusters(sell_events)
        if sell_clusters:
            print(f"Form4: found {len(sell_clusters)} cluster sell(s)!",
                  file=sys.stderr)
            for cluster in sell_clusters:
                cluster_id = f"{company_slug}-cluster-sell-{cluster['start_date']}"
                insiders_str = ", ".join(
                    f"{i['name']} ({i['shares']:,} sh)" for i in cluster["insiders"])
                emit({"type": "entity", "id": cluster_id,
                      "name": (f"Insider cluster sell: {cluster['count']} insiders "
                               f"sold {company_name} within 30 days "
                               f"({cluster['start_date']} to {cluster['end_date']})"),
                      "kind": "insider_cluster",
                      "attrs": {
                          "company": company_name,
                          "start_date": cluster["start_date"],
                          "end_date": cluster["end_date"],
                          "insider_count": cluster["count"],
                          "insiders": [i["name"] for i in cluster["insiders"]],
                          "total_shares": sum(i["shares"] for i in cluster["insiders"]),
                          "total_value": sum(i["value"] for i in cluster["insiders"]),
                          "direction": "sell",
                      },
                      "external_ids": {}, "aliases": []})

                # Cluster sells are a bearish red flag (weaker signal than buys)
                emit({"type": "claim", "subj": company_slug,
                      "pred": "has_red_flag",
                      "obj": cluster_id,
                      "polarity": "supports",
                      "evidence": "direct",
                      "confidence": 0.6,
                      "cites": [],
                      "basis": (f"Cluster sell signal: {cluster['count']} unique insiders "
                                f"sold {company_name} shares on the open market "
                                f"within 30 days ({cluster['start_date']} to "
                                f"{cluster['end_date']}). Insiders: {insiders_str}. "
                                "Note: insider sells are a weaker signal than buys "
                                "(diversification, tax, 10b5-1 plans).")})

    print("Form4: done.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
