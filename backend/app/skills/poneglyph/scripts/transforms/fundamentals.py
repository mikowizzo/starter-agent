#!/usr/bin/env python3
"""
fundamentals.py — GI v2 transform: fundamentals snapshot JSON → direct claims.

Reads a market_fundamentals.py snapshot (GI v1 fetcher output) from the
host-fetched artifact and emits NDJSON: one company entity, one literal
entity + claim per non-null metric. Every claim quotes the verbatim
`"key": value` pair from the artifact (span runs from the key's opening
quote through the value's last character), so the gate verifies both the
NUMBER and its BINDING to the metric name.

Epistemics: a deterministic numeric fetcher is an observer, not an
inferencer — claims are evidence=direct, confidence=0.99. (Contrast the
llm reader: inferred, capped at 0.80.) Interpretation of the numbers
stays the analyst's job; this records what was REPORTED, when.

No yfinance import: the snapshot was captured before the artifact entered
the CAS. This transform is stdlib-only and fully deterministic — replay
never re-fetches, so a run can never silently change its mind.

Span trivia: offsets are Python str (character) indices into
artifact_text; the host gate slices the same decoded str, so they always
agree regardless of non-ASCII content.

Usage (host side):
    python3 market_fundamentals.py MSFT > msft.json        # capture snapshot
    GI2_ALLOW_FILE_URI=1 gi2.py run fundamentals \\
        --uri file:///abs/msft.json [--arg subject=company:msft]
"""

from __future__ import annotations

import hashlib
import json
import re
import sys

# (json key, pred) — curated; notes and scaffolding excluded.
TOP_KEYS = [
    ("company_name", "attr:company_name"),
    ("sector", "attr:sector"),
    ("industry", "attr:industry"),
    ("market_cap", "fund:market_cap"),
    ("shares_outstanding", "fund:shares_outstanding"),
    ("as_of_date", "fund:as_of"),
]

SECTIONS: dict[str, list[tuple[str, str]]] = {
    "valuation": [
        ("pe_trailing", "fund:pe_trailing"),
        ("pe_forward", "fund:pe_forward"),
        ("peg_ratio", "fund:peg_ratio"),
        ("price_to_book", "fund:price_to_book"),
        ("enterprise_to_ebitda", "fund:enterprise_to_ebitda"),
        ("price_to_sales", "fund:price_to_sales"),
        ("dividend_yield", "fund:dividend_yield"),
        ("enterprise_value", "fund:enterprise_value"),
        # valuation.market_cap duplicates the top-level key; skip it
    ],
    "growth": [
        ("revenue_yoy_pct", "fund:revenue_yoy_pct"),
        ("earnings_yoy_pct", "fund:earnings_yoy_pct"),
        ("revenue_qoq_pct", "fund:revenue_qoq_pct"),
        ("earnings_qoq_pct", "fund:earnings_qoq_pct"),
        ("revenue_3yr_cagr_pct", "fund:revenue_3yr_cagr_pct"),
    ],
    "profitability": [
        ("gross_margin_pct", "fund:gross_margin_pct"),
        ("operating_margin_pct", "fund:operating_margin_pct"),
        ("net_margin_pct", "fund:net_margin_pct"),
        ("return_on_equity_pct", "fund:return_on_equity_pct"),
        ("return_on_assets_pct", "fund:return_on_assets_pct"),
        ("fcf_margin_pct", "fund:fcf_margin_pct"),
    ],
    "balance_sheet": [
        ("debt_to_equity", "fund:debt_to_equity"),
        ("current_ratio", "fund:current_ratio"),
        ("quick_ratio", "fund:quick_ratio"),
        ("cash_to_debt", "fund:cash_to_debt"),
        ("net_debt", "fund:net_debt"),
        ("working_capital", "fund:working_capital"),
    ],
    "cash_flow": [
        ("operating_cf_ttm", "fund:operating_cf_ttm"),
        ("free_cf_ttm", "fund:free_cf_ttm"),
        ("capex_ttm", "fund:capex_ttm"),
        ("fcf_yield_pct", "fund:fcf_yield_pct"),
    ],
    "estimates": [
        # analyst-consensus aggregates, as REPORTED by the snapshot
        ("eps_forward_estimate", "fund:eps_forward_estimate"),
        ("revenue_forward_estimate", "fund:revenue_forward_estimate"),
        ("target_mean_price", "fund:target_mean_price"),
        ("target_median_price", "fund:target_median_price"),
        ("analyst_count", "fund:analyst_count"),
        ("recommendation_mean", "fund:recommendation_mean"),
        ("recommendation_key", "fund:recommendation_key"),
    ],
}

