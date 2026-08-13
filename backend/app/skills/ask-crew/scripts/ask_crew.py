#!/usr/bin/env python3
"""Ask the model crew — fire one query at multiple models in parallel.

Each model uses a bespoke route (Synthetic / ZAI / OpenRouter) — see CREW
below.

Usage:
  python ask_crew.py "your question here"
  python ask_crew.py --models x-ai/grok-4.6,google/gemini-3.7-flash "your question"
  python ask_crew.py --file path/to/code.py "review this"
  python ask_crew.py --file a.py --file b.py "compare these"
  python ask_crew.py --file README.md         # default: "please review"

Allowed --models ids (exact match; anything else is rejected with this list):
  hf:moonshotai/Kimi-K3    Kimi K3            (Synthetic)
  x-ai/grok-4.6            Grok 4.6           (OpenRouter)
  google/gemini-3.7-flash  Gemini 3.7 Flash   (OpenRouter)
  (default: all of the above)

Pass one or more --file PATH args to inline file contents into the prompt.
Text files are inlined; binary files are noted but not inlined. Files larger
than MAX_FILE_BYTES are truncated with a warning.

Reads SYNTHETIC_API_KEY (Kimi K3) and OPENROUTER_API_KEY
(Grok 4.6, Gemini 3.7 Flash) from the env.
"""
import concurrent.futures
import hashlib
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

BASE_URL = "https://opencode.ai/zen/go/v1/chat/completions"
KEY_ENV = "OPENCODE_API_KEY"

# The model crew: (model id, display label, [base_url, api_key_env],
#   input_price_per_1m, output_price_per_1m).
# Two-tuples (id, label) route via OpenCode (BASE_URL + OPENCODE_API_KEY) at
# $0 pricing; six-tuples override the route and pricing — e.g. Kimi K3 goes
# through the Synthetic API instead.
CREW = [
    ("hf:moonshotai/Kimi-K3", "Kimi K3 (Synthetic)",
     "https://api.synthetic.new/v1", "SYNTHETIC_API_KEY",
     2.80, 14.00),
    ("x-ai/grok-4.6", "Grok 4.6 (OpenRouter)",
     "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY",
     2.00, 6.00),
    ("google/gemini-3.7-flash", "Gemini 3.7 Flash (OpenRouter)",
     "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY",
     0.75, 3.75),
]



def route(entry: tuple) -> tuple[str, str]:
    """(full endpoint url, api_key_env) for a crew entry; default = OpenCode."""
    if len(entry) >= 4:
        base, key_env = entry[2], entry[3]
        # entry base_url is a bare base (e.g. .../v1) — append the completions path.
        url = base if base.endswith("/chat/completions") else base.rstrip("/") + "/chat/completions"
        return url, key_env
    return BASE_URL, KEY_ENV

TIMEOUT = 600  # 10 min per model — long enough for complex queries
# Per-file inlining cap. Large files blow the context window; truncate with a
# marker so the model still sees the structure.
MAX_FILE_BYTES = 100_000
# Rough chars-per-token for the cache-hint estimate. Display-only; the real
# prefix size is whatever the provider's tokenizer produces.
CHARS_PER_TOKEN = 4
# Cloudflare 403s Python-urllib's default UA (error 1010) — browser UA required.
HEADERS = {
    "Authorization": "Bearer {key}",
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
}


