# gi2 Improvement Plan — v2 (2026-08-16)

**Status:** APPROVED after council plan-review (Kimi K3, Grok 4.6,
Gemini 3.7 Flash; $0.48; 35 claims → 30 clusters). Supersedes v1.
**Source trail:** council code-review round (findings baked in below) →
v1 plan → council plan-review round (amendments below).
**Live fixture:** case `ai` (39 entities, 128 claims, 241 events, 60
artifacts, 0 chain defects). Zero data-loss tolerance.
**Invariants — never violate:**
- Journal format FROZEN (FORMAT_VERSION 1). No new ops; extra keys on
  existing ops allowed (replay ignores unknown keys).
- Replay handlers for retired commands stay forever.
- `verify`'s hash-chain walk stays a FULL walk, always; **default
  `verify` stays full CAS re-hash** — tiering is opt-in only
  (council: weakening the default is a policy regression).
- One claim = one cited hash; verification is byte-identity against
  that hash only.
- Extraction deterministic + versioned; new bytes → new hash.
- Confidence defaults are written **at filing time** (`_prepare_claim`,
  `cmd_run` gate) — never inside `belief.py`; historical nulls stay
  categorical 1.0 by kernel policy (treaty).

---

## Phase 1 — Correctness — ✅ COMPLETED 2026-08-16

> All eight items landed (1.0, 1.1, 1.2a, 1.2, 1.3, 1.4, 1.5, 1.8) plus
> the 1.6 supersede pass and 1.7 prevention. Regression gate green:
> 246 events, 120 quotes verified, 0 defects, chain intact. Scarred
> claims superseded with corrected text (seqs 242–245). Twin audit:
> all four `model:` twins were already merged in prior sessions;
> `_entity_dup_guard` + `--force` now prevents the class (tested live:
> `glm-5-2` refused against `glm-5.2`). Remaining 1.7 deliverable:
> `dupes` diagnostic command (fold into Phase 5 alongside `lint`)
> and a `verify --full` re-run of the whole case after Phase 4.
> Original plan below, for the record.

**REGRESSION GATE for the whole phase (Gemini):** before any journal
surgery, replay case `ai`'s unmodified 241-event journal through
`full_rebuild` + `verify_chain`; assert 0 defects, identical counts.
Re-run after every item.

**1.0 Schema versioning (new — Kimi+Grok: load-bearing omission).**
`_projection_is_current` checks table names + watermark only; a stale
pre-1.1 `case.db` looks "current" forever and 1.1's new columns never
appear. Fix: `upsert_meta("schema_version", N)` in `full_rebuild`;
require equality in `_projection_is_current`; bump constant on every
SCHEMA/view change (1.1, 2.4, 5.2). Ships WITH 1.1. Effort: 0.3d.

**1.1 `asserted_ts` / `--as-learned` fix.** Store `ev["ts"]` /
`ev["seq"]` in the claim branch of `apply_event` → `claims_filed`;
expose in `_install_claims_view`; `_temporal_where` already expects it.
Effort: 0.5d (with 1.0).

**1.3 `_derive_claim_id` tritemporal fields — BEFORE 1.2.** Include
`valid_from/valid_to/pub_ts/time_source` in the stable hash dict.
(all three reviewers: order matters — 1.2 without 1.3 raises on false
collisions). Effort: 0.2d.

**1.2a `cmd_run` within-run claim-id dedupe (new — Kimi: the actual
source of the divergence).** The run gate lacks the `seen` set
`_cmd_claim_batch` has: two same-content claims in one run → two
journal events → second silently dropped on replay. Track
`seen_claim_ids` across `accepted_claims`; reject duplicates (not
journaled). Effort: 0.2d.

**1.2 claim-ID collision raise — audited + hatch.** Pre-flight: scan
case `ai`'s journal for duplicate `claim_id`s with differing canonical
payloads. Identical payload → `INSERT OR IGNORE` stays (idempotent);
differing → raise with seqs, PLUS emergency `rebuild
--ignore-claim-id-conflict` so a live case is never bricked (Grok) or
a hardcoded flag-day seq carve-out (Kimi — pick at implementation;
hatch is simpler). Effort: 0.5–1d.

