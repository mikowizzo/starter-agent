#!/usr/bin/env python3
"""transform-risk-diff — GI transform: 10-K/10-Q risk factor diffs → NDJSON.

Pulls the two most recent 10-K filings, extracts the risk factor sections,
diffs them, and emits claims for new and removed risk language.

This is the "What Changed" newsletter feature — the most underexploited
alpha source in SEC filings.

Contract: standard GI transform (stdin JSON → stdout NDJSON).
Evidence: "direct" for extracted quotes from filing text.

Usage:
  gi run transform-risk-diff --entity "Marqeta"
"""
import hashlib
import html as html_lib
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

UA = "gi-transform-risk-diff/0.1 (+research; sample@example.com)"
TICKER_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"



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


def make_cite(digest: str, norm_text: str, needle: str,
              context: int = 200) -> dict | None:
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
        claim["confidence"] = 0.9
        claim["cites"] = [cite]
    else:
        claim["evidence"] = "hypothesis"
        claim["confidence"] = 0.7
        claim["basis"] = basis
    emit(claim)


# ── Risk factor extraction ─────────────────────────────────────────────────

# Patterns for the risk factors section heading (em-dashes, colons supported)
RISK_SECTION_PATTERNS = [
    r"Item\s+1A[\.\s\-—:]+.*?Risk\s+Factors",
    r"RISK\s+FACTORS",
]

# Where the risk section typically ends
END_PATTERNS = [
    r"Item\s+1B[\.\s]",
    r"Item\s+2[\.\s]",
    r"UNRESOLVED\s+STAFF\s+COMMENTS",
]

# A real Item 1A body is never shorter than this; TOC lines are ~1 line long.
MIN_RISK_SECTION_CHARS = 2_000

# HTML blocks whose inner text is NOT document content
_BLOCK_CONTENT_RE = re.compile(
    r'<(script|style|noscript|template|svg)\b[^>]*>.*?</\1>',
    re.IGNORECASE | re.DOTALL,
)
_COMMENT_RE = re.compile(r'<!--.*?-->', re.DOTALL)
_TAG_RE = re.compile(r'<[^>]+>')
_BLOCK_TAG_RE = re.compile(
    r'</?(?:p|div|br|hr|tr|table|li|h[1-6]|section|header|footer)\b[^>]*>',
    re.IGNORECASE,
)


def _strip_html(html: str) -> str:
    """Strip HTML to plain text, removing script/style bodies entirely.

    Order matters: remove comments and non-content blocks FIRST (their
    contents contain '<' and '>' that would confuse a naive tag regex,
    and CSS 'body { ... }' text would otherwise leak into output).
    """
    text = html
    text = _COMMENT_RE.sub(' ', text)
    # Loop: EDGAR filings occasionally nest/misorder these blocks.
    while True:
        new = _BLOCK_CONTENT_RE.sub(' ', text)
        if new == text:
            break
        text = new
    # Convert block-level tags to newlines so paragraph splitting works.
    text = _BLOCK_TAG_RE.sub('\n', text)
    text = _TAG_RE.sub(' ', text)
    text = html_lib.unescape(text)          # &amp; &nbsp; &#160; etc.
    text = text.replace('\xa0', ' ')
    # Collapse spaces but preserve paragraph breaks.
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n\s*\n+', '\n\n', text)
    return text.strip()


def _candidate_body(text: str, start_match: re.Match) -> str:
    """Extract body between a start match and the nearest end marker."""
    end = len(text)
    for ep in END_PATTERNS:
        em = re.search(ep, text[start_match.end():], re.IGNORECASE)
        if em:
            end = min(end, start_match.end() + em.start())
    return text[start_match.end():end].strip()


def extract_risk_factors(html_text: str) -> str:
    """Extract the Item 1A Risk Factors body from a 10-K, skipping TOC.

    10-K filings list 'Item 1A. Risk Factors' in the table of contents
    near the top. We collect ALL start-pattern matches and return the
    first whose body meets the minimum length requirement; short bodies
    are TOC entries or cross-references.
    """
    # Strip HTML first, preserving paragraph breaks
    text = _strip_html(html_text)
    # Don't normalize_ws here — we need paragraph breaks (\n\n) for
    # extract_risk_paragraphs to work. Normalize only for the candidate
    # matching (which uses its own normalize inside _candidate_body).

    candidates: list[tuple[int, str]] = []
    for pat in RISK_SECTION_PATTERNS:
        for m in re.finditer(pat, text, re.IGNORECASE):
            body = _candidate_body(text, m)
            candidates.append((len(body), body))

    # Longest body wins — the actual section, never the TOC entry.
    candidates.sort(key=lambda c: c[0], reverse=True)
    if candidates and candidates[0][0] >= MIN_RISK_SECTION_CHARS:
        return candidates[0][1]
    return ""


