---
name: ask-crew
description: >
  Fire one query at multiple OpenCode MODELS in parallel (MiniMax M3, Kimi K3,
  Qwen 3.8 Max, GLM 5.2) and print each answer. Use to compare how different
  MODELS answer the same question. IMPORTANT: this queries AI MODELS via the
  OpenCode API — NOT the clone crew members (franky, nami, etc.). Do NOT use
  the talk_to tool for this. Reads OPENCODE_API_KEY from the environment.
license: MIT
---

# Ask Crew

Asks the "model crew" — four OpenCode MODELS (MiniMax M3, Kimi K3, Qwen 3.8 Max,
GLM 5.2) answer the same query in parallel and their answers are printed side
by side.

> ⚠️ This is about MODELS, not crew-member clones. "Ask the crew" =
> this script. To message a Straw Hat clone (franky, nami, ...) use the
> `talk_to` tool instead.
## Usage

    python backend/app/skills/ask-crew/ask_crew.py "What's the best way to..."
    python backend/app/skills/ask-crew/ask_crew.py --models kimi-k3,glm-5.2 "..."
    python backend/app/skills/ask-crew/ask_crew.py --file path/to/code.py "review this"
    python backend/app/skills/ask-crew/ask_crew.py --file a.py --file b.py "compare these"
    python backend/app/skills/ask-crew/ask_crew.py --file README.md   # defaults to "please review"

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
- To change the crew, edit `CREW` at the top of `ask_crew.py`.
