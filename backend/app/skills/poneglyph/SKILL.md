---
name: poneglyph
description: Poneglyph — forensic graph investigation tool (formerly GI v2). Append-only hash-chained journal, content-addressed evidence, derived SQLite projection, subjective-logic belief engine, identity merge/unmerge, HTML companions, brief/lint/dig reorientation. Use for investigations where provenance must survive an audit.
license: MIT
---

# Poneglyph — Graph Investigator, journal edition

*Carved in stone: records of true history that cannot be rewritten, whose
meaning emerges only when connected.*

A single-binary research tool for building evidence graphs where every claim
traces to a verbatim quote in a hash-verified artifact.

## Architecture (one sentence each)

- **Journal** (`journal.ndjson`) — append-only NDJSON events, SHA-256 hash-chained.
  The only source of truth. Never edited; corrected by new events.
- **Evidence store** (`evidence/sha256/ab/…`) — content-addressed artifacts
  (files stored by their own hash; re-hashed on every read).
- **Projection** (`case.db`) — disposable SQLite rebuilt from the journal.
  `claims` are stored as **filed** (original ids) and exposed through a VIEW
  that resolves identity aliases onto canonical entity ids.
- **Belief kernel** (`scripts/belief.py`) — subjective-logic opinion fusion.
  Claims citing the same artifact are correlated (fused once); independent
  artifacts corroborate. Verdicts (SUPPORTED/REFUTED/DISPUTED/UNKNOWN) are
  computed at query time, never stored.
- **Reputation** (Slice 5, `scripts/reputation.py`) — sources earn Beta
  track records from the journal itself: corroborations earn α, scored
  retractions and contra-majority earn β. The multiplier can only discount
  a claim's stated confidence (never inflate it); a whole transform run
  can be withdrawn in one scored command (`retract-run`).
- **Counterfactuals** (Slice 6, `scripts/counterfactual.py`) — `whatif`
  re-folds every edge with a source masked (`exclude`: fabricated source
  vanishes; `floor`: unreliable source hedged to 0.10) and diffs the
  verdicts: FLIPS / WEAKENED / STRENGTHENED. `loadbearing` computes each
  artifact's marginal contribution and a greedy approximate minimum cut.
  Both are PURE QUERY — no journal events, no fingerprints.
- **Pivot loop** (Slice 7, `scripts/pivot.py`) — `neighbors` ranks neighbours
  by an explainable score (edge belief + disputed bump, divided by hub factor
  and context-node factor), collapsed to one row per neighbour (strongest edge
  wins, edge count annotated); `expand` walks ranked BFS rings under a hard
  node budget so hubs can't flood the view. Visited-state and the breadcrumb
  trail live in `.session.json` — navigation is not evidence. But `mark`
  (suspicious / interesting / cleared / dead-end / followup) IS a journal
  event: analyst judgment is evidence, and marks survive rebuilds.
- **Identity** — `merge`/`unmerge` are journal events; the alias map is
  union-find rebuilt at replay. Claims are never rewritten; absorbed ids
  resolve through the map.
- **Transforms** (Slice 4) — `poneglyph.py run NAME --uri URL`: the host fetches
  the artifact into the CAS, then runs `scripts/transforms/NAME.py` as a
  jailed subprocess (no network). The transform reads the artifact on
  stdin and emits NDJSON claims on stdout; the gate re-verifies every
  quote against the host-stored artifact before journaling. Fabricated
  citations are rejected individually; `run_id` provenance links every
  accepted claim to its run (`via_run`), so a whole run can be retracted
  later. Transforms are deterministic parsers (e.g. `wiki`); the pipe
  contract equally admits LLM readers — the gate doesn't care who reads,
  only that every quote verifies. The `llm` reader (`transforms/llm.py`,
  see `references/LLM-READER.md`) calls an OpenAI-compatible endpoint
  directly — a deliberate egress exception: the jail was contractual,
  the gate is forensic, and a hallucinating model's fabricated quotes
  all die at the gate. LLM claims are `evidence=inferred` with confidence
  capped at 0.80; each run is frozen verbatim with `via_run` provenance
  and replay never re-executes the reader.
