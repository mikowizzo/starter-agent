#!/usr/bin/env python3
"""
market_fundamentals.py - GI transform script.

Fetches fundamental data for a stock ticker via yfinance (valuation ratios,
margins, returns, growth, balance-sheet ratios, cash-flow TTM aggregates,
analyst estimates, and earnings-surprise history) and emits a single JSON
document to stdout.

This is a SEPARATE transform from market_pricing.py because fundamentals have
a different refresh cadence (quarterly vs daily), different failure modes, and
different provenance. yfinance fundamentals are notoriously flaky: fields
silently return None, and ETFs/ADRs/commodity proxies often return empty
frames. Every section degrades gracefully to nulls plus a data_quality
warning; the script NEVER exits without emitting JSON.

Output conventions (matching market_pricing.py):
    - Fields suffixed `_pct` are percentages on a 0-100 scale.
      Margins and growth rates are converted from yfinance's 0-1 fractions.
    - Ratios (pe, price_to_book, current_ratio, ...) are plain numbers.
    - Money quantities (market_cap, net_debt, *_ttm) are in the reporting
      currency emitted by yfinance, unscaled.
    - Fields that could not be computed are null; check data_quality.warnings.

Usage:
    python market_fundamentals.py TICKER

Dependencies: yfinance, pandas, numpy (plus stdlib). Fully self-contained.
"""

import argparse
import json
import sys
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Small numeric helpers
# ---------------------------------------------------------------------------

def _r(x, digits=4):
    """Round a scalar, converting NaN/inf/numpy scalars to None."""
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(v):
        return None
    return round(v, digits)


def _pct(fraction, digits=1):
    """Convert a 0-1 fraction (yfinance convention) to a 0-100 percentage."""
    v = _r(fraction, digits + 4)
    if v is None:
        return None
    return round(v * 100.0, digits)


def _growth_pct(latest, prior):
    """Growth rate as a 0-100 percentage. Guards against missing values,
    zero, and negative bases (a growth rate off a negative earnings base is
    meaningless, so we return None rather than a sign-flipped artifact)."""
    latest = _r(latest)
    prior = _r(prior)
    if latest is None or prior is None or prior <= 0:
        return None
    return round((latest / prior - 1.0) * 100.0, 1)


def _ratio(num, den, digits=4):
    """Plain ratio with None/zero guards. Negative numerators are allowed."""
    num = _r(num)
    den = _r(den)
    if num is None or den is None or den == 0:
        return None
    return round(num / den, digits)


# ---------------------------------------------------------------------------
# yfinance frame access helpers
# ---------------------------------------------------------------------------

def _get_row(df, candidates):
    """Return values of the first matching row label, sorted most-recent-
    column first, as a list of floats (NaN dropped). None if no match."""
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return None
    index_lower = {str(i).strip().lower(): i for i in df.index}
    for name in candidates:
        key = name.strip().lower()
        if key in index_lower:
            row = df.loc[index_lower[key]]
            pairs = []
            for col, val in row.items():
                try:
                    ts = pd.Timestamp(col)
                except Exception:
                    continue
                v = _r(val)
                if v is not None:
                    pairs.append((ts, v))
            pairs.sort(key=lambda p: p[0], reverse=True)
            values = [v for _, v in pairs]
            return values if values else None
    return None


def _sum_first(values, n):
    """Sum the first n (most recent) values; None unless all n exist."""
    if not values or len(values) < n:
        return None
    return round(sum(values[:n]), 4)


def _fetch_frame(getter, warnings, label):
    """Call a yfinance frame property defensively; return the frame or None."""
    try:
        df = getter()
        if df is None or not isinstance(df, pd.DataFrame) or df.empty:
            warnings.append(f"{label}: empty or unavailable from yfinance")
            return None
        return df
    except Exception as e:
        warnings.append(f"{label}: fetch failed ({e})")
        return None


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------

def build_valuation(info):
    return {
        "pe_trailing": _r(info.get("trailingPE"), 2),
        "pe_forward": _r(info.get("forwardPE"), 2),
        "peg_ratio": _r(info.get("pegRatio"), 2),
        "price_to_book": _r(info.get("priceToBook"), 2),
        "enterprise_to_ebitda": _r(info.get("enterpriseToEbitda"), 2),
        "price_to_sales": _r(info.get("priceToSalesTrailing12Months"), 2),
        "dividend_yield": _pct(info.get("dividendYield"), 2),
        "market_cap": _r(info.get("marketCap"), 0),
        "enterprise_value": _r(info.get("enterpriseValue"), 0),
    }


