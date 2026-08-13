#!/usr/bin/env python3
"""transform-8k-events — GI transform: 8-K material event extraction → NDJSON.

Fetches recent 8-K filings, classifies them by item code, and emits typed
material_event entities with claims.  This powers the "8-K Pulse" newsletter
feature — the daily/weekly digest of material events.

Contract: standard GI transform (stdin JSON → stdout NDJSON).

8-K Item Code Reference (material events that move stocks):
  1.01  Material definitive agreement (M&A, major contract)
  1.02  Termination of material agreement
  1.03  Bankruptcy or receivership
  1.04  Mine safety (skip)
  1.05  Cybersecurity incident (NEW — high signal)
  2.01  Completion of acquisition or disposition
  2.02  Results of operations (earnings release)
  2.03  Creation of direct financial obligation
  2.04  Acceleration of direct financial obligation (default trigger)
  2.05  Costs associated with exit/disposal (restructuring)
  3.01  Delisting notice
  3.02  Unregistered sales of equity
  3.03  Material modification to security holders
  4.01  Change in accountant (AUDITOR CHANGE — fraud signal)
  5.01  Change in control of company
  5.02  Departure/election of director or officer (CFO/CEO departure)
  5.03  Amendment to charter/bylaws
  5.05  Amendment to code of ethics
  5.07  Submission of matters to a vote (proxy/activist)
  7.01  Regulation FD disclosure
  8.01  Other events (catch-all — often material)
  9.01  Financial statements and exhibits

Usage:
  gi run transform-8k-events --entity "Marqeta"
"""
import hashlib
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

UA = "gi-transform-8k-events/0.1 (+research; sample@example.com)"
TICKER_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"

MAX_8K = 30  # cap per run

# ── 8-K Item Classification ─────────────────────────────────────────────────
# Maps item codes → (GI predicate, severity score, description)
ITEM_MAP = {
    "1.01": ("reported_material_agreement", 8, "Material definitive agreement entered"),
    "1.02": ("reported_material_agreement", 9, "Material agreement terminated"),
    "1.03": ("experienced_event", 10, "Bankruptcy or receivership filed"),
    "1.05": ("disclosed_cyber_incident", 9, "Cybersecurity incident disclosed"),
    "2.01": ("completed_acquisition", 8, "Completed acquisition or disposition"),
    "2.02": ("reported_earnings", 6, "Results of operations (earnings release)"),
    "2.03": ("experienced_event", 6, "Created direct financial obligation"),
    "2.04": ("experienced_event", 10, "Acceleration of financial obligation (DEFAULT)"),
    "2.05": ("experienced_event", 5, "Costs associated with exit/disposal"),
    "3.01": ("experienced_event", 10, "Delisting notice"),
    "3.02": ("experienced_event", 4, "Unregistered sales of equity"),
    "3.03": ("experienced_event", 6, "Material modification to securities"),
    "4.01": ("changed_auditor", 9, "Change in certifying accountant"),
    "5.01": ("experienced_event", 8, "Change in control of company"),
    "5.02": ("changed_executive", 8, "Director/officer departure or election"),
    "5.03": ("experienced_event", 4, "Amendment to charter or bylaws"),
    "5.07": ("experienced_event", 5, "Matters submitted to a vote"),
    "7.01": ("experienced_event", 3, "Regulation FD disclosure"),
    "8.01": ("experienced_event", 5, "Other material event"),
    "9.01": ("experienced_event", 2, "Financial statements and exhibits"),
}


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


def lookup_cik(name_or_ticker: str) -> int | None:
    try:
        ticker_map = json.loads(http_get_raw(TICKER_URL))
    except Exception as e:
        print(f"WARNING: failed to fetch ticker map: {e}", file=sys.stderr)
        return None
    query = name_or_ticker.strip()
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


def make_cite(digest: str, norm_text: str, needle: str,
              context: int = 150) -> dict | None:
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
        claim["basis"] = basis
    emit(claim)


