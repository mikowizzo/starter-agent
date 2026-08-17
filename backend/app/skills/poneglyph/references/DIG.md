# GI v2 — `dig`: the exploratory pivot (PROPOSED Slice 8)

Status: **SUPERSEDED 2026-08-15** — the prospect→accept→test→close state
machine was retired after field use. `dig` is now a READ-ONLY graph
synthesis dump (see SKILL.md): the analyst reads it, brainstorms dig sites
with the `innovate` skill, and expands the graph via the normal fetch/claim
loop. Journal replay handlers for `prospect`/`dig_*` events REMAIN so old
cases rebuild identically; only the write paths are gone. This document is
kept as the historical design record.

Historical status: IMPLEMENTED 2026-08-15 (Slice 8 + hardening rounds r1–r4,
with ask-crew reviews after each round; regression-proven on a scratch
case). Ratified in principle 2026-08-15 after an ask-crew architecture
review (Kimi K3, Grok 4.6, Gemini 3.7 + designated heretic). Nothing here
repurposes a frozen v1 field; `dig` is **command-layer sugar over existing
event grammar**.

## 1. The loop this slice serves

```
project → prospect → accept → test → fetch → verdict → (re-)prospect
```

Synthesis over the current graph produces *prospects* — candidate edges
asserting "test me", never "believe me". Prospects drive expansion; expansion
lands direct evidence; evidence corroborates or kills. The loop is an
**exploratory pivot**: the graph's growth is steered by auditable reasoning
about its own gaps.

Design maxim, from the council verbatim: *the journal records that a lead was
generated; it never records that a lead is believed.*

## 2. Command grammar

```
gi2 dig                        show the map (projection: §6)
gi2 dig prospect               survey the graph for promising sites
gi2 dig accept <prospect-id>   analyst commits a prospect to testing
gi2 dig test <prospect-id>     pre-register the kill-first fetch plan
gi2 dig close <id> --verdict corroborated|killed|expired   record the verdict
gi2 dig withdraw <id>          retire a prospect unscored (dead-end)
```

Excavation itself uses existing machinery (`fetch` / transforms / `claim`);
`dig test` only writes the plan *before* the shovels come out. Journal event
ordering is the proof the plan preceded the fetch — seq position is evidence.

## 3. `gi2 dig prospect` — the untrusted novelty feed

A gated transform run, exactly in the Slice 4 pattern, journaling:

```
artifact(snapshot) → transform_run(dig-prospect) → [accepted prospect ops]
```

- **Snapshot**: direct claims + entities ONLY. Hypothesis claims, prior
  prospect packs, and marks are excluded from the model's input by default
  (anti-echo-chamber; the model must not read its own offspring as landscape).
- **Engine**: the innovate framework's lenses (signal tracing, constraint
  relaxation, scale jumping weighted first; analogical mapping weighted last —
  structurally-seductive analogies are the top source of confident-wrong
  leads). The four innovate gates are **generation discipline only**; they are
  never claimed as forensic certification. Self-grading is not a gate.
- **Run identity pinned**: model id, prompt hash, technique config, seed,
  snapshot hash, claim-id set fed to the model. Unpinned = not auditable.
- **Output**: a hash-addressed **prospect pack** artifact (schema §4) plus at
  most 5–8 prospects per run.

### The admission gate (mechanical, verifiable — the quote-gate analogue)

A prospect enters the pack only if **machines** can check all of:

1. **Anchor gate.** Every load-bearing premise resolves to an existing
   **artifact-cited** claim id (either in-graph or landed before the pack is
   filed), and at least one anchor shares an entity with the prospect's
   subject/object (semantic anchor gate — an unrelated true claim warrants
   nothing). Zero anchors ⇒ speculation ⇒ stays in run stderr, never
   journaled. *Ruling (a), principal, 2026-08-15*: the anchor class is
   **artifact-cited**, not strictly `direct` — reader-extracted claims are
   `evidence=inferred` *with gate-verified quotes*, and the quote is the
   warrant. Strict direct-only would leave reader-fed cases anchorless.
2. **Kill criterion.** A concrete, operational observation that would retract
   the prospect, plus the polarity of the disconfirming finding and the source
   class expected to settle it. No kill criterion ⇒ unfalsifiable ⇒ rejected
   by the gate, not by judgment.
3. **Dedupe.** No restatement of existing claims, prior pack theses, retracted
   claims, or dead-end-marked entities. Killed ideas may not resurrect by
   rephrasing.
4. **Quota.** Per-run cap AND a case-level `hypothesis : direct` ratio cap.
   Hypotheses are costless to mint; directs are expensive. Costless classes
   that drive real work get flooded. (Suggested starting ratio: ≤ 1:3.)

Gates that cannot fail are rubber stamps; novelty-vs-incomplete-corpus is
recorded as a *score* in the pack, never as a pass/fail certificate.

## 4. Prospect pack schema (artifact JSON)