def build_growth(info, quarterly_financials, annual_financials, warnings):
    revenue = _get_row(quarterly_financials, ["Total Revenue"])
    earnings = _get_row(quarterly_financials, [
        "Net Income",
        "Net Income Common Stockholders",
        "Net Income Applicable To Common Shares",
    ])

    revenue_qoq = _growth_pct(revenue[0], revenue[1]) \
        if revenue and len(revenue) >= 2 else None
    revenue_yoy = _growth_pct(revenue[0], revenue[4]) \
        if revenue and len(revenue) >= 5 else None
    earnings_qoq = _growth_pct(earnings[0], earnings[1]) \
        if earnings and len(earnings) >= 2 else None
    earnings_yoy = _growth_pct(earnings[0], earnings[4]) \
        if earnings and len(earnings) >= 5 else None

    notes = []
    if revenue_yoy is None:
        notes.append("revenue YoY needs 5 quarters of revenue history")
    if earnings_yoy is None:
        notes.append("earnings YoY needs 5 quarters of positive-base earnings")
    # Fall back to yfinance's precomputed quarterly yoy growth if our
    # own computation could not run (still guarded; these are fractions).
    if revenue_yoy is None and revenue_qoq is None:
        fb = _pct(info.get("revenueGrowth"))
        if fb is not None:
            revenue_yoy = fb
            notes.append("revenue_yoy_pct fell back to info.revenueGrowth")
    if earnings_yoy is None and earnings_qoq is None:
        fb = _pct(info.get("earningsGrowth"))
        if fb is not None:
            earnings_yoy = fb
            notes.append("earnings_yoy_pct fell back to info.earningsGrowth")

    # 3-year revenue CAGR from the ANNUAL income statement (needs two revenue
    # readings ~3 fiscal years apart, i.e. 4 annual columns).
    cagr = None
    annual_rev = _get_row(annual_financials, ["Total Revenue"])
    if annual_rev and len(annual_rev) >= 4 \
            and annual_rev[3] > 0 and annual_rev[0] > 0:
        cagr = round(((annual_rev[0] / annual_rev[3]) ** (1.0 / 3.0) - 1.0)
                     * 100.0, 1)
    else:
        notes.append("revenue_3yr_cagr_pct: insufficient annual history")

    return {
        "revenue_yoy_pct": revenue_yoy,
        "earnings_yoy_pct": earnings_yoy,
        "revenue_qoq_pct": revenue_qoq,
        "earnings_qoq_pct": earnings_qoq,
        "revenue_3yr_cagr_pct": cagr,
        "note": "; ".join(notes) if notes else
                "growth computed from quarterly_financials; "
                "negative earnings bases yield null",
    }


def build_profitability(info, quarterly_cashflow, revenue_ttm):
    ocf = _get_row(quarterly_cashflow, [
        "Operating Cash Flow", "Total Cash From Operating Activities"])
    capex = _get_row(quarterly_cashflow, [
        "Capital Expenditure", "Capital Expenditures"])
    fcf_row = _get_row(quarterly_cashflow, ["Free Cash Flow"])

    fcf_ttm = None
    if fcf_row and len(fcf_row) >= 4:
        fcf_ttm = _sum_first(fcf_row, 4)
    elif ocf and capex and len(ocf) >= 4 and len(capex) >= 4:
        # Capex is conventionally negative in yfinance cashflow frames.
        fcf_ttm = round(sum(o + c for o, c in zip(ocf[:4], capex[:4])), 4)

    fcf_margin = None
    if fcf_ttm is not None and revenue_ttm is not None and revenue_ttm > 0:
        fcf_margin = round(fcf_ttm / revenue_ttm * 100.0, 1)

    return {
        "gross_margin_pct": _pct(info.get("grossMargins")),
        "operating_margin_pct": _pct(info.get("operatingMargins")),
        "net_margin_pct": _pct(info.get("profitMargins")),
        "return_on_equity_pct": _pct(info.get("returnOnEquity")),
        "return_on_assets_pct": _pct(info.get("returnOnAssets")),
        "fcf_margin_pct": fcf_margin,
    }