SURPRISE_KEYS = [
    ("eps_estimate", "eps_estimate"),
    ("eps_actual", "eps_actual"),
    ("surprise_pct", "surprise_pct"),
]

CONFIDENCE = 0.99

_DEC = json.JSONDecoder()  # raw_decode: exact value spans, stdlib-guaranteed


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s or "unnamed"


# ---------------------------------------------------------------------------
# Depth-1 member scanning via stdlib raw_decode (crew-reviewed rewrite).
# The whole artifact already passed json.loads, so a JSON value ALWAYS
# follows each key — raw_decode gives its exact end offset, no escape
# walking, no lookback, no silent drops.
# ---------------------------------------------------------------------------

def _skip_ws(text: str, i: int) -> int:
    while i < len(text) and text[i] in " \t\r\n":
        i += 1
    return i


def scan_members(text: str, obj_start: int) -> dict[str, tuple[int, int, int]]:
    """Depth-1 members of the object whose '{' is at obj_start.
    Returns {key: (pair_start, val_start, val_end)} — pair_start is the
    key's opening quote, so text[pair_start:val_end] is the verbatim
    `"key": value` pair. Later duplicate keys win (json.loads semantics)."""
    members: dict[str, tuple[int, int, int]] = {}
    i = _skip_ws(text, obj_start + 1)
    while i < len(text) and text[i] != "}":
        if text[i] == ",":
            i = _skip_ws(text, i + 1)
            continue
        key, kend = _DEC.raw_decode(text, i)   # the key string just opened
        j = _skip_ws(text, kend)
        j = _skip_ws(text, j + 1)              # past the ':'
        _, vend = _DEC.raw_decode(text, j)     # the value, consumed whole
        members[key] = (i, j, vend)
        i = _skip_ws(text, vend)
    return members


def iter_array_objects(text: str, arr_start: int) -> list[tuple[int, int]]:
    """(start, end) of each top-level object in the array whose '[' is at
    arr_start. Non-object elements and nested arrays are skipped whole."""
    out: list[tuple[int, int]] = []
    i = _skip_ws(text, arr_start + 1)
    while i < len(text) and text[i] != "]":
        if text[i] == ",":
            i = _skip_ws(text, i + 1)
            continue
        obj, vend = _DEC.raw_decode(text, i)
        if isinstance(obj, dict):
            out.append((i, vend))
        i = _skip_ws(text, vend)
    return out


def _display_name(val_text: str) -> str:
    """Human name for a literal: decoded if the value is a JSON string."""
    if val_text.startswith('"'):
        try:
            return str(json.loads(val_text)) or val_text
        except ValueError:
            return val_text
    return val_text


# ---------------------------------------------------------------------------
# Claim emission
# ---------------------------------------------------------------------------

