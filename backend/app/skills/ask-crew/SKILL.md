---
name: ask-crew
description: >
  Fire one query at multiple MODELS in parallel (Kimi K3 via Synthetic,
  DeepSeek V4 Pro 0813, Grok 4.6, Gemini 3.7 Flash) and print each answer.
  Use to compare how different MODELS answer the same question.
  IMPORTANT: this queries AI MODELS via the Synthetic/OpenRouter APIs
  — NOT the clone crew members (franky, nami, etc.). Do NOT use the
  talk_to tool for this. Reads SYNTHETIC_API_KEY for Kimi K3,
  OPENROUTER_API_KEY for DeepSeek V4 Pro 0813, Grok 4.6, and Gemini 3.7 Flash.
license: MIT
---

# Ask Crew
Asks the "model crew" — four MODELS (Kimi K3 via Synthetic,
DeepSeek V4 Pro 0813, Grok 4.6, Gemini 3.7 Flash) answer the same query in
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
    python backend/app/skills/ask-crew/scripts/ask_crew.py --models x-ai/grok-4.6,deepseek/deepseek-v4-pro-0813 "..."
    python backend/app/skills/ask-crew/scripts/ask_crew.py --file path/to/code.py "review this"
    python backend/app/skills/ask-crew/scripts/ask_crew.py --file a.py --file b.py "compare these"
    python backend/app/skills/ask-crew/scripts/ask_crew.py --file README.md   # defaults to "please review"

## Models

The `--models` flag takes **exact ids only** — there are no aliases. An
unknown id is rejected with the list below (exit code 2), so a misspelling
can never silently route to the wrong API.

| Model | `--models` id | Route |
|---|---|---|
| Kimi K3 | `hf:moonshotai/Kimi-K3` | Synthetic (`SYNTHETIC_API_KEY`) |
| DeepSeek V4 Pro 0813 | `deepseek/deepseek-v4-pro-0813` | OpenRouter (`OPENROUTER_API_KEY`) |
| Grok 4.6 | `x-ai/grok-4.6` | OpenRouter (`OPENROUTER_API_KEY`) |
| Gemini 3.7 Flash | `google/gemini-3.7-flash` | OpenRouter (`OPENROUTER_API_KEY`) |

Default (no `--models`): all four.

## Files

- `--file PATH` may be passed multiple times. Each file's contents are
  inlined into the prompt sent to every model, wrapped in
  `--- file: PATH (N bytes) ---` ... `--- end PATH ---` markers, with the
  user's question appended under `--- question ---`.
- Text files are inlined as-is. Files larger than `MAX_FILE_BYTES`
  (100 KB) are truncated and a `[truncated: ...]` marker is appended.
- Binary (non-UTF-8) files are noted (`N bytes; binary, not inlined`) so
  the model still sees the file was provided.
- Missing / unreadable paths print a warning to stderr and are skipped —
  one bad path never aborts the run. If all files are missing and there's
  no query, the script exits 2.
- If a query is omitted but at least one file was inlined, the prompt
  defaults to "Please review the file(s) above."

## Notes

- **User-Agent**: the script sends a browser UA because Cloudflare (error
  1010) blocks Python-urllib's default `Python-urllib/x.y` UA with a 403.
  Don't "simplify" that away.
- One model failing (e.g. transient router error) doesn't stop the others;
  its error prints inline.
- To change the crew, edit `CREW` at the top of `scripts/ask_crew.py`.