def build_balance_sheet(info, balance_sheet):
    note_parts = []

    debt = _get_row(balance_sheet, ["Total Debt", "Total Liabilities Net Minority Interest"])
    equity = _get_row(balance_sheet, [
        "Stockholders Equity", "Common Stock Equity", "Total Stockholder Equity",
        "Total Equity Gross Minority Interest"])
    cur_assets = _get_row(balance_sheet, [
        "Current Assets", "Total Current Assets", "Total Assets"])
    cur_liabs = _get_row(balance_sheet, [
        "Current Liabilities", "Total Current Liabilities"])
    cash = _get_row(balance_sheet, [
        "Cash And Cash Equivalents",
        "Cash Cash Equivalents And Short Term Investments", "Cash"])
    inventory = _get_row(balance_sheet, ["Inventory", "Inventories"])

    total_debt = debt[0] if debt else _r(info.get("totalDebt"))
    total_equity = equity[0] if equity else None
    if debt is None and total_debt is not None:
        note_parts.append("total debt fell back to info.totalDebt")

    debt_to_equity = _ratio(total_debt, total_equity, 2) \
        if total_equity and total_equity != 0 else None
    if debt_to_equity is None and total_debt is not None:
        note_parts.append("debt_to_equity: equity missing or zero")

    current_ratio = _ratio(cur_assets[0] if cur_assets else None,
                           cur_liabs[0] if cur_liabs else None, 2)
    if current_ratio is None:
        current_ratio = _r(info.get("currentRatio"), 2)
        if current_ratio is not None:
            note_parts.append("current_ratio fell back to info.currentRatio")

    quick_ratio = None
    if cur_assets and cur_liabs:
        liquid = cur_assets[0] - (inventory[0] if inventory else 0.0)
        quick_ratio = _ratio(liquid, cur_liabs[0], 2)
    if quick_ratio is None:
        quick_ratio = _r(info.get("quickRatio"), 2)
        if quick_ratio is not None:
            note_parts.append("quick_ratio fell back to info.quickRatio")

    cash_latest = cash[0] if cash else _r(info.get("totalCash"))
    cash_to_debt = _ratio(cash_latest, total_debt, 2)
    net_debt = round(total_debt - cash_latest, 4) \
        if total_debt is not None and cash_latest is not None else None
    working_capital = round(cur_assets[0] - cur_liabs[0], 4) \
        if cur_assets and cur_liabs else None

    return {
        "debt_to_equity": debt_to_equity,
        "current_ratio": current_ratio,
        "quick_ratio": quick_ratio,
        "cash_to_debt": cash_to_debt,
        "net_debt": net_debt,
        "working_capital": working_capital,
        "note": "; ".join(note_parts) if note_parts else
                "ratios computed from latest quarterly balance sheet",
    }


def build_cash_flow(quarterly_cashflow, revenue_ttm, market_cap):
    note_parts = []

    ocf = _get_row(quarterly_cashflow, [
        "Operating Cash Flow", "Total Cash From Operating Activities"])
    capex = _get_row(quarterly_cashflow, [
        "Capital Expenditure", "Capital Expenditures"])
    fcf_row = _get_row(quarterly_cashflow, ["Free Cash Flow"])

    ocf_ttm = _sum_first(ocf, 4)
    capex_ttm = _sum_first(capex, 4)
    if ocf_ttm is None:
        note_parts.append("operating_cf_ttm needs 4 quarters of OCF")
    if capex_ttm is None:
        note_parts.append("capex_ttm needs 4 quarters of capex "
                          "(sign convention: capex is normally negative)")

    fcf_ttm = None
    if fcf_row and len(fcf_row) >= 4:
        fcf_ttm = _sum_first(fcf_row, 4)
    elif ocf_ttm is not None and capex_ttm is not None:
        fcf_ttm = round(ocf_ttm + capex_ttm, 4)
        note_parts.append("free_cf_ttm derived as operating_cf + capex")
    if fcf_ttm is None:
        note_parts.append("free_cf_ttm unavailable")

    fcf_yield = None
    if fcf_ttm is not None and market_cap is not None and market_cap > 0:
        fcf_yield = round(fcf_ttm / market_cap * 100.0, 1)

    return {
        "operating_cf_ttm": ocf_ttm,
        "free_cf_ttm": fcf_ttm,
        "capex_ttm": capex_ttm,
        "fcf_yield_pct": fcf_yield,
        "note": "; ".join(note_parts) if note_parts else
                "TTM figures are the sum of the last 4 reported quarters",
    }