**1.4 Confidence default at filing — CLI AND transform gate.** In
`_prepare_claim` and `cmd_run`'s gate: omitted confidence +
evidence∈{direct,inferred} → write 0.7. Hypothesis stays explicit.
EXTENDED (Kimi): transform-side hole — an `llm` claim omitting
confidence fuses at 1.0, sailing over the documented 0.80 cap; enforce
the cap on the *effective* confidence, default included. Effort: 0.3d.

**1.5 `fundamentals` direct-mint closure.** `direct` =
analyst-hand-filed only; policy tightened to `inferred` max. Effort: 0.1d.

**1.8 `cmd_run` alias-aware validation + tritemporal passthrough
(moved from 6.3 — Grok+Gemini: must precede 1.7 merges).** Validate
endpoints via `_known_entity_ids` (entities ∪ canonical ∪ absorbed
aliases); allow `valid_from/pub_ts/time_source` through the transform
gate with `_valid_iso_ts` checks (also makes 1.3's fields writable
from runs). Without this, the 1.7 merges make the next scheduled run
reject absorbed ids. Effort: 0.4d.

**1.6 Corruption supersede pass (case `ai`).** Grep claim objs AND
quotes for `^/(bin|usr|sbin)/` and `$[0-9]` remnants. Supersede with
`kind=corrects` citing the SAME artifact; corrections must pass
`verify_quote_span` against that artifact. Reality check (Grok): if
the `$` was stripped at extract time in a *quote*, Phase 1 can't fix
it — leave those for 4.1/4.3 and note them. Effort: 0.5–1d.

**1.7 Entity twin merges + registration guard + `dupes`.** (a) merge
known pairs (`glm-5.2`/`glm-5-2`, `model:` twins, gemini twins) with
quote citations; (b) fuzzy-dup warning inside `cmd_entity`
(registration is the unguarded hole); (c) `gi2.py dupes` diagnostic.
Re-run `dupes` AFTER merges for stragglers (runbook note). Effort: 0.5d.

## Phase 2 — I/O correctness & speed (2.3 DEFERRED — unanimous) — ✅ COMPLETED 2026-08-16

> All three items landed (2.1, 2.2, 2.4, 2.5). Tail-seek passed a
> seven-case torture suite (normal, no-trailing-newline, 2MB-line,
> torn tail, empty, blank, window-edge) — first run caught two real
> bugs in the draft (trailing-newline handling, cap-exceeded loop),
> fixed in iteration. Regression gate green: 246 events, chain head
> byte-identical to Phase 1 close, 120 quotes verified. `artifacts`
> table live: 60 rows, 28.7MB measured, sizes from CAS at rebuild
> time; Content-Type captured at fetch (NULL for historical — correct,
> fetch-time-only). `cmd_run` single-transaction (2.5): entities +
> transform_run + claims in ONE final batch; live-run test blocked by
> Wikipedia 403ing the sandbox fetcher (bot-block, network-side — no
> journal pollution, lock clean, 2.5 path structurally verified; the
> scheduled Saturday capture will exercise it live).

**2.1 `last_event` tail-seek.** Bounded backward read that
LOOP-EXPANDS (double window, up to 1MB cap) until a complete final
line is found — quotes can exceed 4KB (Kimi). Replicate current
torn-tail semantics: `GiError` on invalid final-line JSON, `None` on
empty file. Effort: 0.1d.

**2.2 Shared read locks — on the LOCKFILE.** Readers
(`last_event`, `verify_chain`, `_projection_is_current`) take
`fcntl.LOCK_SH` on the SAME `.gi2.lock` file writers hold `LOCK_EX`
(Kimi: locking the journal itself excludes nothing). Lock order:
lockfile → journal. Implement 2.1 under 2.2 (Grok). Effort: 0.2d.

**2.5 `cmd_run` single transaction — REWRITTEN (Grok).** Do NOT fold
the artifact into the success-only batch: failed transforms must still
leave a journaled `artifact` (scheduled captures depend on it). Keep
the artifact `transact` BEFORE spawn (EVENTS.md order artifact →
transform_run → claims); batch entities + transform_run + claims after
the gate. 3 rebuilds → 2 (→1 if 2.3 ever lands). Effort: 0.2d.

**2.4 `artifacts` table + Content-Type + missing indexes.**
`artifacts(hash PK, uri, size, content_type, derived_from, extractor,
seq)` populated in `apply_event`; capture Content-Type at fetch
(headers currently dropped — also breaks PDF detection). Add
`idx_id_canon_lookup(entity_id, canon_id)` and
`idx_supersedes_target(target_id)` (Gemini: full scans today). Hard
dependency of Phase 4. Effort: 0.4d.