def extract_risk_paragraphs(risk_text: str) -> list[str]:
    """Split risk factor text into paragraphs for comparison.

    Paragraph granularity is the right unit for diffing: a risk factor is
    typically one or more paragraphs, and sentence-level splitting is
    fragile against abbreviations (U.S., Inc., Dr.) and decimals found
    throughout SEC filings.
    """
    paragraphs = re.split(r'\n\s*\n', risk_text)
    # Keep paragraphs with meaningful risk language
    risk_keywords = re.compile(
        r"\b(risk|could|may|might|materially|adversely|uncertain|"
        r"competition|regulatory|litigation|cyber|supply.chain|"
        r"inflation|geopolitical|sanction|tariff|climate|AI|"
        r"artificial.intelligence|customer.concentration|"
        r"key.person|intellectual.property|data.breach|"
        r"recession|downturn|volatil|liquidity|debt|"
        r"covenant|going.concern|restatement|weakness)\b",
        re.IGNORECASE)
    return [re.sub(r"\s+", " ", p).strip() for p in paragraphs
            if len(p.strip()) > 30 and len(p.strip()) < 1000
            and risk_keywords.search(p)]


def diff_risk_factors(prev_paragraphs: list[str],
                      curr_paragraphs: list[str]) -> dict:
    """Compare two sets of risk paragraphs. Returns added, removed, shared."""
    # Hash the FULL normalized text, not a prefix, to avoid collisions
    # on boilerplate openings shared by many risk factors.
    def norm_key(s: str) -> str:
        return hashlib.sha256(
            re.sub(r"\s+", " ", s.lower().strip()).encode("utf-8")
        ).hexdigest()

    prev_keys = {norm_key(s): s for s in prev_paragraphs}
    curr_keys = {norm_key(s): s for s in curr_paragraphs}

    added = [curr_keys[k] for k in curr_keys if k not in prev_keys]
    removed = [prev_keys[k] for k in prev_keys if k not in curr_keys]
    shared = list(set(prev_keys.keys()) & set(curr_keys.keys()))

    return {"added": added, "removed": removed, "shared_count": len(shared)}


# ── Main ────────────────────────────────────────────────────────────────────