- `fundamentals` — deterministic JSON snapshot → `evidence=inferred`,
  `confidence` capped at 0.80 (the machine ceiling; 1.5 closed the
  laundering hole that let it mint `direct` claims), every quote a
  verbatim `"key": value` pair from the artifact. For snapshots captured
  host-side by v1's
  `market_fundamentals.py` (yfinance): fetch the snapshot on the host
  (yfinance is installed in the host env, not the jail), save it as JSON,
  then `poneglyph run fundamentals --uri file:///abs/path/snapshot.json --arg
  ticker=MSFT`. Valuation/growth/profitability/balance-sheet/cash-flow
  metrics, analyst estimates, and earnings surprises each land as
  `fund:*` / `attr:*` claims; null values never leak (the transform skips
  them). Numbers are observations, not inferences — machine-observed
  facts stay epistemically distinct from analyst-filed `direct` claims
  and from LLM readings of the same artifacts.

## Commands

```
poneglyph.py [--case DIR] init
poneglyph.py entity NAME [--id ID] [--kind KIND] [--attr K=V]...
poneglyph.py ingest FILE | fetch URL          # evidence into the CAS
poneglyph.py claim SUBJ PRED OBJ [--polarity supports|refutes]
         [--evidence direct|inferred|hypothesis] [--confidence F]
         [--artifact sha256:… --quote "..."]
                                [--span START END]  # optional: auto-located when
                                # omitted; refused if quote occurs 0 or >1 times
                                [--strict-entities] # fail on unregistered subj/obj ids
                                [--batch FILE]      # NDJSON lines of the fields above;
                                # staged, validated, journaled in one transaction
poneglyph.py retract CLAIM_ID --reason "..." [--scored]
poneglyph.py retract-run RUN_ID --reason "..."        # withdraw a whole transform
                                           # run; scored by default
poneglyph.py reputation [--json]                    # source track records:
                                           # α corroborations, β punishments,
                                           # reliability multiplier
poneglyph.py whatif SOURCE [--mode exclude|floor]  # SIMULATE discrediting a source
                                           # (run:<id> | sha256:<h> | claim id);
                                           # journal untouched — rehearsal only
poneglyph.py loadbearing SUBJ PRED OBJ [--threshold F]
                                           # which artifacts hold up this edge?
                                           # greedy marginal contribution + cut
poneglyph.py neighbors ENTITY [--limit N] [--no-rep]  # ranked pivot: one row per
                                            # neighbour, hub/visited/mark notes
poneglyph.py expand ENTITY [--depth N] [--budget N]  # ranked BFS rings, cycle-safe
poneglyph.py mark ENTITY KIND [--reason TXT]     # suspicious|interesting|cleared|
                                            # dead-end|followup (journal event)
poneglyph.py trail [--clear]                     # session breadcrumbs (dotfile only)
poneglyph.py merge ABSORBED into SURVIVOR --reason "..."
         [--artifact sha256:… --span START END --quote "..."]
poneglyph.py unmerge ENTITY_ID --reason "..."
poneglyph.py run TRANSFORM --uri URL [--arg K=V]... [--timeout S]
                                          # host fetches → jailed transform
                                          # → gate verifies every quote
                                          # (file:// only if GI2_ALLOW_FILE_URI=1)
poneglyph.py run llm --uri URL [--arg model=deepseek-v4-flash] [--arg focus="..."]
                                          # LLM reader: reads any page, emits
                                          # gated claims (conf ≤ 0.80, inferred)
poneglyph.py dig                                # READ-ONLY graph synthesis: entities,
                                           # claims digest, and a PIVOT BOARD of
                                           # structural signals (unexpanded nodes,
                                           # open hypotheses, intra-lab gaps,
                                           # passive nodes) — Maltego-style
                                           # pivot candidates. The tool reports
                                           # structure; the ANALYST judges which
                                           # pivot matters, then expands via
                                           # fetch/claim (old state machine
                                           # retired 2026-08-15; prospect events
                                           # still replay)
poneglyph.py find-quote SHA QUOTE    # read-only: exact span(s) the gate would
                               # accept, or an ambiguity report (meta tags
                               # often duplicate body text — pick the
                               # unique body instance)
poneglyph.py show [ID] [--min-belief F] [--no-rep]  # --no-rep: skip reputation
poneglyph.py why SUBJ PRED OBJ [--no-rep]           # discounting of confidence
poneglyph.py log [--since N] [--pretty]
poneglyph.py brief                  # 2KB session-start reorientation: envelope,
                               # recent events, open hypotheses, catalysts,
                               # pivot signals, last transform, lint digest
poneglyph.py lint                   # drift & scars: shell-scar patterns, missing
                               # confidence, machine-claim cap violations,
                               # unregistered ids (shared engine w/ brief §7)
poneglyph.py verify [--quick]       # chain + CAS re-hash + all quotes; --quick
                               # skips CAS re-hash for size+mtime-unchanged
                               # artifacts via .verify-checkpoint sidecar
                               # (chain walk + ALL quote checks always run)
poneglyph.py rebuild [--repair]
```

