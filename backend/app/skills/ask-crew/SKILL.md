---
name: ask-crew
description: >
  Fire one query at multiple MODELS in parallel (Kimi K3 via Synthetic,
  Grok 4.6, Gemini 3.7 Flash) and print each answer.
  Use to compare how different MODELS answer the same question.
  IMPORTANT: this queries AI MODELS via the Synthetic/OpenRouter APIs
  — NOT the clone crew members (franky, nami, etc.). Do NOT use the
  talk_to tool for this. Reads SYNTHETIC_API_KEY for Kimi K3,
  OPENROUTER_API_KEY for Grok 4.6 and Gemini 3.7 Flash.
license: MIT
---

# Ask Crew
Asks the "model crew" — three MODELS (Kimi K3 via Synthetic,
Grok 4.6, Gemini 3.7 Flash) answer the same query in
parallel and their answers are printed side by side. Each has a bespoke
route: Kimi K3 (Synthetic), the rest via OpenRouter.

> ⚠️ This is about MODELS, not crew-member clones. "Ask the crew" =
> this script. To message a Straw Hat clone (franky, nami, ...) use the
> `talk_to` tool instead.

## Usage

Preferred — invoke through the skill access tools (never shell):

    get_skill_script(skill_name="ask-crew", script_path="ask_crew.py", execute=True, args=["your question"])

Legacy shell usage (kept for reference, not recommended):

    python backend/app/skills/ask-crew/scripts/ask_crew.py "What's the best way to..."
    python backend/app/skills/ask-crew/scripts/ask_crew.py --models x-ai/grok-4.6,google/gemini-3.7-flash "..."
    python backend/app/skills/ask-crew/scripts/ask_crew.py --file path/to/code.py "review this"
    python backend/app/skills/ask-crew/scripts/ask_crew.py --file a.py --file b.py "compare these"
    python backend/app/skills/ask-crew/scripts/ask_crew.py --file README.md   # defaults to "please review"

**Default = the lean path:** N models, N calls, answers printed. Everything
that costs extra calls is opt-in:

| Flag | What it adds | Cost |
|---|---|---|
| `--heretic` | designated devil's-advocate pass (Kimi K3) after the crew answers | +1 full call |
| `--judge` | blind arena: pairs judged, Elo ratings updated, leaderboard shown | +1 call per pair (3 models = 3 pairs) |
| `--judge-swap` | `--judge` + double-judge each pair to catch position bias | ×2 judge calls |
| `--no-claims` | skip claim cartography (on by default, zero extra calls) | −0 calls |

**Built-in, always on, zero extra calls:** automatic retries on transient
failures (429/5xx/timeout — 2s→4s backoff, only before the first token
arrives), count-based quorum (returns once 2 council answers are in;
stragglers keep streaming to disk, never lost), and capability routing
for oversized packs (small-context models get structural outlines while
big-context models review the full files — see Files section).

## Reliability

- **Retries**: a transient failure (HTTP 429/408/425/5xx, connection or
  timeout error) is retried up to 3 attempts with 2s→4s backoff — but
  only if no tokens have streamed yet. A mid-stream failure is never
  retried (a retry would duplicate the checkpointed stream); what
  arrived is kept and the error is shown.
- **Quorum**: the tool returns as soon as `QUORUM_COUNT` (2) good
  council answers are in, instead of waiting for every model. An
  errored model never counts toward quorum. Stragglers keep streaming
  to the ledger — their checkpoints are not lost. When `--heretic` is
  on, quorum never abandons the heretic mid-stream.

## Models

The `--models` flag takes **exact ids only** — there are no aliases. An
unknown id is rejected with the list below (exit code 2), so a misspelling
can never silently route to the wrong API.

| Model | `--models` id | Route |
|---|---|---|
| Kimi K3 | `hf:moonshotai/Kimi-K3` | Synthetic (`SYNTHETIC_API_KEY`) |
| Grok 4.6 | `x-ai/grok-4.6` | OpenRouter (`OPENROUTER_API_KEY`) |
| Gemini 3.7 Flash | `google/gemini-3.7-flash` | OpenRouter (`OPENROUTER_API_KEY`) |

Default (no `--models`): all three.

## Files — fail closed, never silently truncated

- `--file PATH` may be passed multiple times. Each file's contents are
  inlined into the prompt sent to every model, wrapped in
  `--- file: PATH (N bytes) ---` ... `--- end PATH ---` markers, with the
  user's question appended under `--- question ---`.
- **Whole files only.** There is no per-file cap. The budget is per-RUN:
  all inlined files together must fit `MAX_RUN_BYTES` (300 KB ≈ 75k
  tokens, the estimate shown at prompt-build time). A single 250 KB file
  passes fine on its own.
- **Over budget → the run refuses** (exit 2) with a per-file size report
  and your options. It never sends a silent head-only review — the model
  would confidently review code it never saw.
  - `--max-bytes N` — raise the budget; whole files go through (more cost).
  - `--force-truncate` — explicitly opt into head-sampling every file to
    fit the budget. Loud per-file warnings; models will NOT see full files.
- Binary (non-UTF-8) files are noted (`N bytes; binary, not inlined`) so
  the model still sees the file was provided.
- Missing / unreadable paths print a warning to stderr and are skipped —
  one bad path never aborts the run. If all files are missing and there's
  no query, the script exits 2.
- If a query is omitted but at least one file was inlined, the prompt
  defaults to "Please review the file(s) above."
- Do NOT pre-split long files into chunks yourself — that breaks
  cross-chunk invariants and introduces splitting errors. Pass the whole
  file; raise `--max-bytes` if needed.
- **Capability routing:** if the full pack exceeds a model's context
  window, that model is automatically sent deterministic structural
  outlines (every `def`/`class`/declaration line + file metadata) instead
  of full file bodies — no API calls needed to build them. Big-context
  models (e.g. Kimi K3) still get the complete files. A `⚡ routing:`
  notice is printed so you always know who saw what. This keeps every
  model useful on large reviews without silently dropping content.

## Ledger

Every run is persisted to a SQLite ledger at `<repo>/data/crew/crew_ledger.db`
(or `$CREW_LEDGER_DB`): run metadata, per-model responses, claims, and
match/Elo history. `--history [N]` shows recent runs. Disable with
`CREW_LEDGER=off`. A ledger failure never kills the ask flow.

## Notes

- **User-Agent**: the script sends a browser UA because Cloudflare (error
  1010) blocks Python-urllib's default `Python-urllib/x.y` UA with a 403.
  Don't "simplify" that away.
- One model failing (e.g. transient router error) doesn't stop the others;
  its error prints inline.
- To change the crew, edit `CREW` at the top of `scripts/ask_crew.py`.
