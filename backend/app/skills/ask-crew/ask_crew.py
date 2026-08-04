#!/usr/bin/env python3
"""Ask the model crew — fire one query at multiple OpenCode models in parallel.

Usage:
  python ask_crew.py "your question here"
  python ask_crew.py --models kimi-k3,glm-5.2 "your question"

Reads OPENCODE_API_KEY from the environment (same key the app uses).
"""
import concurrent.futures
import json
import os
import sys
import time
import urllib.request

BASE_URL = "https://opencode.ai/zen/go/v1/chat/completions"
KEY_ENV = "OPENCODE_API_KEY"

# The model crew: (OpenCode model id, display label).
CREW = [
    ("minimax-m3", "MiniMax M3"),
    ("kimi-k3", "Kimi K3"),
    ("grok-4.5", "Grok 4.5"),
    ("glm-5.2", "GLM 5.2"),
]

TIMEOUT = 120
# Cloudflare 403s Python-urllib's default UA (error 1010) — browser UA required.
HEADERS = {
    "Authorization": "Bearer {key}",
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
}


def ask(model_id: str, query: str, key: str) -> tuple[str, str, float]:
    """Ask one model, return (model_id, content_or_error, elapsed_seconds)."""
    body = json.dumps(
        {"model": model_id, "messages": [{"role": "user", "content": query}]}
    ).encode()
    req = urllib.request.Request(
        BASE_URL,
        data=body,
        method="POST",
        headers={k: v.format(key=key) for k, v in HEADERS.items()},
    )
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            data = json.loads(r.read())
        content = (
            (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        )
        return model_id, content.strip() or "(empty reply)", time.monotonic() - t0
    except Exception as e:
        return model_id, f"ERROR: {type(e).__name__}: {e}", time.monotonic() - t0


def main() -> int:
    args = sys.argv[1:]
    models = [m for m, _ in CREW]
    if args and args[0] == "--models":
        if len(args) < 2:
            print("ERROR: --models needs a comma-separated list", file=sys.stderr)
            return 2
        models = [m.strip() for m in args[1].split(",") if m.strip()]
        args = args[2:]
    query = " ".join(args).strip()
    if not query:
        print(__doc__)
        return 2

    key = os.environ.get(KEY_ENV, "")
    if not key:
        print(f"ERROR: {KEY_ENV} not set", file=sys.stderr)
        return 1

    print(f"🤖 Asking the crew ({len(models)} models): {query}\n")
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(models)) as pool:
        futures = [pool.submit(ask, mid, query, key) for mid in models]
        results = [f.result() for f in futures]

    order = {m: i for i, m in enumerate(models)}
    for mid, content, dt in sorted(results, key=lambda r: order[r[0]]):
        label = next((l for m, l in CREW if m == mid), mid)
        print(f"── {label} ({mid}) — {dt:.1f}s ──")
        print(content)
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
