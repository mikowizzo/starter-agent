# Design Review: Shared Market Database ("The Observatory") for Poneglyph

## 1. Background: what exists today

**Poneglyph** (formerly gi2) is a forensic graph-investigation tool. Core design:
- **Evidence vault**: immutable content-addressed artifact store (raw HTML, JSON, PDFs), hash-verified forever
- **Journal**: append-only hash-chained NDJSON of events (claims, entities, merges, artifacts, transforms). 271 events in the flagship case. Claims carry verbatim quotes + byte spans verified against artifact bytes, confidence ceilings (machine claims capped 0.80)
- **Projection**: SQLite views rebuilt from journal (replay = sole truth)
- Cases are self-contained directories (journal + vault + db), mergeable via entity-id discipline

**Ticker X-ray engine** (battle-tested, built by a colleague): produces a rich JSON doc per ticker per run — pricing leg (returns, percentile ranks, Bollinger %b, z-scores, MAs, RSI, realized vol + regime, drawdowns, anchor events, decision signals: tug_of_war, shelf_dwell, amihud_gradient, wick_asymmetry, counter_leverage_vol, alignment_squeeze, gap_adjudication, cost_of_conviction_index, close_print_persistence) + fundamentals leg (valuation/growth/profitability/balance_sheet/cash_flow/estimates/earnings_surprises + data_quality warnings). Data: yfinance primary, Stooq fallback, 24h cache. Currently emits stdout JSON only — files zero claims, stores nothing.

## 2. Proposed architecture

A **standalone shared market database** OUTSIDE any case — a numeric research substrate any case can query:

```
market.db (one DB, owned by no case)
  watchlist    ← driven by case entity registration (ticker entities)
  bars         ← OHLCV daily (and intraday later), the true primary substrate
  stats        ← EVERY xray stat, long format: (ticker, stat_name, asof_date, value, run_id)
  fundamentals ← quarterly snapshots, long-ish format
  ingest_log   ← run_id → started, finished, sources (yfinance/stooq), source hashes
  blobs/       ← raw API responses, gzipped, permanent (market history cannot be re-fetched)
```

**Key decisions and rationale:**

1. **Coverage loop**: watchlist is driven by case graphs — registering a ticker entity in ANY case adds it to the shared watchlist. Scheduled job (daily cron) x-rays every watched name; stats land as time series. Coverage follows curiosity automatically.

2. **Long format for stats**: (ticker, stat_name, asof_date, value, run_id). Schema-immune to engine evolution — new signals appear as new stat_names, no migrations. Wide tables break every time the engine adds a signal.

3. **Bars are primary evidence**: every xray stat derives from OHLCV bars. Bars + raw blobs retained permanently (own blob store, NOT case vaults). Market history is the one thing you cannot re-fetch identically tomorrow.

4. **Three-store separation**:
   - Observatory (numbers, time series, shared) ← market.db
   - Vaults (raw evidence, per case) ← when an investigation wants to CLAIM a market fact, that specific snapshot gets vaulted into the case on demand and cited with quote+span
   - Journals (belief, per case) ← testimony only; routine coverage never pollutes journals (a year of daily bars = 252 rows in DB, NOT 252 journal events)

5. **Single-writer discipline**: the scheduled job is the sole writer; investigations read-only (plus watchlist inserts). No lock contention.

6. **Integrity model**: the DB is OUTSIDE the hash-chained world — integrity is SQLite's + ingest_log source hashes + permanent blobs. Rebuildable in derived layers (stats recomputable from bars), permanent in primary layers (bars, blobs).

## 3. Questions for review

1. **Is the three-store boundary right?** Observatory/vault/journal separation — numbers vs evidence vs belief. Any failure mode where this blurs dangerously?
2. **Long format for stats**: correct call vs wide tables? Any query patterns that suffer? (cross-sectional pivots?)
3. **Watchlist-from-entities loop**: auto-adding tickers when cases register them — sane? Ways it could pollute (e.g. non-ticker entities mistakenly mapped)?
4. **Single-writer + scheduled job**: daily cadence sufficient? Failure/retry handling? What happens when the job is down for 3 days (gaps)?
5. **Provenance**: ingest_log + blobs + source hashes — adequate audit trail for a numeric substrate outside the chain? Or does this undermine the forensic story?
6. **Backfill strategy**: on first watch of a ticker, backfill how much history? (yfinance serves ~years of daily bars)
7. **Cross-case stitching**: ticker symbols as natural stable ids — sufficient?
8. **Schema critique**: what's missing? (corporate actions/splits? ticker changes? delistings? currency? exchange?)
9. **Anything you'd do differently** — name the single biggest architectural risk in this design.

## 4. Constraints (treaty lines)

- Poneglyph core is stdlib-only Python; the market DB tooling may use sqlite3 + the xray engine's existing deps (yfinance), but must not contaminate Poneglyph core
- The DB must never become load-bearing for case truth — cases must be able to lose it entirely and still verify (chain + vaults intact)
- Lazy discipline: querying starts as raw SQL; first-class commands only if SQL proves clumsy