`--case` defaults to `$GI_CASE` or `./case`.

## Conventions for agents filing claims

1. **Let the tool locate spans.** Omit `--span`; pass only `--quote`
   (copied *byte-exactly* from the artifact, tags/entities included — HTML
   entities like `&#8217;` or tags like `<strong>` are part of the text).
   The gate decodes artifacts as UTF-8 with `errors="replace"` and works
   in character offsets on that decode; hand-computing offsets (bytes,
   newline translation) is how errors happen.
2. **Check uniqueness first.** `find-quote SHA QUOTE` lists occurrences and
   their spans. Descriptions duplicated across og:/twitter:/JSON meta tags
   are the standard trap; quote the unique body instance.
3. **Register entities before citing them, with explicit ids.** Ids are
   `kind:slug` with dots→dashes: `entity NAME --id glm-5-3 --kind model`.
   The write edge warns on unregistered ids and suggests close matches;
   `--strict-entities` refuses outright. Id drift splits identity — recover
   with `entity --id DRIFT_ID --kind …` then `merge DRIFT_ID into CANONICAL
   --reason …` (the absorbed id must first be a registered entity).
4. **Batch waves of claims.** One `claim --batch file.jsonl` per research
   wave; per-line results, one journal transaction, no partial writes.

## The forensic promises

1. **No fabricated citations** — a claim/merge is refused at ingest unless the
   quote appears verbatim near the cited span in the cited artifact; `verify`
   re-checks every quote forever.
2. **Correlation is not corroboration** — multiple quotes from one artifact
   count once; belief never inflates by repetition. Internal support/refute
   conflict inside one artifact is surfaced, never buried.
3. **History is never rewritten** — retraction and unmerge are events; the
   projection recomputes, the journal remembers.
4. **Tampering is visible** — hash chain, CAS re-hash, and quote re-checks
   make silent mutation detectable. Torn tails are repairable
   (`rebuild --repair`), in-place edits are not.
5. **Transforms propose, the gate disposes** — a transform can only cite
   the artifact the host fetched for it, and every quote is re-verified
   before journaling. A lying transform wastes its own time, not your case.

## References

- `references/EVENTS.md` — the frozen v1 event grammar (treat as a treaty).
- `references/RENAME.md` — the gi2 → Poneglyph rename record (2026-08-16):
  what moved, what stayed frozen (lockfile, GI2_* env, UA string).
- `references/IMPROVEMENT-PLAN.md` — the APPROVED 2026-08 build plan (6
  phases, correctness → speed → filing safety → HTML companions →
  brief/FTS → verify tiers). Consult before changing poneglyph; log progress
  by editing the plan, not just the code.
- `scripts/belief.py`, `scripts/test_belief.py` — belief kernel + goldens.
- `scripts/transforms/` — the transform pipe contract + wiki pilot +
  `llm` reader; `scratch/test_slice4.py` and `scratch/test_slice4b.py`
  — the gated-run test suites (liar and mock model server included).
- `scratch/gi2-fund/` — fundamentals transform test suite
  (`test_fundamentals.py`, MSFT fixture with nulls/edge cases, span
  verification of every claim against the artifact).
- `references/LLM-READER.md` — the LLM reader's design note
  (egress exception, routing, caps, repair round).
- Project plan: `docs/gi-v2-project-plan.md` in the workspace root.
