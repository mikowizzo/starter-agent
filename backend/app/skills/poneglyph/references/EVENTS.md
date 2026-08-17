# GI v2 — Frozen v1 Event Grammar (TREATY)

Status: **FROZEN v1 as of Slice 3; APPENDED by Slice 4** (2026-08-14). New
event types may be appended
in later slices; existing fields must not be repurposed. Unknown ops in old
journals must never crash replay — they are copied through to the projection's
`meta` table as `unknown_op_<seq>`.

Every event carries: `seq` (1-based, gapless), `ts` (ISO-8601 UTC),
`prev_hash` (sha256 of the previous event's canonical JSON), `op`.
Canonical JSON: `json.dumps(e, sort_keys=True, separators=(",", ":"))`.

## case_init
`format: "gi2-journal-v1"`. Always seq 1.

## entity
Registers an id: `id` (e.g. `person:john-smith`), `name`, `kind`
(default `entity`), `attrs` (JSON object; `--attr K=V` pairs merged).
Re-registering an id with different name/kind is refused.

## artifact
Records an evidence object entering the CAS: `hash` (`sha256:<64hex>`),
`uri` (source URL or `file://` path), `size` (bytes).

## claim
Asserts an edge: `subj`, `pred`, `obj` (entity ids **as filed** — never
rewritten by later merges), `polarity` (`supports|refutes`), `evidence`
(`direct|inferred|hypothesis`), `confidence` (float 0–1, or **null =
categorical 1.0** by kernel policy), optional citation (`artifact`,
`span_start`, `span_end` [start, end) character span, `quote` verbatim text),
`claim_id` (`c_` + 16 hex, deterministic from content). A `claim` with a
citation is refused unless the quote verifies against the artifact.
Optional `via_run` (Slice 4): `run_id` of the `transform_run` that produced
this claim; replay must tolerate its absence in older journals.

## retract
Marks a claim inactive: `claim_id`, `reason`. The projection drops it from
belief; the journal keeps it; `why` shows it under "retracted (journal
history)".

**Slice 5 fields:** `scored` (default true for `retract-run`, opt-in for
single retract): the retraction counts against the claim's source reputation
(Beta β+1). `--no-scored` / omitting `--scored` marks cleanup, not fault.
`via_run` (optional, `retract-run` only): provenance that the retraction came
from a run-level withdrawal, not a per-claim decision. Re-asserting a claim
after a scored retraction does NOT launder the source: the projection's
`retractions` table keys on journal seq, so retraction history is replayable
and permanent.

## retract-run  (Slice 5)
Withdraws every active claim produced by one transform run:
`run_id`, `reason`. Emits ONE retract event per active claim from that run
(`via_run` set, `scored` default true) under a single exclusive lock — the
batch is atomic: either all retract events append or none do. Unknown run_id
refused with a list of known runs. Track record: a retracted run's `run_id`
source loses β per retracted claim via the `retractions` table; the multiplier
discounts its future claims in belief (floor 0.10, never inflates). Run-level
retraction after re-assertion: a re-asserted claim retracted by run id is a
new claim — only its own history counts.

## reputation  (Slice 5)
Derived, never journaled: computed at query time from the projection.
Sources = `via_run` (transform runs) or `claim:<id>` (manual claims). Beta
reputation per source: α+1 per **corroboration** (an independent cluster on
the same edge agreeing with this claim's polarity — requires a *different*
artifact, so self-corroboration is structurally impossible); β+1 per
**punishment** (scored retraction, or contra-majority: this claim's cluster
is the weaker side of an edge whose opposing clusters carry combined mass
≥0.7 belief or disbelief). Same-artifact refutes are internal conflict,
never punishment. Multiplier = α/(α+β+2) scaled into [0.10, 1.0] — it can
only discount stated confidence, never inflate it; cold start (no events)
is neutral 1.0 (no privilege, no smear). `--no-rep` on `why`/`show` skips
discounting.

## merge  (Slice 3)
Absorbs one entity into another as an identity alias: `absorbed`,
`survivor`, `reason` (required, non-empty), optional citation (`artifact`,
`span_start`, `span_end`, `quote` — same verification rules as claims).
Semantics: union-find parent map rebuilt at replay; claims resolve through
the map onto canonical ids at query time. Refusals at write time:
self-merge; unknown ids; cycle (would invert an existing chain); survivor
is itself absorbed (must merge into the canonical root); absorbed is
already absorbed elsewhere (unmerge first). Kind mismatch warns but
proceeds.

## unmerge  (Slice 3)
Restores an absorbed entity's independence: `entity_id`, `reason`.
Semantics: removes the alias edge; the entity's claims follow it back out.
If X was merged into A, and A into B, unmerging A restores A's subtree
(X still resolves to A). Refused if the entity is not currently absorbed.