def main() -> None:
    try:
        payload = json.loads(sys.stdin.read())
    except Exception as e:
        print(f"error parsing stdin: {e}", file=sys.stderr)
        sys.exit(1)

    artifact_text = payload.get("artifact_text", "")
    args = payload.get("args") or {}
    logs: list[tuple[str, str]] = []

    try:
        data = json.loads(artifact_text)
    except Exception as e:
        print(json.dumps({"op": "log", "level": "error",
                          "message": f"artifact is not valid JSON: {e}"}))
        sys.exit(1)
    if not isinstance(data, dict):
        print(json.dumps({"op": "log", "level": "error",
                          "message": f"artifact root is {type(data).__name__}, "
                                     f"expected a JSON object"}))
        sys.exit(1)
    if data.get("error"):
        print(json.dumps({"op": "log", "level": "error",
                          "message": f"snapshot fetch had failed: {data['error']}"}))
        sys.exit(1)

    ticker = str(data.get("ticker") or args.get("ticker") or "unknown")
    company_name = data.get("company_name") or ticker.upper()
    as_of = str(data.get("as_of_date") or "unknown")
    subj = args.get("subject") or f"company:{slugify(ticker)}"

    print(json.dumps({
        "op": "entity", "id": subj, "name": company_name,
        "kind": "company",
        "attrs": {"ticker": ticker, "as_of": as_of,
                  "source": "yfinance", "uri": payload.get("uri")},
    }))

    dq = data.get("data_quality")
    if not isinstance(dq, dict):
        dq = {}
    for w in dq.get("warnings") or []:
        if w and w != "none":
            logs.append(("warning", f"data_quality: {w}"))
    for k in ("info_available", "quarterly_financials_available",
              "balance_sheet_available", "cashflow_available"):
        if k in dq and not dq[k]:
            logs.append(("warning", f"data_quality: {k}=false"))

    top_members = scan_members(artifact_text, artifact_text.index("{"))

    claims = 0

    def emit(pred: str, pair: tuple[int, int, int], val_text: str) -> None:
        nonlocal claims
        pair_start, _, vend = pair
        quote = artifact_text[pair_start:vend]  # verbatim `"key": value`
        lit_name = _display_name(val_text)
        lit_id = "literal:" + hashlib.sha1(
            f"{pred}|{val_text}|{as_of}".encode()).hexdigest()[:10]
        print(json.dumps({
            "op": "entity", "id": lit_id, "name": lit_name,
            "kind": "literal", "attrs": {"pred": pred, "as_of": as_of},
        }))
        print(json.dumps({
            "op": "claim", "subj": subj, "pred": pred, "obj": lit_id,
            "polarity": "supports", "evidence": "direct",
            "confidence": CONFIDENCE, "quote": quote,
            "span_start": pair_start, "span_end": vend,
        }))
        claims += 1

    def _skip_null(pair: tuple[int, int, int]) -> bool:
        val = artifact_text[pair[1]:pair[2]].strip()
        return val in ("null", '""')

    # Top-level scalars (depth 1, so valuation.market_cap can't shadow)
    for key, pred in TOP_KEYS:
        m = top_members.get(key)
        if m and not _skip_null(m):
            emit(pred, m, artifact_text[m[1]:m[2]])

    # Sections
    for sec, keys in SECTIONS.items():
        tm = top_members.get(sec)
        if not tm or artifact_text[tm[1]] != "{":
            logs.append(("warning", f"section {sec} missing or not an object"))
            members: dict[str, tuple[int, int, int]] = {}
        else:
            members = scan_members(artifact_text, tm[1])
        for key, pred in keys:
            m = members.get(key)
            if m and not _skip_null(m):
                emit(pred, m, artifact_text[m[1]:m[2]])

    # Earnings surprises: per-quarter triples
    tm = top_members.get("earnings_surprises")
    if tm and artifact_text[tm[1]] == "[":
        for obj_start, obj_end in iter_array_objects(artifact_text, tm[1]):
            fields = scan_members(artifact_text, obj_start)
            qm = fields.get("quarter")
            quarter = _display_name(artifact_text[qm[1]:qm[2]]) if qm else "unknown"
            qslug = slugify(quarter)
            for key, suffix in SURPRISE_KEYS:
                m = fields.get(key)
                if m and not _skip_null(m):
                    emit(f"fund:{suffix}_{qslug}", m, artifact_text[m[1]:m[2]])

    for level, msg in logs:
        print(json.dumps({"op": "log", "level": level, "message": msg}))
    print(json.dumps({"op": "log", "level": "info",
                      "message": f"fundamentals snapshot {ticker} as_of {as_of}: "
                                 f"{claims} direct claim(s) from "
                                 f"{payload.get('uri')}"}))


if __name__ == "__main__":
    main()
