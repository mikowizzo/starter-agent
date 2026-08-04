---
name: ask-crew
description: >
  Fire one query at multiple OpenCode models in parallel (MiniMax M3, Kimi K3,
  Grok 4.5, GLM 5.2) and print each answer. Use to compare how different
  models answer the same question. Reads OPENCODE_API_KEY from the environment.
license: MIT
---

# Ask Crew

Asks the "model crew" — four OpenCode models answer the same query in
parallel and their answers are printed side by side.

## Usage

    python backend/app/skills/ask-crew/ask_crew.py "What's the best way to..."
    python backend/app/skills/ask-crew/ask_crew.py --models kimi-k3,glm-5.2 "..."

## Notes

- **User-Agent**: the script sends a browser UA because Cloudflare (error
  1010) blocks Python-urllib's default `Python-urllib/x.y` UA with a 403.
  Don't "simplify" that away.
- One model failing (e.g. transient router error) doesn't stop the others;
  its error prints inline.
- To change the crew, edit `CREW` at the top of `ask_crew.py`.