def get_env_key(name: str) -> str:
    """Read a key from the environment, falling back to /workspace/.env."""
    key = os.environ.get(name, "")
    if key:
        return key
    env_file = Path("/workspace/.env")
    if env_file.is_file():
        try:
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if line.startswith(name + "="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
        except OSError:
            pass
    return ""


def _stable_path_key(path: str) -> str:
    """Deterministic sort key for a file path.

    Uses sha256 of the path string — NOT built-in hash(), which is salted by
    PYTHONHASHSEED and changes across processes. A stable ordering of file
    blocks keeps the prompt prefix byte-for-byte identical between runs, so
    provider-side prompt caching (KV-cache reuse) actually engages.
    """
    return hashlib.sha256(path.encode("utf-8")).hexdigest()


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
        # Fall back to the workspace root — the script may run from a
        # different cwd than the caller (e.g. invoked via skill tools).
        alt = Path("/workspace") / path
        if alt.is_file():
            p = alt
        else:
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

    Prompt-cache hygiene: file blocks are emitted in a deterministic order
    (sorted by a stable sha256 of the path), independent of CLI arg order.
    Provider KV-cache reuse keys on an identical prompt prefix, so
    "--file a.py --file b.py" and "--file b.py --file a.py" now produce the
    SAME prefix and hit the cache on repeat runs. File contents themselves
    are untouched — only the block order changes. The question always comes
    last so the entire file section remains a cacheable static prefix.
    """
    blocks: list[str] = []
    # Sort BEFORE reading so even unreadable files can't perturb the order
    # of the survivors (they're skipped with a warning downstream).
    for path in sorted(files, key=_stable_path_key):
        block = read_file_block(path)
        if block is None:
            print(f"WARNING: file not found or not readable: {path}", file=sys.stderr)
            continue
        blocks.append(block)
    if not query and blocks:
        query = "Please review the file(s) above."
    if blocks:
        prefix_chars = sum(len(b) for b in blocks)
        est_tokens = prefix_chars // CHARS_PER_TOKEN
        print(
            f"📎 Files sorted for cache reuse "
            f"({len(blocks)} blocks, ~{est_tokens:,} tokens prefix)"
        )
        blocks.append(f"--- question ---\n{query}")
        return "\n\n".join(blocks)
    return query


def ask(model_id: str, query: str, base_url: str, key: str) -> tuple[str, str, float, dict]:
    """Ask one model, return (model_id, content_or_error, elapsed_seconds, usage).

    ``usage`` is {"prompt_tokens": int, "completion_tokens": int} extracted
    from the response's ``usage`` block (defaults to zeros on error/missing).
    """
    body = json.dumps(
        {"model": model_id, "messages": [{"role": "user", "content": query}]}
    ).encode()
    req = urllib.request.Request(
        base_url,
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
        usage_raw = data.get("usage") or {}
        usage = {
            "prompt_tokens": usage_raw.get("prompt_tokens", 0),
            "completion_tokens": usage_raw.get("completion_tokens", 0),
        }
        return model_id, content.strip() or "(empty reply)", time.monotonic() - t0, usage
    except Exception as e:
        return model_id, f"ERROR: {type(e).__name__}: {e}", time.monotonic() - t0, {"prompt_tokens": 0, "completion_tokens": 0}


def parse_args(argv: list[str]) -> tuple[list[str], list[str], str]:
    """Parse CLI into (models, files, query). Query may be empty (file-only)."""
    models: list[str] = [e[0] for e in CREW]
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
    jobs = []  # (entry, base_url, key)
    for mid in models:
        entry = next((e for e in CREW if e[0] == mid), None)
        if entry is None:
            print(
                f"ERROR: unknown model {mid!r}. Allowed --models ids "
                f"(exact match, no aliases):\n"
                + "\n".join(f"  {e[0]}" for e in CREW),
                file=sys.stderr,
            )
            return 2
        base_url, key_env = route(entry)
        key = get_env_key(key_env)
        if not key:
            print(f"ERROR: {key_env} not set (needed for {entry[1]})", file=sys.stderr)
            return 1
        jobs.append((entry, base_url, key))

    prompt = build_prompt(query, files)
    # Show a short summary, not the full inlined prompt (which may be huge).
    summary = query or "(no question — file review only)"
    if files:
        summary += f"  [files: {', '.join(files)}]"
    print(f"🤖 Asking the crew ({len(jobs)} models): {summary}\n")
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = [pool.submit(ask, e[0], prompt, base_url, key) for e, base_url, key in jobs]
        results = [f.result() for f in futures]

    order = {e[0][0]: i for i, e in enumerate(jobs)}
    total_cost = 0.0
    for mid, content, dt, usage in sorted(results, key=lambda r: order[r[0]]):
        entry = next((e for e in CREW if e[0] == mid), None)
        label = entry[1] if entry else mid
        in_tok = usage["prompt_tokens"]
        out_tok = usage["completion_tokens"]
        # Calculate cost from token counts + per-model pricing (per 1M tokens)
        in_price = entry[4] if entry and len(entry) >= 6 else 0.0
        out_price = entry[5] if entry and len(entry) >= 6 else 0.0
        cost = in_tok * in_price / 1_000_000 + out_tok * out_price / 1_000_000
        total_cost += cost
        print(f"── {label} ({mid}) — {dt:.1f}s · {in_tok:,}→{out_tok:,} tok · ${cost:.4f} ──")
        print(content)
        print()
    print(f"💰 Total crew cost: ${total_cost:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
