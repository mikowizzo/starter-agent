# Implication Chain Framework

Shared analytical engine for market recap products. Both weekly and overnight recaps load this framework. Each product applies it in a different mode (see their respective framework files for mode-specific depth and output).

## Core Concept

When a major catalyst hits, the market prices the obvious reaction fast. The alpha lives in tracing the implication chain deeper:

- **1st order** — Direct, obvious reaction. Already priced within hours. Crowded. Low edge.
- **2nd order** — Derivative effects: sector spillovers, regional contagion, cross-asset flows. Less obvious, still has positioning edge.
- **3rd order** — Structural or contrarian implications. Counterintuitive. Where real alpha lives, but also where the reasoning is most fragile.

Every chain link must state the **mechanism** — *why* A causes B. No mechanism, no chain link.

## The No-Trade Confidence Rule

Producing zero trades is a success metric, not a failure. A week with 4 "no catalyst" reports and 1 genuine trade is better than a week with 5 forced trades. Most periods do not contain structural catalysts worth multi-week positioning. The value is in filtering noise, not generating volume.

## Event Tiering

Not every event deserves a full chain. Classify each catalyst and **state the tier justification in the output** (one line, citing which criteria were met):

| Tier | Definition | Chain Depth |
|---|---|---|
| **Tier 1** | Structural catalyst — policy shift, geopolitical realignment, regime change, tech breakthrough | Full 1st/2nd/3rd order + positioning trade |
| **Tier 2** | Notable but incremental — surprising data, sector-specific shock, meaningful but not regime-shifting | 1st/2nd order + positioning note |
| **Tier 3** | Contextual, routine — inline data, sentiment shifts, mean-reverting flows | 1st order observation only |

A catalyst hitting fewer than 2 criteria is Tier 3 and gets no chain beyond a one-line market reaction.

## Chain Trigger Rubric

Before extending a chain beyond 1st order, the agent must verify triggers. **If triggers aren't met, stop. Do not fabricate depth.**

**2nd order triggers (need ≥1):**
- Magnitude in the 90th percentile vs. trailing 4 weeks
- Cross-asset confirmation (move spans equities + bonds + FX or commodities)
- Regime relevance (central bank, fiscal policy, geopolitical escalation, structural tech shift)

**3rd order triggers (need 2nd order AND ≥2):**
- The 2nd-order chain contradicts the prevailing market narrative
- A known historical analog exists for the pattern
- Positioning data suggests the consensus is crowded in the opposite direction
- A visible asymmetry exists (e.g., crowded positioning, regime mispricing)

**Catalyst purity test (mandatory):** The 3rd-order trade must derive its edge *from the catalyst itself*, not merely correlate with it. Ask: "Did this catalyst change the path of this theme, or did a pre-existing theme just happen to move on the same day?" If the catalyst didn't cause the edge, it's a separate observation — flag it as such, not a chain derivative. Do not use a structural theme you already liked as a 3rd-order trade just because it moved overnight.

## Decision Gates

After every chain order — including the last order before a trade — the agent must state a **Stop or continue?** gate with **named, observable evidence**. This turns the chain from a storytelling device into a screening tool. If you cannot cite a specific data point, the answer is "stop."

Gate format (use exactly this structure):
```
*Stop or continue? [Continue/Stop] — Evidence: [specific data point or observable]*
```

Good gates cite evidence you could point to on a screen:
> *Continue — Evidence: XLE down 4% but airline ETF (JETS) only up 1.2%, lagging the oil drop magnitude; margin transfer not fully priced.*
> *Continue — Evidence: 2Y yield dropped 6bps but Fed funds futures still pricing 70% odds of hold through October; gap between oil move and rate expectations.*
> *Stop — Evidence: Brent and EUR/USD already moved the full implied magnitude; no positioning gap remaining.*

Bad gates cite narrative assertion:
> *~~Continue — "visible but not yet reflected in positioning"~~* — What positioning? Where? How do you know?

