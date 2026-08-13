# Innovation Ideas: market_fundamentals.py → Investment Decision Engine

## Convergence Summary (4 models: MiniMax M3, Kimi K3, Qwen 3.8 Max, GLM 5.2)

### 🔴 Cross-Model Resonance (4/4)

**1. Reverse DCF / Expectations Arbitrage**
- Mechanism: A stock price is a compressed forecast. Instead of "what is this worth?", ask "what must be true for this price to be correct?" HALO's P/E gap (30→9.87) means the market prices ~3× earnings growth.
- What to build: `expectations` section — solve for implied perpetual growth rate from market cap, FCF, assumed WACC. Decompose into falsifiable sub-claims. Output an "expectations gap score": cheap stocks with demanding expectations get penalized (value-trap detector).
- Technique: Lateral inversion + first-principles ascent
- Confidence: Moderately grounded (Mauboussin's Expectations Investing, Damodaran reverse-DCF)
- Build cost: Low — uses fields already in the JSON

**2. Peer-Relative Sector Ranking**
- Mechanism: PE of 30 is cheap for software, expensive for utilities, meaningless for pre-revenue biotech. Absolute numbers are uninterpretable without peer context.
- What to build: Batch-run existing script across sector peers. Z-score and percentile-rank every metric. Compute quality-adjusted cheapness composite (Greenblatt/Novy-Marx). Output: "top-quintile growth, bottom-quintile leverage safety, mid-pack cheapness vs 14 peers."
- Technique: Lateral inversion + scale jumping
- Confidence: Moderately grounded (standard factor-investing practice)
- Build cost: Medium — needs sector universe database

**3. Failure-Mode Pre-Mortem Engine**
- Mechanism: Klein (2007): imagining failure before acting nearly triples risk discovery. Investment failure has a taxonomy — overpayment, leverage blow-up, secular decline, fraud — each leaves a different fundamental fingerprint.
- What to build: Auto-generate top-5 "ways this loses 30%+" with quantified triggers. Each kill item gets a monitor_field and red_line_value for alerting. Coverage: leverage blow-up, value trap, earnings quality collapse, dilution, refinancing risk.
- Technique: Failure-mode inversion
- Confidence: Moderately grounded
- Build cost: Medium — needs rule library

**4. Reverse DCF + Macro Stress Simulator**
- Mechanism: Leverage is reflexive (Soros; Minsky hedge→speculative→Ponzi). The same D/E that's benign at 60% margins becomes existential if revenue drops 30%. Equity is a call option on the assets.
- What to build: `stress_tests` section running 3 deterministic scenarios: (a) revenue −30% for 4Qs → recompute FCF coverage, cash runway; (b) credit-spread shock → refinancing cost; (c) capex overrun. Classify Minsky-style (hedge/speculative/fragile). For HALO: $2B net debt / $518M FCF = ~3.8yr paydown.
- Technique: Failure-mode inversion + first-principles ascent
- Confidence: Moderately grounded
- Build cost: Medium — needs debt maturity data for full power

### 🟡 Dual/Triple Resonance (2-3/4)

**5. Null Pattern / Missingness Sonar**
- Mechanism: The script's defensive coding is a feature. yfinance nulls identify entity class: ETFs have no earnings; biotechs have wild ROE + null dividends; distressed companies have null forward estimates. A field going null flags corporate events before news catches up.
- What to build: Convert data_quality into structured information_quality layer: field completeness score, entity class inference, staleness tracking, confidence_multiplier scaling downstream signals.
- Technique: Lateral inversion
- Confidence: Plausible
- Build cost: Low — uses existing data_quality fields

**6. Accounting Quality Autopsy (Sloan, Beneish, Piotroski, DuPont)**
- Mechanism: Sloan (1996) accruals anomaly: earnings divorced from cash flow predict underperformance. HALO's Q4-2025 EPS of −$0.24 vs $2.20 estimate contaminates trailing P/E and all downstream ratios. DuPont decomposition reveals ROE of 173.7% is leverage-driven, not quality-driven.
- What to build: earnings_quality section: accrual intensity, surprise-regime classifier (quarantine |surprise|>50%), clean earnings reconstruction, Piotroski F-Score (0–9), Beneish M-Score (fraud), Altman Z-Score (bankruptcy), DuPont ROE decomposition.
- Technique: Failure-mode inversion + signal tracing
- Confidence: Moderately grounded (decades of academic validation)
- Build cost: Low — uses existing fields; scoring formulas are public

**7. Earnings Revision Momentum + Time-Series Tracking**
- Mechanism: PEAD (Bernard & Thomas, 1989) is one of the most persistent anomalies. Analysts revise slowly, herd, and anchor. HALO's surprise sequence (+5.5%, −110% one-time, +5.3%, +25.5%) shows accelerating beats — analysts are structurally behind.
- What to build: Add persistence layer (append-only JSONL). On re-runs: revision_dynamics (delta of forward_eps, target_mean_price), surprise autocorrelation, consensus gap velocity, PEAD signal (fresh beat + rising revisions = tradeable drift).
- Technique: Scale jumping + signal tracing
- Confidence: Moderately grounded
- Build cost: Medium — needs persistence layer

**8. Lifecycle Stage Classifier → Adaptive Valuation**
- Mechanism: Same metric means different things by stage. High growth + negative FCF is normal for early-stage; high FCF yield + low growth = value trap risk. D/E 15 + ROE 174% + FCF yield 4.5% + revenue growth 48% = "levered scaling" not "value stock."
- What to build: Rule-based stage classifier (nascent/growth/transition/harvest/decline/distress). Each stage activates different primary lens and metric weights. For transition: debt paydown modeling; for growth: reinvestment efficiency; for harvest: FCF yield vs cost of capital.
- Technique: Signal tracing + analogical mapping
- Confidence: Plausible
- Build cost: Medium — needs empirical calibration

**9. Decision Journal / Calibration Loop**
- Mechanism: Decision hygiene research: gap between remembered and recorded reasoning is where overconfidence lives. Each run emits a thesis stub; on re-runs, diff against recorded thesis. Over 10+ calls, build a personal Brier score.
- What to build: Each run emits decision_object: thesis, key_assumptions, invalidation_triggers, confidence, position_size_multiplier. Subsequent runs diff fundamental state vs recorded thesis → scorecard. Over time: calibration metrics (Brier score, hit rate by signal).
- Technique: Analogical mapping (OODA loops, medical checklists)
- Confidence: Plausible
- Build cost: Medium — needs persistence + UX

### 🟢 Unique Ideas (1/4)

**10. ⭐ Historical Twin Finder**
- Mechanism: Companies with similar fundamental fingerprints at similar stages tend to experience similar fates. Convert investing from forecasting to retrieval with empirical priors. Counters narrative fallacy directly.
- What to build: Fingerprint vector per ticker-year (z-scored PE, EV/EBITDA, P/B, ROIC, margins, growth, leverage, etc.). Embed in FAISS. Query top-20 analogs from ≥5 years ago. Attach outcomes (1y/3y/5y return, max drawdown, bankruptcy). Output: "Your stock looks like X (2014), Y (2011). Median outcome −8%, worst case −70%."
- Technique: Analogical mapping (medical "patients like this") + signal tracing
- Confidence: Plausible
- Build cost: High — needs FAISS + historical fundamental database
- Model: MiniMax M3 (starred as most creative)

**11. ⭐ Differential Diagnosis Engine**
- Mechanism: A constellation of findings doesn't point to one narrative — it generates a ranked list of candidate diagnoses with prior probabilities. HALO's pattern (high D/E + negative one-quarter EPS + FCF positive + no dividend) → candidates: leveraged buyback (p=0.45), legal settlement, goodwill impairment.
- What to build: Library of ~30 financial signatures. Each is a rule mapping fundamental patterns to candidate narratives with Bayesian priors. Output: differential_diagnoses array with each hypothesis, supporting/refuting data points, and the next discriminating observation.
- Technique: Analogical mapping (medical differential diagnosis) + signal tracing
- Confidence: Plausible
- Build cost: Medium-High — needs curated signature library
- Model: GLM 5.2 (starred as most creative)

**12. Catalyst / Patent / Clinical Trial Graph**
- Mechanism: Fundamentals are backward-looking; catalysts move prices before statements catch up. Biotech value depends on royalty streams, pipeline milestones, patent cliffs, clinical trial outcomes.
- What to build: Sector-aware catalyst graph: clinical trial data, PDUFA dates, patent expirations, revenue concentration, milestone scenario trees. Probability-weighted valuation based on catalyst outcomes.
- Technique: Constraint relaxation + signal tracing
- Confidence: Speculative-Plausible
- Build cost: High — needs external APIs (clinical trials, patents)
- Model: Qwen 3.8 Max

**13. Epidemiological Portfolio Map**
- Mechanism: A portfolio is a population. Sector ETFs are reservoirs, factor exposures are vectors, drawdowns propagate like outbreaks through correlation networks. Classical correlation matrix is a poor diagnostic because correlations are unstable and concentrated in tails.
- What to build: Multi-ticker mode: pairwise co-crash probability matrix, concentration R₀ per ticker (expected holdings affected by 30%+ drop), factor decomposition (Fama-French 5), macro stress overlay (2008/2020/2022 regimes).
- Technique: Analogical mapping (epidemiology) + scale jumping
- Confidence: Speculative
- Build cost: High — needs multi-ticker + return history
- Model: MiniMax M3

## Suggested Build Roadmap

### Phase 1 — "Ship Next Week" (existing data only)
1. DuPont ROE Decomposition (trivial)
2. Accounting Quality Scores (Piotroski, Beneish, Altman Z, Sloan)
3. Null Pattern Entity Fingerprint
4. PE Compression Ratio / Earnings Velocity
5. Reverse DCF Implied Growth

### Phase 2 — "Real Decision Tool" (new data sources)
6. Peer-Relative Sector Ranking
7. Pre-Mortem Kill Switch
8. Balance Sheet Stress Simulator

### Phase 3 — "Differentiated Alpha" (persistence + external data)
9. Revision Momentum Tracker
10. Historical Twin Finder
11. Decision Journal / Calibration Loop
12. Catalyst Graph