def main() -> int:
    payload = json.load(sys.stdin)
    ent = payload["entity"]
    evidence_dir = Path(payload["evidence_dir"])
    name = ent.get("name") or ent.get("id", "")

    print(f"8K: looking up '{name}'...", file=sys.stderr)
    cik = lookup_cik(name)
    if not cik:
        print(f"WARNING: could not find '{name}' on SEC EDGAR", file=sys.stderr)
        return 0
    cik_padded = str(cik).zfill(10)
    print(f"8K: CIK {cik_padded}", file=sys.stderr)

    try:
        sub_url = SUBMISSIONS_URL.format(cik=cik_padded)
        sub_raw = http_get_raw(sub_url)
        sub_json = json.loads(sub_raw)
    except Exception as e:
        print(f"WARNING: submissions fetch failed: {e}", file=sys.stderr)
        return 0

    # Store submissions artifact
    sub_digest = "sha256:" + hashlib.sha256(sub_raw).hexdigest()
    sub_tmp = (evidence_dir /
               f"tmp-8k-sub-{os.getpid()}-{int(time.time())}.json")
    sub_tmp.write_bytes(sub_raw)
    emit({"type": "artifact", "uri": sub_url,
          "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
          "file": str(sub_tmp), "hash": sub_digest})
    sub_norm = normalize_ws(sub_raw.decode("utf-8"))

    company_name = sub_json.get("name", name)
    company_slug = slugify(company_name)

    # Find 8-K filings
    recent = sub_json.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accns = recent.get("accessionNumber", [])
    items_list = recent.get("items", [])

    k8_indices = [i for i, f in enumerate(forms) if f in ("8-K", "8-K/A")]
    if not k8_indices:
        print(f"8K: no 8-K filings found for {company_name}", file=sys.stderr)
        return 0

    print(f"8K: found {len(k8_indices)} 8-K filings (processing up to {MAX_8K})",
          file=sys.stderr)

    count = 0
    high_severity_events = []

    for idx in k8_indices[:MAX_8K]:
        date = dates[idx]
        accn = accns[idx]
        items_str = items_list[idx] if idx < len(items_list) else ""

        # Parse the comma-separated item codes
        items = [s.strip() for s in items_str.split(",") if s.strip()]

        for item in items:
            if item not in ITEM_MAP:
                continue

            pred, severity, desc = ITEM_MAP[item]

            event_id = f"{company_slug}-8k-{date}-item{item}"
            event_id = event_id.replace(".", "-")

            emit({"type": "entity", "id": event_id,
                  "name": f"8-K Item {item}: {desc} ({date})",
                  "kind": "material_event",
                  "attrs": {
                      "item": item,
                      "description": desc,
                      "date": date,
                      "severity": severity,
                      "accession": accn,
                      "company": company_name,
                  },
                  "external_ids": {"accession": accn},
                  "aliases": []})

            # Cite the accession number in submissions JSON
            cite = make_cite(sub_digest, sub_norm, accn)
            emit_claim(company_slug, pred, event_id, cite,
                       f"8-K filed {date}: {desc}")

            if severity >= 8:
                high_severity_events.append({
                    "item": item, "date": date, "desc": desc,
                    "event_id": event_id, "severity": severity,
                })

            count += 1

    print(f"8K: extracted {count} material events "
          f"({len(high_severity_events)} high-severity)", file=sys.stderr)

    # Emit a high-severity alert if there are multiple serious events
    if len(high_severity_events) >= 2:
        alert_id = f"{company_slug}-event-alert-{dates[k8_indices[0]]}"
        items_summary = "; ".join(
            f"Item {e['item']} ({e['date']})" for e in high_severity_events)
        emit({"type": "entity", "id": alert_id,
              "name": (f"High-severity event cluster: "
                       f"{len(high_severity_events)} material events "
                       f"for {company_name}"),
              "kind": "red_flag",
              "attrs": {
                  "company": company_name,
                  "event_count": len(high_severity_events),
                  "events": [{"item": e["item"], "date": e["date"],
                              "desc": e["desc"]}
                             for e in high_severity_events],
              },
              "external_ids": {}, "aliases": []})
        emit_claim(company_slug, "has_red_flag", alert_id, None,
                   f"{company_name} has {len(high_severity_events)} "
                   f"high-severity 8-K events: {items_summary}")

    print("8K: done.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