## mark  (Slice 7)
Analyst judgment, journaled: `entity_id`, `kind`, `reason` (optional).
`kind` ∈ suspicious | interesting | cleared | dead-end | followup (refused
otherwise). Marks are *annotations on entities, not evidence about the
world* — they never affect belief or reputation; they surface in `neighbors`,
`expand`, `show` and `log --pretty`. Projection: `marks` table, INSERT OR
REPLACE (idempotent re-marking; a kind *change* is a new event, last wins on
replay). Deliberate contrast: navigation state (visited, breadcrumb trail)
lives in the session dotfile and is NEVER journaled — judgment is evidence,
movement is mood.

## transform_run  (Slice 4)
Provenance for a gated transform run: `transform` (name; resolved to
`scripts/transforms/<name>.py`, path traversal refused), `uri` (fetched by
the HOST — transforms have no network access), `artifact_hash`, `args`
(JSON object of `--arg KEY=VALUE`), `accepted` / `rejected` (counts),
`run_id` (16 hex, derived from transform+uri+ts+args). Journal order:
`artifact` → `transform_run` → accepted claims (each carrying `via_run`),
so a future slice can retract a whole run by `run_id`. The projection keeps
a `transform_runs` table (INSERT OR REPLACE — last run wins on replay).

### The pipe contract
Transform stdin: one JSON doc `{case_dir, artifact_hash, artifact_path,
artifact_text, uri, args}`. Transform stdout: NDJSON lines —
- `entity` ops (`id`, `name`, `kind`, `attrs`): insert-if-new; a differing
  name/kind on an existing id → that line rejected, run continues.
- `claim` ops: citation fields must point at THE RUN'S artifact; the gate
  re-runs `verify_quote_span` on every quote and rejects fabricated
  citations individually. Omitted `evidence` defaults to `inferred`, omitted
  `polarity` to `supports`, omitted `confidence` to null (categorical 1.0).
- `log` ops (`level`, `message`): passthrough to host stderr, never
  journaled.
Non-JSON or non-object lines are skipped with a stderr note. Transform exit
non-zero, or `--timeout` exceeded (default 60 s), fails the whole run and
NOTHING is journaled. `file://` URIs are refused unless
`GI2_ALLOW_FILE_URI=1` (tests only).

**Egress exception (Slice 4b).** The no-network clause above is the
default posture, not an enforced sandbox. The `llm` reader transform
calls an OpenAI-compatible chat-completions endpoint directly
(routing and caps in `references/LLM-READER.md`). The forensic
guarantee is unchanged: the gate re-verifies every quote against the
host-stored artifact, so a hallucinating model cannot poison the
journal — its fabricated citations are rejected individually. LLM
claims are `evidence=inferred`, confidence capped at 0.80; the run's
accepted claims are frozen verbatim with `via_run`, and replay never
re-executes the reader.

## Replay invariants

1. Replay is a pure function of journal bytes → projection bytes.
2. `verify` re-checks: chain hashes, CAS re-hash of every referenced
   artifact, quote/span of every claim citation, and every merge citation.
3. Belief is derived, never stored. Verdicts recompute on every query.
4. Claims are stored as filed; canonical resolution happens in the
   `claims` VIEW, not in the journal.
5. `INSERT OR IGNORE` on claim replay: a duplicate `claim_id` must not
   crash a rebuild (the earliest event wins).

## counterfactuals  (Slice 6 — NOT journal events)

`whatif` and `loadbearing` are pure query over the projection. They append
NOTHING, ever — a counterfactual leaves no fingerprints (T3 proves it: the
journal's last event is byte-identical before and after). This is a frozen
contract of the tool, not an implementation detail.

Mask sources: `run:<run_id>` | `sha256:<hash>` | `c_<hex>` | `claim:<c_hex>`.
Modes: `exclude` (masked claims vanish from fusion — a fabricated source
should not even count as uncertainty) vs `floor` (masked claims stay, every
multiplier floored at the reputation floor 0.10 — unreliable but not lying).
Classification: FLIPS on any verdict change (both directions — removing a
bogus refuter STRENGTHENS an edge), WEAKENED/STRENGTHENED for mass moves,
UNTOUCHED for edges the source never touched.

`loadbearing`: exact minimum cut is NP-hard (hitting set); the greedy
pass recomputes marginals after each removal (correlated crutches: two
weak sources that only together hold an edge). Approximate, labelled so.

The real surgery is Slice 5's `retract-run` — scored, journaled, permanent.
`whatif` is the rehearsal; retract-run is the operation.
