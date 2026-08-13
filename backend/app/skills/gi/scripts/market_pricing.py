#!/usr/bin/env python3
"""
market_pricing.py - GI transform script.

Fetches daily OHLCV history for a ticker (yfinance primary, Stooq fallback),
computes technical context (returns, range position, percentile rank, Bollinger
%b, z-scores, moving averages, RSI, realized vol, roll-gap detection), resolves
named date anchors, and emits a single JSON document to stdout.

Decision signals (nested under "decision_signals" in the output):
    - tug_of_war: Lou-Polk-Skouras overnight vs intraday return decomposition
      (institutional vs retail/sentiment dominance).
    - shelf_dwell: consecutive days price dwells within ±1.5 ATR of the
      lookback-day high (anchored-supply absorption).
    - amihud_gradient: Amihud illiquidity trend (|ret| / dollar volume);
      rising = thin/fragile tape, falling = deep absorption.
    - wick_asymmetry: volume-weighted upper/lower wick imbalance with 252d
      z-score (climactic rejection vs capitulation defense).
    - counter_leverage_vol: Black(1976) leverage-effect check; rising realized
      vol near highs = two-sided urgency / distribution.

Usage:
    python market_pricing.py TICKER START_DATE END_DATE [--anchor NAME=DATE]...
            [--anchor-before NAME=DATE]...

Anchor semantics:
    --anchor         NAME=DATE resolves to the first trading day ON OR AFTER
                     DATE (event-day pricing; the default and usually what you
                     want for "price at announcement").
    --anchor-before  NAME=DATE resolves to the last trading day ON OR BEFORE
                     DATE (rare; use when the anchor marks a period end and
                     same-day movement should be excluded).

Output conventions:
    - Fields suffixed `_pct` are percentages on a 0-100 scale
      (e.g. realized_vol_20d_pct, vol_percentile_2y_pct).
    - percentile_rank_252d and range_position* are fractions on a 0-1 scale.
    - bollinger_pctb is a fraction that may lie outside 0-1.
    - Within "decision_signals": tug_of_war, amihud_* and
      counter_leverage_vol_trend are log-return log-scales; wick_asymmetry is
      normalized to [-1, +1]; *_pctile fields are 0-100.

Dependencies: yfinance, pandas, numpy (plus stdlib).
"""

import argparse
import hashlib
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from math import log, sqrt

# Chart generation (optional - only imports when --chart flag is used)
try:
    from chart_svg import generate_price_svg, standalone_html
    _HAS_CHART = True
except ImportError:
    _HAS_CHART = False

import numpy as np
import pandas as pd

CACHE_DIR = "/tmp/market_cache"
CACHE_TTL_HOURS = 24.0
TRADING_DAYS = 252

# Consolidated commodity metadata.
COMMODITIES = {
    "KC=F": {"name": "Coffee",       "unit": "US cents/lb",     "stooq": "kc.f"},
    "CC=F": {"name": "Cocoa",        "unit": "USD/MT",          "stooq": "cc.f"},
    "ZW=F": {"name": "Wheat",        "unit": "US cents/bushel", "stooq": "zw.f"},
    "ZC=F": {"name": "Corn",         "unit": "US cents/bushel", "stooq": "zc.f"},
    "ZS=F": {"name": "Soybeans",     "unit": "US cents/bushel", "stooq": "zs.f"},
    "SB=F": {"name": "Sugar",        "unit": "US cents/lb",     "stooq": "sb.f"},
    "CT=F": {"name": "Cotton",       "unit": "US cents/lb",     "stooq": "ct.f"},
    "OJ=F": {"name": "Orange Juice", "unit": "US cents/lb",     "stooq": "oj.f"},
    "LE=F": {"name": "Live Cattle",  "unit": "US cents/lb",     "stooq": "le.f"},
    "HE=F": {"name": "Lean Hogs",    "unit": "US cents/lb",     "stooq": "he.f"},
    "GC=F": {"name": "Gold",         "unit": "USD/troy oz",     "stooq": "gc.f"},
    "SI=F": {"name": "Silver",       "unit": "USD/troy oz",     "stooq": "si.f"},
    "CL=F": {"name": "Crude Oil",    "unit": "USD/barrel",      "stooq": "cl.f"},
    "NG=F": {"name": "Natural Gas",  "unit": "USD/MMBtu",       "stooq": "ng.f"},
    "HG=F": {"name": "Copper",       "unit": "US cents/lb",     "stooq": "hg.f"},
    "ES=F": {"name": "E-mini S&P",   "unit": "index points",    "stooq": "es.f"},
    "NQ=F": {"name": "E-mini Nasdaq","unit": "index points",    "stooq": "nq.f"},
}