def build_estimates(info):
    return {
        "eps_forward_estimate": _r(info.get("forwardEps"), 2),
        "revenue_forward_estimate": None,  # not exposed in ticker.info
        "target_mean_price": _r(info.get("targetMeanPrice"), 2),
        "target_median_price": _r(info.get("targetMedianPrice"), 2),
        "analyst_count": (_r(info.get("numberOfAnalystOpinions"), 0)),
        "recommendation_mean": _r(info.get("recommendationMean"), 2),
        "recommendation_key": info.get("recommendationKey"),
    }


def build_earnings_surprises(ticker, warnings):
    """Last 4 quarters of EPS estimate vs actual. earnings_history is absent
    on some yfinance versions, so every access is guarded."""
    try:
        hist = ticker.earnings_history
    except Exception as e:
        warnings.append(f"earnings_history: unavailable ({e})")
        return []
    if hist is None or not isinstance(hist, pd.DataFrame) or hist.empty:
        warnings.append("earnings_history: empty or unavailable from yfinance")
        return []

    df = hist.copy()
    df.columns = [str(c).strip() for c in df.columns]
    col_map = {c.lower(): c for c in df.columns}
    c_est = col_map.get("epsestimate")
    c_act = col_map.get("epsactual")
    c_surp = col_map.get("surprisepercent")
    c_qtr = col_map.get("quarter")

    rows = df.tail(4)
    surprises = []
    for _, row in rows.iterrows():
        est = _r(row[c_est]) if c_est else None
        act = _r(row[c_act]) if c_act else None
        surp = _r(row[c_surp]) if c_surp else None
        if surp is not None:
            # yfinance reports surprisePercent as a fraction (0.05 == 5%).
            if abs(surp) <= 1.0:
                surp = round(surp * 100.0, 2)
            else:
                surp = round(surp, 2)
        elif est is not None and act is not None and est != 0:
            surp = round((act - est) / abs(est) * 100.0, 2)

        quarter_label = None
        if c_qtr and row[c_qtr] is not None:
            quarter_label = str(row[c_qtr])
        elif hasattr(row.name, "strftime"):
            quarter_label = row.name.strftime("%Y-%m-%d")

        surprises.append({
            "quarter": quarter_label,
            "eps_estimate": est,
            "eps_actual": act,
            "surprise_pct": surp,
        })
    return surprises


# ---------------------------------------------------------------------------
# Output scaffolding
# ---------------------------------------------------------------------------