def main() -> int:
    payload = json.load(sys.stdin)
    ent = payload["entity"]
    evidence_dir = Path(payload["evidence_dir"])
    name = ent.get("name") or ent.get("id", "")

    print(f"RiskDiff: looking up '{name}'...", file=sys.stderr)
    cik = lookup_cik(name)
    if not cik:
        print(f"WARNING: could not find '{name}' on SEC EDGAR", file=sys.stderr)
        return 0
    cik_padded = str(cik).zfill(10)
    print(f"RiskDiff: CIK {cik_padded}", file=sys.stderr)

    # Fetch submissions
    try:
        sub_url = SUBMISSIONS_URL.format(cik=cik_padded)
        sub_raw = http_get_raw(sub_url)
        sub_json = json.loads(sub_raw)
    except Exception as e:
        print(f"WARNING: submissions fetch failed: {e}", file=sys.stderr)
        return 0

    company_name = sub_json.get("name", name)
    company_slug = slugify(company_name)

    # Find the two most recent 10-K filings
    recent = sub_json.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accns = recent.get("accessionNumber", [])
    docs = recent.get("primaryDocument", [])

    tenk_indices = [i for i, f in enumerate(forms) if f in ("10-K", "10-K/A")]
    if len(tenk_indices) < 2:
        print(f"RiskDiff: need at least 2 10-K filings, found {len(tenk_indices)}",
              file=sys.stderr)
        return 0

    # Take the two most recent
    current_idx = tenk_indices[0]
    previous_idx = tenk_indices[1]

    filings_to_fetch = [
        ("current", current_idx, dates[current_idx], accns[current_idx], docs[current_idx]),
        ("previous", previous_idx, dates[previous_idx], accns[previous_idx], docs[previous_idx]),
    ]

    risk_texts = {}
    for label, idx, date, accn, doc in filings_to_fetch:
        accn_clean = accn.replace("-", "")
        doc_url = (f"https://www.sec.gov/Archives/edgar/data/"
                   f"{cik}/{accn_clean}/{doc}")

        print(f"RiskDiff: fetching {label} 10-K ({date})...", file=sys.stderr)
        try:
            filing_raw = http_get_raw(doc_url)
        except Exception as e:
            print(f"  WARNING: fetch failed: {e}", file=sys.stderr)
            continue

        # Strip HTML using the proper parser (removes script/style bodies)
        html_text = filing_raw.decode("utf-8", errors="replace")

        risk_text = extract_risk_factors(html_text)
        if not risk_text:
            print(f"  WARNING: could not extract risk factors from {label} 10-K",
                  file=sys.stderr)
            continue

        risk_texts[label] = {
            "text": risk_text,
            "date": date,
            "accn": accn,
            "raw_size": len(filing_raw),
        }

        # Store as artifact — use synthetic URI since we hash the EXTRACTED
        # section, not the full filing. The source_url is kept as provenance
        # so a verifier knows where the bytes came from.
        risk_bytes = risk_text.encode("utf-8")
        risk_digest = "sha256:" + hashlib.sha256(risk_bytes).hexdigest()
        risk_tmp = (evidence_dir /
                    f"tmp-risk-{label}-{accn_clean}-{os.getpid()}-{int(time.time())}.txt")
        risk_tmp.write_bytes(risk_bytes)
        synthetic_uri = f"urn:gi:risk-section:{accn_clean}:{risk_digest[7:23]}"
        emit({"type": "artifact",
              "uri": synthetic_uri,
              "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "file": str(risk_tmp), "hash": risk_digest,
              "source_url": doc_url})

        risk_texts[label]["digest"] = risk_digest
        risk_texts[label]["norm"] = normalize_ws(risk_text)

    if "current" not in risk_texts or "previous" not in risk_texts:
        print("RiskDiff: could not fetch both 10-Ks for comparison", file=sys.stderr)
        return 0

    # Extract and diff risk paragraphs
    print("RiskDiff: extracting and diffing risk paragraphs...", file=sys.stderr)
    prev_paragraphs = extract_risk_paragraphs(risk_texts["previous"]["text"])
    curr_paragraphs = extract_risk_paragraphs(risk_texts["current"]["text"])
    diff = diff_risk_factors(prev_paragraphs, curr_paragraphs)

    print(f"RiskDiff: {len(diff['added'])} new risks, "
          f"{len(diff['removed'])} removed risks, "
          f"{diff['shared_count']} unchanged", file=sys.stderr)

    # Emit the diff entity
    curr_date = risk_texts["current"]["date"]
    prev_date = risk_texts["previous"]["date"]
    diff_id = f"{company_slug}-risk-diff-{curr_date}"

    emit({"type": "entity", "id": diff_id,
          "name": f"Risk Factor Diff: {company_name} ({prev_date} → {curr_date})",
          "kind": "risk_factor_diff",
          "attrs": {
              "company": company_name,
              "current_date": curr_date,
              "previous_date": prev_date,
              "added_count": len(diff["added"]),
              "removed_count": len(diff["removed"]),
              "unchanged_count": diff["shared_count"],
              "added_risks": diff["added"][:20],  # cap for graph size
              "removed_risks": diff["removed"][:10],
          },
          "external_ids": {}, "aliases": []})

    # Emit claim linking the company to the diff
    emit_claim(company_slug, "has_risk_factor", diff_id, None,
               f"Risk factor diff for {company_name} between "
               f"{prev_date} and {curr_date}")

    # Emit individual NEW risk factors as entities with claims
    # These are the alpha signals — new risks that didn't exist before
    for i, risk in enumerate(diff["added"][:15]):
        risk_id = f"{company_slug}-new-risk-{curr_date}-{i+1}"
        risk_snippet = risk[:200]

        emit({"type": "entity", "id": risk_id,
              "name": f"New risk factor ({risk_snippet[:80]}...)",
              "kind": "risk_factor",
              "attrs": {
                  "text": risk_snippet,
                  "detected_date": curr_date,
                  "type": "new",
              },
              "external_ids": {}, "aliases": []})

        # Cite the new risk in the current 10-K text
        cite = make_cite(risk_texts["current"]["digest"],
                         risk_texts["current"]["norm"],
                         risk[:80])
        emit_claim(company_slug, "added_risk_factor", risk_id, cite,
                   f"New risk factor detected in {company_name} 10-K "
                   f"filed {curr_date}")

    # Emit removed risks (less alpha but still interesting)
    for i, risk in enumerate(diff["removed"][:5]):
        risk_id = f"{company_slug}-removed-risk-{curr_date}-{i+1}"
        emit({"type": "entity", "id": risk_id,
              "name": f"Removed risk: {risk[:80]}...",
              "kind": "risk_factor",
              "attrs": {"text": risk[:200], "detected_date": curr_date,
                        "type": "removed"},
              "external_ids": {}, "aliases": []})
        cite = make_cite(risk_texts["previous"]["digest"],
                         risk_texts["previous"]["norm"],
                         risk[:80])
        emit_claim(company_slug, "removed_risk_factor", risk_id, cite,
                   f"Risk factor removed in {company_name} 10-K filed {curr_date}")

    print("RiskDiff: done.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