def commodity_name(t):
    info = COMMODITIES.get(t)
    return info["name"] if info is not None else None


def commodity_unit(t):
    info = COMMODITIES.get(t)
    return info["unit"] if info is not None else None


def stooq_symbol(t):
    info = COMMODITIES.get(t)
    if info is not None:
        return info["stooq"]
    return t.lower().replace("^", "").replace("-", "")


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def _cache_path(ticker, start, end):
    key = hashlib.sha256(f"{ticker}|{start}|{end}".encode()).hexdigest()[:20]
    return os.path.join(CACHE_DIR, f"{key}.json")


def _read_cache(ticker, start, end, ttl_hours=CACHE_TTL_HOURS):
    """Return (df, source_label, age_hours) or None.

    Entries older than `ttl_hours` are treated as misses. The stored origin
    source (yahoo/stooq) is surfaced as "cache(yahoo)" / "cache(stooq)".
    """
    path = _cache_path(ticker, start, end)
    if not os.path.exists(path):
        return None
    try:
        age_seconds = time.time() - os.path.getmtime(path)
        if age_seconds > ttl_hours * 3600.0:
            return None
        with open(path) as f:
            payload = json.load(f)
        df = pd.DataFrame(payload["bars"])
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date")
        origin = payload.get("source", "unknown")
        if not str(origin).startswith("cache"):
            origin = f"cache({origin})"
        return df, origin, age_seconds / 3600.0
    except Exception as e:
        print(f"warning: cache read failed ({e}); refetching", file=sys.stderr)
        return None


def _write_cache(ticker, start, end, df, source):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        bars = df.reset_index().rename(columns={"index": "date"})
        bars["date"] = pd.to_datetime(bars["date"]).dt.strftime("%Y-%m-%d")
        payload = {"source": source,
                   "bars": bars[["date", "open", "high", "low", "close", "volume"]].to_dict("records")}
        with open(_cache_path(ticker, start, end), "w") as f:
            json.dump(payload, f)
    except Exception as e:
        print(f"warning: cache write failed ({e})", file=sys.stderr)


def _normalize(df):
    """Coerce an arbitrary OHLCV frame into our standard shape."""
    df = df.copy()
    df.columns = [str(c).lower() for c in df.columns]
    if isinstance(df.index, pd.DatetimeIndex):
        df = df.reset_index()
    elif "date" not in df.columns and "Date" not in df.columns:
        first = df.columns[0]
        df = df.rename(columns={first: "date"})
    df.columns = [str(c).lower() for c in df.columns]
    df = df.rename(columns={"datetime": "date"})
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
    for col in ("open", "high", "low", "close", "volume"):
        if col not in df.columns:
            df[col] = np.nan
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df[["date", "open", "high", "low", "close", "volume"]]
    df = df.dropna(subset=["close"]).sort_values("date")
    return df.set_index("date")


def _retry(fn, attempts=3, base_delay=1.0, label="fetch"):
    last_exc = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:
            last_exc = e
            if i < attempts - 1:
                time.sleep(base_delay * (2 ** i))
    raise last_exc or RuntimeError(f"{label} failed")