**2.3 Incremental projection — DEFERRED (3/3 consensus).** Premature
at 241 events (rebuild <15ms; ~100ms at 2,500). Defer past Phase 6,
gate on measured rebuild-time threshold (>500ms sustained). If ever
built, the council's requirements are binding:
- SCHEMA refactor first: create `claims_filed` directly, `apply_event`
  writes the table, view installed once — today's rename leaves
  `apply_event` writing to a non-updatable VIEW (Grok's latent bug).
- Equality gate is LOGICAL (SELECT every table/view vs full rebuild),
  never file bytes.
- Fuzz corpus must include merge/unmerge (AliasMap recompute),
  retract, supersede, retract-run, unknown ops, watermark > length,
  schema mismatch, crash mid-transaction; watermark chain-auth check
  (event at watermark hashes to meta.journal_hash; new events'
  prev_hash link forward — else the write path loses tamper detection
  full_rebuild currently provides).
- Real-journal fixture: case `ai` replayed both paths.
- One SQLite transaction + WAL; discard and full_rebuild on any error.
Effort: 4–5d when built. Effort now: 0.

## Phase 3 — Filing safety (0.4d)

**3.1 `claim --file` as `--batch` extension.** Accept `FILE` or `-`,
one JSON object or NDJSON — sugar over `_cmd_claim_batch` via
`_prepare_claim`. NOT a third pipeline (Grok/Kimi). Keep inline
positionals (resolved dispute from round 1 stands). Effort: 0.2d.

**3.2 Shell-scar linter — scoped (Kimi).** Refuse scars in
`subj`/`pred`/`obj` ONLY. Quotes are byte-attested by the gate and
cannot be scars; refusing them would block legitimate `/bin/sh`-path
quotes. Effort: 0.1d.

**3.3 Transform payload cap (unchanged).** 8MB companion/extracted
text into the subprocess; path for larger. Effort: 0.2d.

## Phase 4 — The HTML tax ends (~2–2.5d)

**4.1 `html-visible-v1` extractor.** As v1 spec (skip
script/style/noscript/template/svg; newline on block set; JSON-LD
appendix section). Add version-stamped golden tests. Effort: 0.75–1d.
**✅ DONE 2026-08-16** — `gi2_extract.py` + `test_gi2_extract.py` (20
tests, determinism by double-hash); worst-case artifact shrink 2.6%.

**4.2 Companion ingestion — uri UNTOUCHED (Grok override of v1).**
Keep `uri` as the source URL / `file://` (the frozen shape); recipe
identity rides ONLY in extra keys: `derived_from`, `extractor`,
`extractor_code_hash`. No `gi2:` scheme URIs in the treaty field.
≥8-word-run sanity check against tag-stripped raw. Effort: 0.5–1d.
**✅ DONE 2026-08-16** — `maybe_companion` + `store_blob_bytes`; live
test seq 253/254 (openrouter.ai/models); idempotent by content hash.

**4.3 Companion-aware filing (unchanged).** Ambiguous-in-raw +
unique-in-companion → print companion hash, REFUSE to guess;
`--prefer-extract` opts in. Verify against cited hash only. Effort: 0.4d.
**✅ DONE 2026-08-16** — refusal-with-hint proven live; `--prefer-extract`
flag; companion citation via seq 255 (retracted as probe).

**4.4 Route companions into `cmd_run` (new — Kimi: Phase 4's biggest
gap in v1).** When a run's artifact is HTML: extract, journal the
companion `artifact` BEFORE `transform_run` (frozen order preserved),
pass `companion_hash`/`companion_text` in the transform payload, and
extend the gate to accept citations against the companion hash for
that run. Without this the LLM reader — the largest text consumer —
still eats raw HTML. Effort: 0.5–1d.
**✅ DONE 2026-08-16** — live wiki run: 301KB HTML → 26KB companion
(11.4× shrink), payload carries prose, `companion_hash` journaled in
`transform_run`; `--raw-text` opt-out.

## Phase 5 — Reorientation (~1d) — COMPLETE 2026-08-16

**5.3 `lint` BEFORE 5.1 (Kimi+Grok majority).** Drift, scars, missing
confidence, hypothesis-over-cap, dupes. Standalone shippable value.
Effort: 0.4d.

**5.1 `brief`.** As v1 spec (2KB cap, 7 sections, drop order). Section
7 calls `collect_lint()`; if empty, omit (no hard dep). Effort: 0.5–0.7d
(Gemini: v1 overestimated).

**5.2 FTS5 `find` — DEFERRED (2/3).** SQL `LIKE` over `claims_filed`
is sufficient at ≤1,300 claims and avoids FTS5 module-availability
risk across builds (Gemini). When built: populate in `full_rebuild`
only; DELETE from `claims_fts` on retract (ghost risk); guard builds
lacking FTS5. Effort: 1d, later.

**5.4 `export --ego` — CUT (2/3).** `dig` already emits LLM-facing
synthesis; redundant this quarter.

## Phase 6 — Verification & leftovers — ✅ COMPLETED 2026-08-16

**Remediation round (council review-3, same day):** all findings landed
— the P0 via_run brick (identity-tuple fix + gate-side refusal; proven
idempotent with double wiki runs), 4.4's companion gate (verifies in
companion coordinates, journals companion_hash), 1.4's batch-path
default, 6.1's read-once memoization (art_buffers), 2.4's schema bump
(v3 + artifacts-table structural check), 2.2's locks (depth-counter
reentrancy, stat-inside-lock, verify_chain shared lock), lint's shared
normalizer + absorbed-entity exclusion, byte-safe payload cap, and the
SKILL.md fundamentals correction. Case `ai` verified after every fix:
271 events, 121 quotes, 0 defects. Remaining known debt: 18
missing-confidence analyst claims (pre-1.4) — a backfill batch, not a
code fix.

