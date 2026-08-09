---
name: gi
description: >
  GI — Graph Investigator, journal edition (Kimi K3's redesign): an
  append-only NDJSON claim journal with a SQLite materialized view and a
  content-addressed evidence store. Contradiction is data, undo is an event,
  time travel is a query flag. One gi binary: new, entity, claim, retract,
  merge, unmerge, resolve, review, query, neighbors, expand, why, mark,
  session, search, run, fetch, ingest, log, show, export, check, vocab.
  Reads GI_CASE. Use for investigations whose provenance must survive an
  audit — claims are verified against verbatim quotes at ingest, and hashes
  are recomputed, so a transform cannot claim it fetched what it did not.
  Iterative traversal (neighbors/expand/why) lets you pivot from node to
  node like Maltego — ranked by belief, annotated with degree and visited
  state, with breadcrumb session tracking.
license: MIT
---

# GI — Graph Investigator, journal edition

The v1 skill (graph-investigator) is a mutable JSON document that you patch.
This is the rebuild from a design session with Kimi K3: an **append-only
claim journal that you fold**. The question shifts from *"what is the current
state of the case?"* to *"what has been claimed, by whom, on what evidence —
and what do we currently believe given all of it?"*

The through-line: **discipline that is automatic is discipline that survives
contact with a long investigation.**

## The case file

```
case/
  journal.ndjson   # THE TRUTH — append-only, never rewritten
  case.db          # SQLite materialized view — disposable cache, rebuilt on mismatch
  evidence/        # content-addressed artifacts: sha256/<h[:2]>/<h[2:]>
  session.json     # traversal state — visited set, marks, breadcrumb trail (deletable)
  gi.toml          # case-local config
```

One JSON event per journal line. The DB is rebuilt by replay whenever its
recorded journal length differs — corruption of the cache is a non-event.

Key properties:

- **Contradiction is data.** A dispute is a `refutes` claim sitting beside a
  `supports` claim. The edge's current belief is *computed* — the lifecycle
  can never drift out of sync with the evidence.
- **Undo is a first-class operation.** `retract` and `unmerge` are events;
  merging rewires nothing destructively.
- **Temporal queries are trivial.** `valid_from/valid_to` (when the fact was
  true) and `ts` (ingestion time) are separate axes, both in the log.
- **Replay = audit.** `gi log --since seq:40` answers what a run added.

## Quickstart

```bash
export GI_CASE=~/cases/stella-chemifa
gi new ~/cases/stella-chemifa
gi entity "Minotaur Capital Pty Ltd" --kind organization --ext-id abn=24639996393
gi fetch https://abr.example/...            # -> sha256:...
gi claim abn:24639996393 employs alice-rivera \
    --evidence direct --confidence 0.9 \
    --cite sha256:ab12cd...:441:512 --quote "Minotaur Capital employs Alice Rivera"
gi claim alice-rivera knows eve-noir --evidence hypothesis --basis "seen together at ASIC filings"
gi merge minotaur-capital abn:24639996393 --reason "same legal entity"
# Iterative traversal — the Maltego-style pivot
gi neighbors alice-rivera                  # 1-hop, ranked by belief
gi expand alice-rivera --depth 2           # multi-hop BFS
gi why alice-rivera minotaur-capital       # evidence behind an edge
gi mark holding-co-a suspicious            # annotate
gi session                                 # breadcrumb trail
# Global analysis
gi query components
gi query path --from eve-noir --to abn:24639996393
gi query hubs
gi export dot | dot -Tpng > case.png
gi check
```

## CLI

All subcommands take `--case PATH` (before the subcommand) or the `GI_CASE`
env var; default is `./case`.

### Core commands

| Command | Purpose |
|---|---|
| `new CASE` | create a case directory (journal header, evidence/, gi.toml) |
| `entity NAME [--id] [--kind] [--ext-id K=V]... [--alias A]...` | record an entity |
| `claim SUBJ PRED OBJ [--evidence] [--confidence] [--polarity supports\|refutes] [--cite SHA:START:END --quote ... \| --basis ...] [--valid-from] [--valid-to]` | record a claim — must cite a verified quote **or** be a hypothesis with a basis |
| `retract CLAIM_ID --reason TEXT` | append a retraction event |
| `merge SRC INTO [--reason]` | append a merge event (reversible) |
| `unmerge SRC [--reason]` | append an unmerge event undoing the merge |
| `resolve [--auto] [--review] [--threshold F]` | registry keys -> auto candidates; fuzzy -> review queue |
| `review [--apply N \| --reject N]` | the human merge queue |
| `query components\|hubs\|bridges\|path [--from X --to Y] [--as-of D] [--min-belief F] [--include-disputed]` | interrogate the network |
| `run TRANSFORM --entity ID` | subprocess a transform; verified ingest of its NDJSON |
| `fetch URL` | download into the evidence store; prints sha256 |
| `ingest [FILE\|-]` | ingest NDJSON from any pipeline (verified) |
| `log [--since SEQ] [--actor A]` | the audit trail |
| `show [ID]` | entity, aliases, claims, belief, citations |
| `export dot\|json\|csv [--as-of D]` | DOT canvas / JSON / CSV court record (CSV injection-guarded) |
| `check` | structural lint: vocab drift, missing artifacts, disputed-too-long |
| `vocab` | print the relation vocabulary (data, not code: `scripts/vocab.toml`) |

### Traversal commands

| Command | Purpose |
|---|---|
| `neighbors ENTITY [--pred P]... [--max-degree N] [--limit N] [--min-belief F] [--as-of D]` | 1-hop neighbors ranked by belief — the core pivot primitive |
| `expand ENTITY [--depth N] [--budget N] [--pred P]... [--max-degree N]` | depth-limited BFS — the multi-hop view |
| `why A B` | evidence behind a specific edge — every claim, citation, and basis |
| `mark ENTITY LABEL` | annotate an entity (interesting/cleared/suspicious) |
| `session [--reset]` | show or clear the breadcrumb trail (visited set, marks, pivot history) |
| `search QUERY` | find entities by name, kind, or external id |

**Entity resolution** for traversal commands accepts: entity id, display
name, alias, or registry id (`abn:24639996393`). All are resolved to the
canonical id automatically.

**Session state** persists to `session.json` in the case directory. It
tracks visited entities, marks, the seed entity, current focus, and the full
pivot trail with timestamps. It is working state — deletable, not journaled,
not evidence.

## Iterative traversal: the investigation loop

The professional investigator's workflow is not "dump the whole graph" —
it's pivot from node to node, following the strongest leads:

```
gi search "minotaur"           → find the entity
gi neighbors minotaur-capital   → see who's connected, ranked by belief
gi why minotaur-capital bob-chen → drill into the evidence for a specific edge
gi neighbors bob-chen           → pivot to a neighbor, [visited] shows where you've been
gi mark bob-chen interesting    → annotate for later
gi expand bob-chen --depth 2    → see the 2-hop neighborhood
gi session                      → review your breadcrumb trail
```

**Design principles (from the crew design session):**

- **Default depth is 1.** Resist making deep expansion easy — deep expansion
  without triage is just the global view with extra steps.
- **Default budget is 50.** The moment an expansion returns more than ~30
  unranked nodes, you've rebuilt the global view one hop at a time and lost
  the entire point.
- **Rank by belief.** The noisy-or belief score sorts neighbors so the
  strongest leads are at the top.
- **Show edge types.** "Sent money to" is an investigation; "connected to"
  is noise.
- **Track visited state.** Re-scanning visited nodes is the #1 time sink in
  manual pivoting. Visited nodes are tagged; marked nodes show their mark.
- **`why` is the audit hook.** Every edge can be drilled into — claims,
  citations, verbatim quotes, or hypothesis basis. This turns traversal into
  defensible analysis.
- **`--pred` filter** avoids the hairball: only follow edges of a specific
  type (e.g. `--pred subsidiary_of --pred parent_of` for corporate structure).
- **`--max-degree`** hides infrastructure-like hubs (a shared DNS server
  with 10,000 connections is poison).

## Belief: the evidence semilattice

Evidence strength is a total order (`direct 1.0 > inferred 0.65 > hypothesis
0.25`, weights in `gi.toml`); belief combines claims per polarity with
noisy-or, so **independent corroboration compounds**:

```python
def noisy_or(ps):
    prod = 1.0
    for p in ps: prod *= 1.0 - p
    return 1.0 - prod
```

Three independent 0.6 sources yield ≈ 0.94 — the signal v1's "max wins"
upsert threw away. Counter-evidence is an opposing claim, not a field, so
`disputed` is computed and can never be inconsistent with the data. Nothing
downgrades silently; retraction is an event by an actor with a reason.

## Transform contract

Transforms are standalone executables (any language) with a strict pipe
contract. They **never touch the case file**:

```
stdin:  {"entity": {"id": ..., "name": ...}, "evidence_dir": "...", "config": {...}}
stdout: {"type": "artifact", "uri": ..., "fetched_at": ..., "file": <tmp>, "hash": "sha256:.."}
        {"type": "entity", "id": ..., "name": ..., "kind": ..., "external_ids": {...}}
        {"type": "claim", "subj": ..., "pred": ..., "obj": ..., "evidence": "inferred",
         "confidence": 0.6, "cites": [{"artifact": "sha256:..", "span": [s, e], "quote": "..."}]}
```

The ingest pipeline — not the transform — enforces three invariants:

1. **The hash is recomputed.** Bytes are bytes, stored immutably, addressed
   by content. A transform cannot claim it fetched what it did not.
2. **Every non-hypothesis claim must cite a verbatim quote** that exists near
   its claimed span (±64 chars slack; the quote is the real check). Spans are
   in whitespace-normalized text — `normalize_ws` is part of the contract.
3. **Uncited claims are legal only as hypotheses** with a free-text `basis`.

`gi run transform-wiki --entity "Canon Inc."` demonstrates the contract:
artifact + entity + pattern claims (`headquartered_in`, `subsidiary_of`,
`founded_by`), evidence `inferred` with confidence 0.6 — wiki prose is a
first-pass map, never a conclusion.

## Identity and resolution

- Identity is a union-find fold over merge/unmerge events. **Registry-keyed
  ids win** (`abn:`, `acn:`, `arsn:`, `afsl:`, `lei:`, `houjin:`, `asx:`).
- `resolve` blocks by registry keys, name fingerprints (JP-suffix aware:
  株式会社/KK/Co., Ltd. stripped), and shared tokens. **Registry-key collisions
  auto-merge** (`--auto`) — an ABN either matches or it doesn't; that is not
  a judgment call. Fuzzy matches go to the `review` queue with a score:
  `6·jaro_winkler(name) + 3·jaccard(aliases) + 1.5·kind + 4·jaccard(neighbors)`.
- `--as-of` and `--min-belief` filter every analysis; time travel is one
  keystroke, so `valid_from/valid_to` metadata stays honest.

## Divergences from graph-investigator v1

| # | v1 | gi |
|---|---|---|
| 1 | Mutable JSON + status flags | Append-only journal; contradiction computed |
| 2 | One row per edge, strongest wins | Claims as citizens, noisy-or corroboration |
| 3 | Provenance *tags* ("testimony") | Content-addressed evidence + verified quote spans |
| 4 | All merges through the analyst | Registry keys auto-merge; only fuzzy -> human |
| 5 | Five scripts, vocab in docstring | One binary, `GI_CASE`, vocab in `vocab.toml` |
| 6 | No traversal — absorb full graph | Iterative pivot: neighbors/expand/why + session |

## Notes

- **User-Agent**: the transform sends a descriptive browser-ish UA; Cloudflare
  blocks `Python-urllib` defaults. Don't simplify that away.
- One model/transform failing never aborts the crew: `gi run` ingests partial
  stdout and warns.
- The journal is the only state that matters; `case.db`, `session.json`,
  DOT, JSON, CSV are derived and regenerable. Fetched artifacts (`evidence/`)
  are evidence — keep the whole case directory.
- Add transforms by following the contract; site-specific one-offs are
  `gi fetch URL | gi claim --interactive`-style, routed through the same
  evidence store and span verification.