def _fetch_yahoo(ticker, start, end):
    import yfinance as yf

    def _go():
        end_next = (pd.Timestamp(end) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        df = yf.download(ticker, start=start, end=end_next,
                         interval="1d", auto_adjust=False,
                         progress=False, timeout=30)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        if df is None or df.empty:
            raise RuntimeError(f"yahoo returned no data for {ticker}")
        return df

    return _normalize(_retry(_go, label="yahoo fetch")), "yahoo"


def _fetch_stooq(ticker, start, end):
    symbol = stooq_symbol(ticker)
    d1 = start.replace("-", "")
    d2 = end.replace("-", "")
    url = f"https://stooq.com/q/d/l/?s={symbol}&d1={d1}&d2={d2}&i=d"

    def _go():
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        if not raw.strip() or "Exceeded the daily hits limit" in raw:
            raise RuntimeError(f"stooq error for {symbol}: {raw[:120]!r}")
        df = pd.read_csv(io.StringIO(raw))
        if df.empty or "close" not in {c.lower() for c in df.columns}:
            raise RuntimeError(f"stooq returned no data for {symbol}")
        return df

    return _normalize(_retry(_go, label="stooq fetch")), "stooq"


def fetch_bars(ticker, start, end):
    """Return (DataFrame indexed by date, source_label, cache_age_hours|None)."""
    cached = _read_cache(ticker, start, end)
    if cached is not None:
        df, source, age_hours = cached
        return df, source, age_hours

    errors = []
    try:
        df, source = _fetch_yahoo(ticker, start, end)
    except Exception as e:
        errors.append(f"yahoo: {e}")
        try:
            df, source = _fetch_stooq(ticker, start, end)
        except Exception as e2:
            errors.append(f"stooq: {e2}")
            raise RuntimeError(
                f"all fetch sources failed for {ticker} [{' ; '.join(errors)}]")

    df = df.loc[start:end]
    if df.empty:
        raise RuntimeError(f"no bars for {ticker} within {start}..{end}")
    _write_cache(ticker, start, end, df, source)
    return df, source, None


# ---------------------------------------------------------------------------
# Indicators (manual numpy/pandas - no TA libs)
# ---------------------------------------------------------------------------

def rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    delta = closes.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    val = rs.iloc[-1]
    if pd.isna(val):
        return None
    return round(float(100.0 - 100.0 / (1.0 + val)), 2)


def true_atr(df, period=20):
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    return atr


def realized_vol(closes, period=20, ann=TRADING_DAYS):
    """Annualized realized vol from log returns, returned as a FRACTION
    (e.g. 0.18 for 18%). Callers convert to percent at output time."""
    rets = np.log(closes / closes.shift(1)).dropna()
    if len(rets) < period:
        return None
    return round(float(rets.iloc[-period:].std(ddof=1) * np.sqrt(ann)), 4)


def detect_roll_gap(df, mult=3.0, atr_period=20, window=5):
    """Flag any single-day close-to-close move > mult * trailing 20d ATR."""
    atr = true_atr(df, atr_period)
    move = (df["close"] - df["close"].shift(1)).abs()
    ratio = move / atr
    flagged = ratio[ratio > mult].dropna()
    if flagged.empty:
        return False, []
    details = [
        {
            "date": idx.strftime("%Y-%m-%d"),
            "move": round(float(move.loc[idx]), 4),
            "atr_20": round(float(atr.loc[idx]), 4),
            "atr_multiple": round(float(ratio.loc[idx]), 2),
        }
        for idx in flagged.index[-window:]
    ]
    return True, details


def vol_percentile_2y(hist_prices_list, current_vol_20d_pct, min_obs=40):
    """Percentile rank (0-100) of current 20d vol vs rolling 20d vols in the
    trailing 2y. `current_vol_20d_pct` is a percentage (0-100 scale).
    log() legs on both sides of each return are guarded against nonpositive
    prices."""
    if current_vol_20d_pct is None:
        return None
    if len(hist_prices_list) < 20 + min_obs:
        return None
    rets = [log(hist_prices_list[i] / hist_prices_list[i - 1])
            for i in range(1, len(hist_prices_list))
            if hist_prices_list[i - 1] > 0 and hist_prices_list[i] > 0]
    if len(rets) < 20 + min_obs:
        return None
    from statistics import stdev
    vols = [stdev(rets[i - 20:i]) * sqrt(TRADING_DAYS) * 100
            for i in range(20, len(rets) + 1)]
    return round(100.0 * sum(v <= current_vol_20d_pct for v in vols) / len(vols), 1)


def vol_regime(pct):
    if pct is None:
        return None
    if pct < 25:
        return "low"
    if pct < 70:
        return "normal"
    if pct <= 90:
        return "elevated"
    return "extreme"


def max_drawdown(prices_list):
    """Max peak-to-trough decline, as negative %. e.g. -18.5"""
    if not prices_list:
        return None
    peak, mdd = prices_list[0], 0.0
    for p in prices_list:
        peak = max(peak, p)
        if peak > 0:
            mdd = min(mdd, 100.0 * (p - peak) / peak)
    return round(mdd, 1)


# ---------------------------------------------------------------------------
# Decision-support indicators (orthogonal to the suite above)
# ---------------------------------------------------------------------------

def tug_of_war(df, window=20):
    """Lou-Polk-Skouras (JFE 2019) overnight vs intraday decomposition.

    Overnight returns (close->open) are sentiment/retail driven and revert;
    intraday returns (open->close) are institutional and persist. Decompose:
        overnight_ret = log(open / prev_close)
        intraday_ret  = log(close / open)
    then cumulate each over `window` and take intraday_cum - overnight_cum.

    Returns (tug, overnight_cum_pct, intraday_cum_pct), cumulatives expressed
    as percentages, or (None, None, None) on insufficient data. Bars with
    missing/nonpositive open or close are skipped (NaN-propagated).
    """
    open_ = df["open"].where(df["open"] > 0)
    close = df["close"].where(df["close"] > 0)
    overnight_ret = np.log(open_ / close.shift(1))
    intraday_ret = np.log(close / open_)
    overnight_cum = overnight_ret.rolling(window).sum()
    intraday_cum = intraday_ret.rolling(window).sum()
    o, i = overnight_cum.iloc[-1], intraday_cum.iloc[-1]
    if pd.isna(o) or pd.isna(i):
        return None, None, None
    return (round(float(i - o), 4),
            round(float(o) * 100.0, 2),
            round(float(i) * 100.0, 2))


def shelf_dwell(df, atr_series, lookback=252, band_atr=1.5):
    """Days price spends within band_atr ATR of the lookback-day high.

    Long dwell at the shelf = supply transferred to anchored holders, so a
    later breakout faces thin air; a failure is a cascade risk.
        rolling_high = high.rolling(lookback).max()
        in_shelf     = close >= rolling_high - band_atr * atr

    Returns (dwell_days, shelf_level, dwell_pctile):
        dwell_days   consecutive in-shelf bars ending today (0 if not in shelf)
        shelf_level  the shelf floor (rolling_high - band_atr*atr) today,
                     or None if not in shelf
        dwell_pctile 0-100 percentile of current dwell length vs all dwell
                     episodes in the series (1dp), or None if not in shelf or
                     fewer than 5 episodes exist
    NaN ATR bars fall out of the comparison and are treated as not-in-shelf.
    """
    if len(df) < lookback + 1:
        return 0, None, None
    close, high = df["close"], df["high"]
    rolling_high = high.rolling(lookback).max()
    atr = atr_series.reindex(df.index)
    floor = rolling_high - band_atr * atr
    in_shelf = (close >= floor).fillna(False)

    # Consecutive in-shelf bars ending at the last bar.
    cur = 0
    for v in in_shelf.values[::-1]:
        if not v:
            break
        cur += 1

    # All dwell episodes (consecutive True runs), ordered.
    groups = (~in_shelf).cumsum()
    episode_lengths = []
    for _, g in in_shelf.groupby(groups):
        if g.iloc[0]:
            episode_lengths.append(len(g))

    current_in = bool(in_shelf.iloc[-1])
    dwell_days = cur if current_in else 0
    shelf_level = None
    if current_in and not pd.isna(floor.iloc[-1]):
        shelf_level = round(float(floor.iloc[-1]), 4)

    pctile = None
    if current_in and dwell_days > 0 and len(episode_lengths) >= 5:
        pctile = round(100.0 * sum(e <= dwell_days for e in episode_lengths)
                       / len(episode_lengths), 1)
    return dwell_days, shelf_level, pctile


def amihud_gradient(df, short_window=10, trend_window=20):
    """Amihud illiquidity and its log-space trend.

        dollar_vol   = close * volume
        illiq        = |close.pct_change()| / dollar_vol
        log_illiq    = log of the short_window rolling mean (stability)
        trend        = log_illiq.diff(trend_window)

    Rising trend = thinning tape (fragile); falling = deep absorption.
    Returns (log_illiq_now, trend_now, illiq_pctile_252d) where the percentile
    is on the RAW (unlogged) illiquidity vs the trailing 252d. All three are
    None on insufficient data. Zero/negative prices or volumes are filtered
    out before computing; infinities are replaced with NaN.
    """
    if len(df) < short_window + trend_window + 2:
        return None, None, None
    close = df["close"].where(df["close"] > 0)
    volume = df["volume"].where(df["volume"] > 0)
    dollar_vol = (close * volume).replace(0.0, np.nan)
    daily_return = close.pct_change()
    illiq = (daily_return.abs() / dollar_vol).replace([np.inf, -np.inf], np.nan)

    roll_mean = illiq.rolling(short_window).mean()
    log_illiq = np.log(roll_mean.where(roll_mean > 0))
    trend = log_illiq.diff(trend_window)

    li, tr = log_illiq.iloc[-1], trend.iloc[-1]
    cur_raw = illiq.iloc[-1]
    window = illiq.dropna().iloc[-252:]
    pctile = None
    if not pd.isna(cur_raw) and len(window) >= 20:
        pctile = round(float(100.0 * (window <= cur_raw).mean()), 1)
    return (round(float(li), 4) if not pd.isna(li) else None,
            round(float(tr), 4) if not pd.isna(tr) else None,
            pctile)


def wick_asymmetry(df, short_window=10, lookback=252):
    """Volume-weighted upper-wick vs lower-wick imbalance.

    Upper wicks = rejection at highs (aggressive sellers); lower wicks =
    defense (buyers stepping in). Each wick is measured as a fraction of the
    day's range, volume-weighted (volume relative to its 20d mean), summed
    over short_window, then normalized to [-1, +1]. A 252d z-score > +2 at
    price highs signals climactic exhaustion; < -2 at lows, capitulation.

    Doji bars (rng == 0) contribute a 0 wick fraction. Z-score requires
    `lookback` observations of the asymmetry series; the raw asymmetry is
    returned regardless. Returns (asymmetry, zscore).
    """
    if len(df) < 20 + short_window + 1:
        return None, None
    open_ = df["open"].astype(float)
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)

    upper_wick = high - np.maximum(open_, close)
    lower_wick = np.minimum(open_, close) - low
    rng = (high - low).replace(0.0, np.nan)
    uw_pct = (upper_wick / rng).fillna(0.0)   # doji -> 0
    lw_pct = (lower_wick / rng).fillna(0.0)

    rel_vol = (df["volume"] / df["volume"].rolling(20).mean())
    rel_vol = rel_vol.replace([np.inf, -np.inf], np.nan)

    uw_signal = (uw_pct * rel_vol).rolling(short_window).sum()
    lw_signal = (lw_pct * rel_vol).rolling(short_window).sum()
    total = uw_signal + lw_signal
    asym = ((uw_signal - lw_signal) / total.where(total > 0)).clip(-1.0, 1.0)

    mu = asym.rolling(lookback).mean()
    sd = asym.rolling(lookback).std(ddof=1)
    z = (asym - mu) / sd.where(sd > 0)

    a, zz = asym.iloc[-1], z.iloc[-1]
    return (round(float(a), 4) if not pd.isna(a) else None,
            round(float(zz), 2) if not pd.isna(zz) else None)