```json
{
  "snapshot": "sha256:<hash>",        "claim_ids": ["c_…"],
  "run": {"model": "…", "prompt_hash": "…", "techniques": "…", "seed": 0},
  "prospects": [{
    "id": "p_<16hex>",
    "thesis": "…", "mechanism": "…",
    "anchors": ["c_…"],               // DIRECT claim ids, gate-verified
    "kill_criterion": {"observation": "…", "polarity": "refutes",
                        "source_class": "peer-reviewed longitudinal"},
    "novelty_against": ["c_…"],       "novelty_score": 0.0,
    "entities": ["thing:…"],          // must exist or pass introduce-entity
    "fetch_targets": ["systematic reviews", "RCT meta-analyses"],
    "status": "unreviewed"
  }]
}
```

Mechanism prose may contain no uncited factual clauses — anything not
pointing at an anchor is tagged `unsupported` and blocks `accept`.

## 5. `accept` and `test` — the two human speech acts

**`gi2 dig accept <id>`** — the only write path from pack to claim table.
Files `evidence=hypothesis` claim(s), **confidence 0.0** (not 0.80 — the cap
is theater for things that have survived nothing), `via_run` = the prospect
run. Optional `mark kind=followup` on related entities, analyst hand only.
The pack stays in the CAS as provenance.

**`gi2 dig test <id>`** — writes a fetch-plan artifact BEFORE any fetching:

- queries in **both polarities** (the disconfirming query is mandatory);
- kill-first framing: the task is *find the evidence most likely to kill H*;
  corroboration found incidentally may be filed, but the plan hunts disproof;
- subsequent fetch/transform runs reference the plan in `args`, so lineage
  `prospect → expansion-run → direct claims → verdict` is replayable.

**Resolution.** Only artifact-cited claims corroborate or kill (ruling (a)
extended, 2026-08-15 — same warrant logic as anchors; in practice almost
always `direct`, since reader `inferred` claims carry the same quote gate).
`corroborated` requires `--evidence <claim-id>+`, each active, non-superseded,
and touching the prospect's subject or object; both `corroborated` and
`killed` are **tested-gated** — no verdict without a prior journaled
kill-first plan. The proposing run may never resolve its own prospects.
Extraction is blinded to the thesis: extract all gate-verified claims from
the artifact, *then* match to the prospect. Lifecycle:
`unreviewed → accepted → tested → corroborated | killed | expired(stale)` —
verdicts are final (terminal states refuse further lifecycle writes),
`corroborated` frees quota but leaves the hypothesis claim active;
`killed` retracts it unscored; killed and corroborated edges are deduped
**forever** (copy-paste resurrection is impossible); `withdrawn` and
`expired` release the edge for re-prospecting (run-id nonce prevents
collision). Transform evidence classes are policed by
`TRANSFORM_EVIDENCE_POLICY` — no transform may ever mint `direct`.

## 6. `gi2 dig` — the projection

A query over prospect runs, their packs, active hypothesis claims, and
marks — rendered like the frontier it is: prospects by status and
**staleness** (neglect must be visible, not silent). Nothing in this
projection touches belief; hypothesis claims contribute zero evidential
mass and are excluded from default belief queries (`WHERE evidence_class IN
('direct','inferred')` — firewall at the projection layer).

## 7. Guardrails (council findings, binding)

| Failure mode | Guard |
|---|---|
| Lead inflation | per-run quota + H:D ratio cap; each prospect names the gap it fills |
| Self-reinforcing frontier | prospect snapshots are direct-only; unresolved prospects cannot spawn children |
| Sycophancy / self-resolution | proposer ≠ resolver, ever; red-team is a separate model+prompt, journaled as its own run |
| Confirmation-shaped fetching | plan-before-fetch, both polarities, kill-first, blinded extraction |
| Zombie prospects | expiry by staleness after N runs; dead-end marks surface in `dig` |
| Unreproducible frontier | run identity fully pinned; replay divergence = quality signal, not more leads |

**Marks honesty clause.** Once `followup` reorders `dig` output and fetch
priority, "marks never affect belief" holds in schema but not in effect.
Marks are hereby documented as an **attention-allocation layer** with
epistemic consequence — governed by requiring the analyst's hand.

## 8. Retract-cascade — DECIDED 2026-08-15 (principal ruling)

**Non-cascade + flags.** If a prospect run is retracted, its prospects
withdraw from the `dig` view, but every direct claim its plans motivated
**survives** — each passed the quote gate on its own merits; the selection
bias lived in the plan, not the artifacts. Those directs surface under
"orphaned evidence" in `gi2 dig` (claims whose fetch run referenced a plan
of a retracted prospect), so downstream judgment sees the lineage without
amputating valid evidence.

*As built (2026-08-15)*: lineage is journaled via `dig test` writing the
plan artifact (hash) and `fetch`/`run` accepting `--arg dig_plan=<hash>`,
recorded in `transform_runs.args` — replayable, queryable, and visible in
the map's orphaned-evidence section. The retracted-run quarantine follows
`retract-run` semantics already frozen in v1.

## 9. What the journal records — and never records

Records: that a prospect was generated (pinned run), what it anchored to
(direct claim ids), its kill criterion (first-class), the plan that tested it
(seq order), and every direct claim that resolved it.

Never records: that a prospect was *believed*. Confidence 0.0 is not a small
opinion — it is the absence of one, made machine-legible.
