#!/usr/bin/env python3
"""Single-URL content extraction — thin wrapper around app.services.url_fetch.

Kept for backward compatibility. Prefer the `read` tool directly:
    read("https://example.com/article")
"""

import json
import logging
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

# Core logic lives in the shared service now
from app.services.url_fetch import fetch_url

# ── Config ──

SCRAPE_SAVE_DIR = Path("/tmp/scout-scrape")
MAX_SCRAPE_CHARS = 30_000

logger = logging.getLogger("scrape")


def _save_scraped_content(result: dict) -> str | None:
    """Save scraped content to a temp file. Returns the file path, or None."""
    content = result.get("content")
    if not content:
        return None

    SCRAPE_SAVE_DIR.mkdir(parents=True, exist_ok=True)

    url = result.get("url", "")
    parsed = urlparse(url)
    slug = re.sub(r'[^a-zA-Z0-9._-]', '_', (parsed.path.strip("/").replace("/", "-") or parsed.hostname or "scrape"))[:60] or "scrape"
    filename = f"{slug}.txt"

    filepath = SCRAPE_SAVE_DIR / filename
    header = f"Source: {url}\nTitle: {result.get('title') or ''}\nMethod: {result.get('method', 'unknown')}\n{'=' * 60}\n\n"
    filepath.write_text(header + content, encoding="utf-8")
    return str(filepath)


def main():
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s: %(message)s")

    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(
            "Usage: python scrape.py <url> [--wait-for-selector SELECTOR] "
            "[--max-wait-ms MS]",
            file=sys.stderr,
        )
        sys.exit(1)

    url = args[0]
    wait_for_selector = None
    max_wait_ms = None
    if "--wait-for-selector" in args:
        i = args.index("--wait-for-selector")
        wait_for_selector = args[i + 1]
    if "--max-wait-ms" in args:
        i = args.index("--max-wait-ms")
        max_wait_ms = int(args[i + 1])

    result = fetch_url(
        url,
        wait_for_selector=wait_for_selector,
        max_wait_ms=max_wait_ms,
    )

    saved_path = _save_scraped_content(result)
    if saved_path:
        result["saved_to"] = saved_path

    print(json.dumps(result, indent=2, ensure_ascii=False))

    if result.get("error"):
        sys.exit(1)


if __name__ == "__main__":
    main()