def counter_leverage_vol(df, rv_window=20, trend_window=10,
                         high_threshold=0.95, lookback=126):
    """Black (1976) leverage-effect check in log space.

    Healthy uptrend = falling vol; rising vol near highs = two-sided urgency
    (distribution). Realized vol is the annualized std of daily pct returns;
    the trend is a log-diff over trend_window (more stable than pct change),
    percentile-ranked over its own trailing 252d.

    Returns (near_high, vol_trend, vol_trend_pctile). near_high is True when
    the latest close is within (1 - high_threshold) of the trailing lookback
    high (computed on shifted closes so today's bar can't set its own high);
    False by default when history is insufficient.
    """
    if len(df) < lookback + trend_window + 2:
        return False, None, None
    close = df["close"].where(df["close"] > 0)
    rv = close.pct_change().rolling(rv_window).std(ddof=1) * np.sqrt(TRADING_DAYS)
    log_rv = np.log(rv.where(rv > 0))
    vol_trend = log_rv.diff(trend_window)
    vol_trend_pctile = vol_trend.rolling(252).rank(pct=True) * 100.0

    near_high = close > close.rolling(lookback).max().shift(1) * high_threshold
    nh = bool(near_high.iloc[-1]) if not pd.isna(near_high.iloc[-1]) else False
    vt, vp = vol_trend.iloc[-1], vol_trend_pctile.iloc[-1]
    return (nh,
            round(float(vt), 4) if not pd.isna(vt) else None,
            round(float(vp), 1) if not pd.isna(vp) else None)