If the answer is "stop," the chain terminates there. No further orders. No trade.

## Positioning Trade Spec

When a chain terminates in an actionable trade, use this template:

```
**Trade:** [Direction] [instrument/sector/pair]
**Thesis:** 1 sentence — the structural mechanism creating the edge
**Edge type:** [Carry / Momentum / Value / Volatility / Event-Driven / Flow-Positioning] — why the market hasn't priced this yet
**Horizon:** [Time range, e.g., 3–6 weeks]
**Liquidation trigger:** [Specific event, date, or condition that ends the trade — not just a time range]
**Conviction:** High / Medium / Low (factors in edge decay: 1st-order = fast-decaying edge → Low; structural = slow-decaying → High)
**Invalidation:** [Specific, observable condition that kills the thesis — a price level, data point, or event, not "if data weakens"]
**Counter-thesis:** [1 sentence steel-manning the consensus view — required for ALL trades]
```

### Instrument Granularity

| Asset Class | Allowed Instruments |
|---|---|
| **FX** | Currency pairs and crosses (EUR/USD, USD/CNH, etc.) |
| **Rates/Bonds** | Tenors and curves (2s10s steepener, 10Y UST, etc.) |
| **Equities** | Sector indices, regional indices, style factors (SXIP, XLK, long EU banks vs EU industrials, etc.) |
| **Commodities** | Contracts and baskets (Brent, copper, gold, etc.) |

## Guardrails

### Forbidden
- Options strikes, spreads, or structures
- Leverage or position sizing recommendations
- Numerical price targets ("EUR/USD to 1.0950")
- Single-stock picks as the primary expression — sectors, factors, and pairs only
- 3rd-order trade without a clear, articulated mechanism
- "As-of" timing language implying a real-time recommendation
- Vague instruments ("long equities") — must name a specific sector, factor, pair, or contract

### Required
- Every positioning trade needs a specific, observable invalidation condition
- Every positioning trade needs a liquidation trigger (event/date, not just a time range)
- Every positioning trade needs a counter-thesis (steel-man the consensus)
- Every trade needs an edge type (why the market hasn't priced this)
- Chains must state the mechanism at each link, not just the outcome
- Decision gates must be stated at each link
## Active Trades Watchlist

Every output includes a watchlist section carrying forward open trades from prior recaps.

**For active trades:**

| Trade | Direction | Entry Date | Entry Level | Current vs Entry | Status | Invalidation Proximity |
|---|---|---|---|---|---|---|
| Short EU industrials (SXIP) | Short | Jun 10 | 540.2 | -1.8% | Active | EUR/USD at 1.158, invalidation at 1.05 — far |

- **Entry Level:** the price/reference level when the trade was first identified
- **Current vs Entry:** how the trade has performed since entry (e.g., "-1.8%", "+0.5%")
- **Invalidation Proximity:** current state of the invalidation condition — how close are we?
- **Status:** Active / Invalidated / Stopped

**When a trade exits (invalidated, stopped, or liquidation trigger hit):**

```
**Closed:** [Trade name]
**Outcome:** Invalidated / Stopped / Liquidation trigger hit
**Result:** [+X% / -X% from entry]
**Post-mortem:** [1 sentence — was the thesis right or wrong? What happened?]
```

Closed-trade post-mortems create a feedback loop that builds credibility. Without them, the product is a stream of ideas with no tracking.

## Sector Taxonomy

Use this taxonomy. Only include sectors with meaningful impact:

- Technology
- Financials
- Energy
- Healthcare
- Industrials
- Consumer Discretionary
- Consumer Staples
- Real Estate
- Materials
- Utilities
- Defense & Aerospace

## Disclaimer

*These are analytical frameworks for tactical positioning consideration, not investment advice. Positioning implications reflect structural analysis of market dynamics. All trades carry risk of loss. Position sizing and risk management are the reader's responsibility.*