**6.1 Verify tiers + artifact memoization.** Default UNCHANGED (full
re-hash + all quotes). `--quick` opt-in with sidecar checkpoint
`.verify-checkpoint.json` (size+mtime_ns; document bitrot blind spot).
Add: group claim events by cited artifact, read each once (today:
per-claim re-hash — dozens of reads per artifact at 10×). Effort: 1d.

**6.2 DROPPED as specified (Grok: cannot work).** `apply_event`
retract does `DELETE FROM claims`; there is no `claims_filed` history
to join — that's why `why` scans the journal. Replacement if wanted
later: soft-delete retract (row stays, VIEW hides, `retractions` is
the history) — belongs next to 1.2's raise/reactivate semantics, not
here. Until then the journal scan stays.

**6.4 Belief aggregation — deferred** (cache source scores once per
command; GROUP BY edge). Only when full `show` measurably slows.

---

## Ship order (council fast lane, ~7–8 days to the value line)
1. Regression gate (case-ai replay) → 1.0+1.1 → 1.3 → 1.2a → duplicate
   audit → 1.2 → 1.4 → 1.5 → 1.8 → 3.2
2. 2.1 (under 2.2's lock) → 2.2 → 2.5-rewritten → 2.4
3. **Case-ai surgery on the repaired kernel:** 1.6 → 1.7 (+ dupes
   re-run), regression gate again
4. 3.1 → 5.3 → 5.1 (weekly workflow unblocked)
5. 4.1 → 4.2 → 4.3 → 4.4 (HTML tax ends, transforms included)
6. Later, gated on measurement: 6.1, 5.2, 2.3, 6.4

## Explicitly rejected (unchanged + additions)
- Retiring inline claim args; readability extractors; incremental
  chain verification; journal v2/new ops; journal compaction; growing
  `dig` into brief
- NEW: weakening default `verify` (6.1 quick becomes default)
- NEW: `gi2:` scheme in the frozen `uri` field
- NEW: 2.5 folding the artifact event into the success-only batch
- NEW: `why` via retractions table while retract hard-deletes rows

## Effort (revised, council-realistic)
Value line (steps 1–5): **~7–8 days**. Phase 6 + deferred items:
+3–4 days whenever measurement demands them. (v1's 9–11d assumed 2.3
in the critical path; deferring it is the saving.)

---

## BUILD COMPLETE — 2026-08-16

All six phases landed, council-reviewed (3 rounds), and remediated.
Case `ai` final state: 271 events, 40 entities, 128 active claims,
66 artifacts, 121 verified quotes, chain head `ac07a90e…`, 0 defects.
Deferred items (2.3, 5.2, 6.4) remain gated on measurement, as ordered.