def empty_output(ticker):
    return {
        "ticker": ticker,
        "company_name": None,
        "sector": None,
        "industry": None,
        "market_cap": None,
        "shares_outstanding": None,
        "as_of_date": date.today().strftime("%Y-%m-%d"),
        "fundamentals_source": "yfinance",
        "data_quality": {
            "info_available": False,
            "quarterly_financials_available": False,
            "balance_sheet_available": False,
            "cashflow_available": False,
            "warnings": ["output scaffold only; no data fetched"],
        },
        "valuation": {k: None for k in (
            "pe_trailing", "pe_forward", "peg_ratio", "price_to_book",
            "enterprise_to_ebitda", "price_to_sales", "dividend_yield",
            "market_cap", "enterprise_value")},
        "growth": {k: None for k in (
            "revenue_yoy_pct", "earnings_yoy_pct", "revenue_qoq_pct",
            "earnings_qoq_pct", "revenue_3yr_cagr_pct", "note")},
        "profitability": {k: None for k in (
            "gross_margin_pct", "operating_margin_pct", "net_margin_pct",
            "return_on_equity_pct", "return_on_assets_pct", "fcf_margin_pct")},
        "balance_sheet": {k: None for k in (
            "debt_to_equity", "current_ratio", "quick_ratio", "cash_to_debt",
            "net_debt", "working_capital", "note")},
        "cash_flow": {k: None for k in (
            "operating_cf_ttm", "free_cf_ttm", "capex_ttm",
            "fcf_yield_pct", "note")},
        "estimates": {k: None for k in (
            "eps_forward_estimate", "revenue_forward_estimate",
            "target_mean_price", "target_median_price", "analyst_count",
            "recommendation_mean", "recommendation_key")},
        "earnings_surprises": [],
        "fetch_timestamp": datetime.now(timezone.utc)
                          .strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def build_output(ticker):
    out = empty_output(ticker)
    warnings = []
    out["data_quality"]["warnings"] = warnings

    try:
        import yfinance as yf
    except ImportError as e:
        warnings.append(f"yfinance import failed ({e}); "
                        "all data sections are null")
        return out

    try:
        tk = yf.Ticker(ticker)
    except Exception as e:
        warnings.append(f"yfinance Ticker construction failed ({e})")
        return out

    # --- info (valuation ratios, margins, metadata) -----------------------
    info = {}
    try:
        info = tk.info or {}
        if not isinstance(info, dict):
            info = {}
    except Exception as e:
        warnings.append(f"ticker.info fetch failed ({e})")
        info = {}

    # info_available: require more than a bare-error stub dict.
    info_available = len(info) > 5 and any(
        info.get(k) is not None for k in
        ("longName", "marketCap", "trailingPE", "sector"))
    out["data_quality"]["info_available"] = bool(info_available)
    if not info_available:
        warnings.append("ticker.info returned nothing useful "
                        "(ETF/ADR/unknown ticker, or yfinance outage)")

    out["company_name"] = info.get("longName") or info.get("shortName")
    out["sector"] = info.get("sector")
    out["industry"] = info.get("industry")
    out["market_cap"] = _r(info.get("marketCap"), 0)
    out["shares_outstanding"] = _r(info.get("sharesOutstanding"), 0)

    # --- quarterly statement frames ---------------------------------------
    quarterly_financials = _fetch_frame(
        lambda: tk.quarterly_financials, warnings,
        "quarterly_financials")
    out["data_quality"]["quarterly_financials_available"] = \
        quarterly_financials is not None

    annual_financials = _fetch_frame(
        lambda: tk.financials, warnings, "annual_financials")

    balance_sheet = _fetch_frame(
        lambda: tk.quarterly_balance_sheet, warnings,
        "quarterly_balance_sheet")
    out["data_quality"]["balance_sheet_available"] = balance_sheet is not None

    cashflow = _fetch_frame(
        lambda: tk.quarterly_cashflow, warnings,
        "quarterly_cashflow")
    out["data_quality"]["cashflow_available"] = cashflow is not None

    # --- sections (each independently guarded) ----------------------------
    try:
        out["valuation"] = build_valuation(info)
    except Exception as e:
        warnings.append(f"valuation section failed ({e})")

    market_cap = _r(info.get("marketCap"), 0)
    revenue = _get_row(quarterly_financials, ["Total Revenue"])
    revenue_ttm = _sum_first(revenue, 4) if revenue else None

    try:
        out["growth"] = build_growth(info, quarterly_financials,
                                     annual_financials, warnings)
    except Exception as e:
        warnings.append(f"growth section failed ({e})")

    try:
        out["profitability"] = build_profitability(info, cashflow,
                                                   revenue_ttm)
    except Exception as e:
        warnings.append(f"profitability section failed ({e})")

    try:
        out["balance_sheet"] = build_balance_sheet(info, balance_sheet)
    except Exception as e:
        warnings.append(f"balance_sheet section failed ({e})")

    try:
        out["cash_flow"] = build_cash_flow(cashflow, revenue_ttm, market_cap)
    except Exception as e:
        warnings.append(f"cash_flow section failed ({e})")

    try:
        out["estimates"] = build_estimates(info)
    except Exception as e:
        warnings.append(f"estimates section failed ({e})")

    try:
        out["earnings_surprises"] = build_earnings_surprises(tk, warnings)
    except Exception as e:
        warnings.append(f"earnings_surprises section failed ({e})")

    if not warnings:
        warnings.append("none")
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Fetch fundamental data for a ticker via yfinance; "
                    "emits a single JSON document to stdout.")
    parser.add_argument("ticker", help="Stock ticker, e.g. HALO")
    args = parser.parse_args()

    try:
        output = build_output(args.ticker)
    except Exception as e:
        # Catastrophic failure: still honor the JSON error contract
        # (same shape as market_pricing.py).
        json.dump({"error": str(e), "ticker": args.ticker}, sys.stdout)
        sys.exit(1)

    json.dump(output, sys.stdout)


if __name__ == "__main__":
    main()
