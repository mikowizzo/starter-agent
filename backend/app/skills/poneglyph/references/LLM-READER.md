# The LLM Reader — Design Note (Slice 4b)

Status: frozen 2026-08-14. Treat as part of the treaty alongside `EVENTS.md`.

## The tension

The Slice 4 pipe contract says transforms are jailed: the host fetches
the artifact, the transform never touches the network, so a transform
cannot cite a document it never saw. An LLM reader must call an API to
read — the jail and the reader appear to conflict.

Resolution starts from what the jail is *for*. Its purpose is not to
prevent network I/O per se; it is to prevent **fabricated sources**.
That guarantee comes from the gate, not the sandbox: every emitted
quote is re-verified verbatim against the host-stored CAS artifact
before anything is journaled. The jail is contractual; the gate is
forensic.

## Two architectures considered

- **A — Host-mediated proxy.** The transform emits `ask-model` requests
  on stdout; the host calls the API and returns responses on stdin.
  Zero egress from the transform, but new host plumbing, a stateful
  protocol, and the host becomes a chaplain to the model's whims.
- **B — Egress exception (chosen).** The transform itself calls an
  OpenAI-compatible endpoint. Zero host changes; the transform is a
  normal pipe-contract participant that happens to use a model.

**B won** because the security property survives either way: a
hallucinating model can emit fabricated quotes all day and every one
is rejected at the gate (test T2 proves it). With A we would pay real
protocol complexity for a guarantee the gate already provides.
Gemini 3.7 Flash's crew build chose B; adversarial review (in lieu of
the heretic, who did not respond that round) concurred after fixes:
real model routing for this environment, honest truncation disclosure
to the model, keyless runs fail cleanly.

## Determinism of record, not of extraction

A second run may extract different claims — model non-determinism is
acknowledged and accepted. The **record** stays deterministic: each
run's accepted claims are frozen verbatim in the append-only journal
with `via_run` provenance, and **replay never re-executes the reader**
(replay applies events, it does not re-run transforms). A bad LLM run
is retractable wholesale by `run_id`, same as any transform.

## Model routing (first match wins)

1. `OPENAI_BASE_URL` / `LLM_API_BASE` env → explicit override (tests use this)
2. model id `kimi*` → `https://api.synthetic.new/v1` (`SYNTHETIC_API_KEY`)
3. anything else → `https://opencode.ai/zen/go/v1` (`OPENCODE_API_KEY`),
   falling back to `https://openrouter.ai/api/v1` (`OPENROUTER_API_KEY`)

Default model: `deepseek-v4-flash` (OpenCode). Model ids are bare on
OpenCode (no `hf:` prefix). LLM endpoints can be slow on first token —
pass `--timeout 200` to the host if the 60s default kills the run, and
`--arg call_timeout=N` to raise the transform's own HTTP timeout
(default 180s).

No key → clean exit-1, nothing journaled.

## Usage

```
gi2.py run llm --uri URL [--arg model=deepseek-v4-flash] [--arg focus="insider sales"]
               [--arg max_chars=24000] [--arg max_claims=30]
               [--arg repair=true] [--arg call_timeout=180] [--timeout 200]
```

Cost guards: `max_chars` truncates the prompt (and the model is *told*
it is reading a prefix — never silently); `max_claims` caps extraction.

## Policy, enforced by the reader

- LLM-emitted claims are `evidence=inferred`, polarity `supports`,
  confidence **capped at 0.80** — an LLM is a good reader but a
  secondhand witness; it may corroborate, never prove.
- Entities the model proposes are registered; claims on entities the
  model never mentioned are rejected by the gate.
- One optional repair round: claims rejected for quote-span problems
  are shown to the model once to fix (exactly ≤2 API calls per run,
  test T3 counts them).

## Test coverage (`scratch/test_slice4b.py`, mock model server)

- T1 valid claims accepted, capped confidence, inferred evidence
- T2 hallucinated quote → 0 accepted, no edge in graph
- T3 repair round: bad quote fixed on round 2, exactly 2 API calls
- T4 second failure is final
- T5 cost caps: truncation + max_claims enforced
- T6 verify + rebuild after an LLM run; verdict intact
- T7 no API key → clean failure, nothing journaled