# ---------------------------------------------------------------------------
# Anchor / date resolution
# ---------------------------------------------------------------------------

def resolve_on_or_before(df, date_str):
    ts = pd.Timestamp(date_str)
    idx = df.index[df.index <= ts]
    if len(idx) == 0:
        return None
    return idx[-1]


def resolve_on_or_after(df, date_str):
    ts = pd.Timestamp(date_str)
    idx = df.index[df.index >= ts]
    if len(idx) == 0:
        return None
    return idx[0]


def range_position(low, high, price):
    if high is None or low is None or price is None or high == low:
        return None
    return round(float((price - low) / (high - low)), 4)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_anchor(s):
    if "=" not in s:
        raise argparse.ArgumentTypeError(
            f"anchor must be NAME=DATE, got {s!r}")
    name, dstr = s.split("=", 1)
    datetime.strptime(dstr, "%Y-%m-%d")  # validate
    return name, dstr


def build_output(ticker, start, end, anchor_specs, anchor_before_specs,
                 hist_closes=None, data_warnings=None, cache_age_hours=None):
    df, source, _fresh_age = fetch_bars(ticker, start, end)
    if cache_age_hours is None:
        cache_age_hours = _fresh_age

    close = df["close"]
    high = df["high"]
    low = df["low"]

    start_date = resolve_on_or_after(df, start)
    end_date = df.index[-1]
    if start_date is None:
        raise RuntimeError(f"no trading day on/after {start} in fetched data")

    # Calendar days between the requested end date and the actual last bar.
    end_bar_staleness_days = max(0, (pd.Timestamp(end) - end_date).days)

    start_price = float(close.loc[start_date])
    end_price = float(close.iloc[-1])
    return_pct = round((end_price / start_price - 1.0) * 100.0, 2)

    max_high = float(high.dropna().max()) if high.notna().any() else None
    min_low = float(low.dropna().min()) if low.notna().any() else None
    rp_now = range_position(min_low, max_high, end_price)

    # Fraction 0-1: share of the last 252 bars' closes <= end_price.
    pct_rank = None
    window = close.dropna().iloc[-252:]
    if len(window) >= 20:
        pct_rank = round(float((window <= end_price).mean()), 4)

    boll_pctb = None
    if len(close.dropna()) >= 20:
        ma20 = close.rolling(20).mean()
        sd20 = close.rolling(20).std(ddof=1)
        upper = ma20 + 2 * sd20
        lower = ma20 - 2 * sd20
        u, l = upper.iloc[-1], lower.iloc[-1]
        if not (pd.isna(u) or pd.isna(l)) and u != l:
            boll_pctb = round(float((end_price - l) / (u - l)), 4)

    def zscore(period):
        w = close.dropna().iloc[-(period + 1):-1] if len(close) > 1 else None
        if w is None or len(w) < period:
            return None
        mu, sd = w.mean(), w.std(ddof=1)
        if sd == 0 or pd.isna(sd):
            return None
        return round(float((end_price - mu) / sd), 2)

    ma = {}
    for period in (20, 50):
        m = close.rolling(period).mean().iloc[-1]
        ma[period] = round(float(m), 4) if not pd.isna(m) else None

    rsi_val = rsi(close, 14)

    # Single unified vol implementation; fractions internally, % at output.
    rv20_frac = realized_vol(close, 20)
    rv60_frac = realized_vol(close, 60)
    rv20_pct = round(rv20_frac * 100.0, 1) if rv20_frac is not None else None
    rv60_pct = round(rv60_frac * 100.0, 1) if rv60_frac is not None else None
    vol_ratio = (round(rv20_frac / rv60_frac, 2)
                 if rv20_frac is not None and rv60_frac is not None
                 and rv60_frac != 0 else None)

    closes_list = [float(c) for c in close.dropna()]
    vp = vol_percentile_2y(hist_closes or closes_list, rv20_pct)
    vr = vol_regime(vp)
    mdd = max_drawdown(closes_list)

    # 20d window return divided by the 1-sigma 20d move implied by current
    # vol; computed on the percentage scale on both sides so units cancel.
    return_vs_vol = None
    if rv20_pct is not None:
        window_ret_pct = ((close.iloc[-1] / close.iloc[-20] - 1.0) * 100.0
                          if len(close) >= 21 else 0.0)
        daily_scale_pct = rv20_pct / np.sqrt(252) * np.sqrt(20)
        if daily_scale_pct is not None and daily_scale_pct != 0:
            return_vs_vol = round(float(window_ret_pct / daily_scale_pct), 2)

    avg_vol_20 = df["volume"].dropna().iloc[-20:]
    avg_volume_20d = int(round(avg_vol_20.mean())) if len(avg_vol_20) else None

    gap_flag, gap_details = detect_roll_gap(df)

    unit = commodity_unit(ticker)

    # ------------------------------------------------------------------
    # Decision-support signals (orthogonal to the indicator suite above)
    # ------------------------------------------------------------------
    atr_series = true_atr(df, 20)
    tug, tug_ovn_pct, tug_intr_pct = tug_of_war(df, window=20)
    shelf_days, shelf_level, shelf_pctile = shelf_dwell(df, atr_series)
    amihud_now, amihud_trend, amihud_pctile = amihud_gradient(df)
    wick_asym, wick_z = wick_asymmetry(df)
    near_high, cl_vol_trend, cl_vol_trend_pctile = counter_leverage_vol(df)

    decision_signals = {
        "tug_of_war": tug,
        "tug_overnight_return_pct": tug_ovn_pct,
        "tug_intraday_return_pct": tug_intr_pct,
        "tug_of_war_note": ("positive = intraday (institutional) dominance; "
                            "negative = overnight (retail/sentiment) dominance"),
        "shelf_dwell_days": shelf_days,
        "shelf_level": shelf_level,
        "shelf_dwell_pctile": shelf_pctile,
        "amihud_illiq": amihud_now,
        "amihud_trend_20d": amihud_trend,
        "amihud_illiq_pctile": amihud_pctile,
        "wick_asymmetry": wick_asym,
        "wick_asymmetry_zscore": wick_z,
        "wick_asymmetry_note": ("positive z-score > 2.0 at price highs = "
                                "climactic selling/exhaustion; negative < -2.0 "
                                "at lows = capitulation/buying"),
        "near_high": near_high,
        "counter_leverage_vol_trend": cl_vol_trend,
        "counter_leverage_vol_trend_pctile": cl_vol_trend_pctile,
    }

    anchors = []

    def _emit_anchor(name, dstr, resolver, mode_label, err_label):
        resolved = resolver(df, dstr)
        if resolved is None:
            anchors.append({
                "name": name, "date": dstr,
                "resolution": mode_label,
                "resolved_date": None,
                "price": None, "return_since_pct": None,
                "range_position_at": None, "range_position_now": rp_now,
                "error": err_label,
            })
            return
        price = float(close.loc[resolved])
        ret_since = round((end_price / price - 1.0) * 100.0, 2)
        # Fixed trailing 252 trading days ending at (and including) the
        # resolved anchor day - independent of the CLI start date.
        df_at = df.loc[:resolved].iloc[-252:]
        h_at = float(df_at["high"].dropna().max()) if df_at["high"].notna().any() else None
        l_at = float(df_at["low"].dropna().min()) if df_at["low"].notna().any() else None
        anchors.append({
            "name": name,
            "date": dstr,
            "resolution": mode_label,
            "resolved_date": resolved.strftime("%Y-%m-%d"),
            "price": round(price, 4),
            "return_since_pct": ret_since,
            "range_position_at": range_position(l_at, h_at, price),
            "range_position_now": rp_now,
        })

    # Default anchor semantics: first trading day ON OR AFTER the given date.
    for name, dstr in anchor_specs:
        _emit_anchor(name, dstr, resolve_on_or_after, "on_or_after",
                     "no trading day on or after anchor date")
    # Opt-in legacy semantics: last trading day ON OR BEFORE the given date.
    for name, dstr in anchor_before_specs:
        _emit_anchor(name, dstr, resolve_on_or_before, "on_or_before",
                     "no trading day on or before anchor date")

    series = [
        {
            "date": idx.strftime("%Y-%m-%d"),
            "close": _num(row["close"]),
            "high": _num(row["high"]),
            "low": _num(row["low"]),
            "volume": _int(row["volume"]),
        }
        for idx, row in df.iterrows()
    ]

    out = {
        "ticker": ticker,
        "commodity": commodity_name(ticker),  # null for non-commodity tickers
        "unit": unit,                         # null for non-commodity tickers
        "source": source,
        "cache_age_hours": (round(cache_age_hours, 2)
                            if cache_age_hours is not None else None),
        "start": start_date.strftime("%Y-%m-%d"),
        "end": end_date.strftime("%Y-%m-%d"),
        "n_bars": len(df),
        "end_bar_staleness_days": end_bar_staleness_days,
        "start_price": round(start_price, 4),
        "end_price": round(end_price, 4),
        "return_pct": return_pct,
        "max_high": round(max_high, 4) if max_high is not None else None,
        "min_low": round(min_low, 4) if min_low is not None else None,
        "range_position": rp_now,                 # fraction 0-1
        "percentile_rank_252d": pct_rank,         # fraction 0-1
        "bollinger_pctb": boll_pctb,
        "zscore_vs_50d": zscore(50),
        "zscore_vs_200d": zscore(200),
        "ma_20": ma[20],
        "ma_50": ma[50],
        "rsi_14": rsi_val,
        "realized_vol_20d_pct": rv20_pct,         # percent 0-100
        "realized_vol_60d_pct": rv60_pct,         # percent 0-100
        "vol_ratio": vol_ratio,
        "vol_percentile_2y_pct": vp,              # percent 0-100
        "vol_regime": vr,
        "max_drawdown": mdd,
        "return_vs_vol": return_vs_vol,
        "avg_volume_20d": avg_volume_20d,
        "decision_signals": decision_signals,
        "anchors": anchors,
        "data_warnings": list(data_warnings or []),
        "series": series,
        "fetch_timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    # Gap field name is instrument-dependent: for futures, a large single-day
    # move in a continuous-series contract usually signals a contract roll;
    # for other instruments it is just an extreme gap.
    if ticker.endswith("=F"):
        out["roll_gap_detected"] = gap_flag
        if gap_details:
            out["roll_gap_details"] = gap_details
    else:
        out["extreme_gap_detected"] = gap_flag
        if gap_details:
            out["extreme_gap_details"] = gap_details

    return out


def add_chart(out, anchor_specs, overlays=None):
    if not _HAS_CHART:
        out["chart_error"] = "chart_svg module not found"
        return out
    prices = [{"date": s["date"], "close": s["close"]} for s in out["series"] if s["close"] is not None]
    if len(prices) < 2:
        out["chart_error"] = "insufficient price points for chart"
        return out
    anchor_dates = {}
    for name, date_str in anchor_specs:
        for a in out.get("anchors", []):
            if a["name"] == name and a.get("resolved_date"):
                anchor_dates[name] = a["resolved_date"]
    pct_for_chart = out["return_pct"]
    if out.get("anchors"):
        first_anchor = out["anchors"][0]
        if first_anchor.get("return_since_pct") is not None:
            pct_for_chart = first_anchor["return_since_pct"]

    from datetime import date as _date
    chart_dates = [_date.fromisoformat(p["date"]) for p in prices]
    chart_prices = [p["close"] for p in prices]

    chart_anchors = []
    for name, resolved in anchor_dates.items():
        chart_anchors.append({"label": name.replace("_", " ").title(),
                              "name": name, "date": _date.fromisoformat(resolved)})

    name = commodity_name(out["ticker"]) or out["ticker"]
    unit = commodity_unit(out["ticker"]) or ""
    title = f"{name} ({out['ticker']})"
    if unit:
        title += f" - {unit}"

    out["chart_svg"] = generate_price_svg(
        dates=chart_dates,
        prices=chart_prices,
        title=title,
        unit=unit,
        anchors=chart_anchors,
        overlays=overlays,
        stats={"max_high": out["max_high"], "min_low": out["min_low"],
               "pct_change": pct_for_chart},
    )
    return out


def _num(x):
    if x is None or (isinstance(x, float) and np.isnan(x)) or pd.isna(x):
        return None
    return round(float(x), 4)


def _int(x):
    if x is None or pd.isna(x):
        return None
    return int(x)


def main():
    parser = argparse.ArgumentParser(
        description="Fetch market pricing + technical context; emits JSON.")
    parser.add_argument("ticker", help="Yahoo ticker, e.g. KC=F")
    parser.add_argument("start", help="Start date YYYY-MM-DD")
    parser.add_argument("end", help="End date YYYY-MM-DD")
    parser.add_argument("--anchor", action="append", type=parse_anchor,
                        default=[], metavar="NAME=DATE",
                        help="Named anchor date; resolves to the first trading "
                             "day ON OR AFTER DATE (event-day pricing).")
    parser.add_argument("--anchor-before", action="append", type=parse_anchor,
                        default=[], metavar="NAME=DATE",
                        help="Named anchor date; resolves to the last trading "
                             "day ON OR BEFORE DATE.")
    parser.add_argument("--chart", action="store_true", default=False,
                        help="Generate SVG price chart and include in JSON output.")
    parser.add_argument("--chart-html", type=str, default=None, metavar="PATH",
                        help="Write standalone HTML chart to this path.")
    parser.add_argument("--overlay", action="append", default=[], metavar="LABEL=START=END",
                        help="Overlay prior event (day-aligned from start). Repeatable.")
    args = parser.parse_args()

    for label, d in (("start", args.start), ("end", args.end)):
        try:
            datetime.strptime(d, "%Y-%m-%d")
        except ValueError:
            json.dump({"error": f"invalid {label} date {d!r}; expected YYYY-MM-DD"},
                      sys.stdout)
            sys.exit(1)
    if args.end < args.start:
        json.dump({"error": "end date is before start date"}, sys.stdout)
        sys.exit(1)

    data_warnings = []
    try:
        hist_start = (datetime.strptime(args.start, "%Y-%m-%d") - timedelta(days=2 * 365 + 42)).strftime("%Y-%m-%d")
        try:
            hist_df, _, _ = fetch_bars(args.ticker, hist_start, args.end)
            hist_closes = [float(c) for c in hist_df["close"].dropna()]
        except Exception:
            data_warnings.append(
                "vol_percentile_2y_basis: in_window_fallback "
                "(2y history fetch failed)")
            hist_closes = None
        output = build_output(args.ticker, args.start, args.end,
                              args.anchor, args.anchor_before,
                              hist_closes=hist_closes,
                              data_warnings=data_warnings)
    except Exception as e:
        json.dump({"error": str(e), "ticker": args.ticker}, sys.stdout)
        sys.exit(1)

    overlays = []
    from datetime import date as _date
    for spec in args.overlay:
        parts = spec.split("=")
        if len(parts) != 3:
            print(f"warning: ignoring bad overlay spec {spec!r}", file=sys.stderr)
            continue
        label, ostart, oend = parts
        try:
            odf, _, _ = fetch_bars(args.ticker, ostart, oend)
            overlays.append({
                "label": label,
                "dates": [_date.fromisoformat(d.strftime("%Y-%m-%d")) for d in odf.index],
                "prices": [float(c) for c in odf["close"]],
            })
        except Exception as e:
            print(f"warning: overlay {label!r} failed: {e}", file=sys.stderr)

    if args.chart or args.chart_html:
        output = add_chart(output, args.anchor + args.anchor_before,
                           overlays=overlays or None)

    if args.chart_html and _HAS_CHART and output.get("chart_svg"):
        from pathlib import Path
        cname = commodity_name(args.ticker) or args.ticker
        Path(args.chart_html).write_text(standalone_html(
            output["chart_svg"],
            title=f"{cname} ({args.ticker})",
        ))

    json.dump(output, sys.stdout)


if __name__ == "__main__":
    main()
