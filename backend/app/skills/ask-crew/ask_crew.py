#!/usr/bin/env python3
"""Ask the model crew — fire one query at multiple OpenCode models in parallel.

Usage:
  python ask_crew.py "your question here"
  python ask_crew.py --models kimi-k3,glm-5.2 "your question"
  python ask_crew.py --file path/to/code.py "review this"
  python ask_crew.py --file a.py --file b.py "compare these"
  python ask_crew.py --file README.md         # default: "please review"

Pass one or more --file PATH args to inline file contents into the prompt.
Text files are inlined; binary files are noted but not inlined. Files larger
than MAX_FILE_BYTES are truncated with a warning.

Reads OPENCODE_API_KEY from the environment (same key the app uses).
"""
import concurrent.futures
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

BASE_URL = "https://opencode.ai/zen/go/v1/chat/completions"
KEY_ENV = "OPENCODE_API_KEY"

# The model crew: (OpenCode model id, display label).
CREW = [
    ("minimax-m3", "MiniMax M3"),
    ("kimi-k3", "Kimi K3"),
    ("qwen3.8-max", "Qwen 3.8 Max"),
    ("glm-5.2", "GLM 5.2"),
]

TIMEOUT = 120
# Per-file inlining cap. Large files blow the context window; truncate with a
# marker so the model still sees the structure.
MAX_FILE_BYTES = 100_000
# Cloudflare 403s Python-urllib's default UA (error 1010) — browser UA required.
HEADERS = {
    "Authorization": "Bearer {key}",
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
}


def read_file_block(path: str) -> str | None:
    """Read a file and return a prompt-ready block, or None if unusable.

    - Text files are inlined, truncated at MAX_FILE_BYTES with a marker.
    - Binary files (not valid UTF-8) return a one-line note (caller still
      includes the file in the prompt so the model sees it was provided).
    - Missing / unreadable / not-a-file paths return None — caller warns
      and skips them so a single bad path doesn't abort the whole run.
    """
    p = Path(path)
    if not p.is_file():
        return None
    try:
        size = p.stat().st_size
        with p.open("rb") as f:
            raw = f.read(MAX_FILE_BYTES + 1)
    except OSError as e:
        print(f"WARNING: cannot read {path}: {e}", file=sys.stderr)
        return None

    truncated = len(raw) > MAX_FILE_BYTES
    if truncated:
        raw = raw[:MAX_FILE_BYTES]

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return f"--- file: {path} ({size} bytes; binary, not inlined) ---\n--- end {path} ---"

    suffix = (
        f"\n... [truncated: kept first {MAX_FILE_BYTES} of {size} bytes]"
        if truncated
        else ""
    )
    return f"--- file: {path} ({size} bytes) ---\n{text}{suffix}\n--- end {path} ---"


def build_prompt(query: str, files: list[str]) -> str:
    """Combine inlined file blocks with the user's question into one prompt.

    If ``query`` is empty and at least one file was inlined, defaults to
    "Please review the file(s) above." so file-only invocations work.
    """
    blocks: list[str] = []
    for path in files:
        block = read_file_block(path)
        if block is None:
            print(f"WARNING: file not found or not readable: {path}", file=sys.stderr)
            continue
        blocks.append(block)
    if not query and blocks:
        query = "Please review the file(s) above."
    if blocks:
        blocks.append(f"--- question ---\n{query}")
        return "\n\n".join(blocks)
    return query


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


def parse_args(argv: list[str]) -> tuple[list[str], list[str], str]:
    """Parse CLI into (models, files, query). Query may be empty (file-only)."""
    models: list[str] = [m for m, _ in CREW]
    files: list[str] = []
    rest: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--models":
            if i + 1 >= len(argv):
                print("ERROR: --models needs a comma-separated list", file=sys.stderr)
                sys.exit(2)
            models = [m.strip() for m in argv[i + 1].split(",") if m.strip()]
            i += 2
        elif a == "--file":
            if i + 1 >= len(argv):
                print("ERROR: --file needs a PATH", file=sys.stderr)
                sys.exit(2)
            files.append(argv[i + 1])
            i += 2
        else:
            rest.append(a)
            i += 1
    query = " ".join(rest).strip()
    return models, files, query


def main() -> int:
    models, files, query = parse_args(sys.argv[1:])
    if not query and not files:
        print(__doc__)
        return 2

    key = os.environ.get(KEY_ENV, "")
    if not key:
        print(f"ERROR: {KEY_ENV} not set", file=sys.stderr)
        return 1

    prompt = build_prompt(query, files)
    # Show a short summary, not the full inlined prompt (which may be huge).
    summary = query or "(no question — file review only)"
    if files:
        summary += f"  [files: {', '.join(files)}]"
    print(f"🤖 Asking the crew ({len(models)} models): {summary}\n")
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(models)) as pool:
        futures = [pool.submit(ask, mid, prompt, key) for mid in models]
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
