#!/usr/bin/env python3
"""Image generation via Pollinations.

LOCKED TO Z-IMAGE: only the zimage model is allowed. Any other --model
value is rejected by argparse (choices=[zimage]); the tool is sealed
so flux, p-image, or any other model cannot be requested.

Free public endpoint by default; authenticated /v1/images/generations
when POLLEN_API_KEY is set (required to actually receive zimage - the
free endpoint silently substitutes sana while still billing zimage).

Usage:
  python generate_image.py "a cat" --width 1024 --height 768
  python generate_image.py --prompt "a cat"

Saves generated images into the frontend public folder
(/workspace/frontend/public/generated) and prints the display URL
(/generated/<file>) as the last stdout line. The free path prints the
public image.pollinations.ai URL directly instead.
"""

import argparse
import base64
import json
import os
import re
import sys
import uuid
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = "https://image.pollinations.ai/prompt"
AUTH_URL = "https://gen.pollinations.ai/v1/images/generations"
DEFAULT_MODEL = "zimage"
# The frontend Vite dev server serves frontend/public/ at the site root,
# so /generated/<file> resolves in the chat UI (same origin, any port).
PUBLIC_DIR = "/workspace/frontend/public/generated"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def positive_int(value):
    """argparse type: reject zero, negatives, and non-integers."""
    try:
        ivalue = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not an integer")
    if ivalue <= 0:
        raise argparse.ArgumentTypeError(f"{value!r} must be positive")
    return ivalue


def build_url(prompt, model=DEFAULT_MODEL, width=1024, height=1024, seed=None):
    """Return the public Pollinations image URL (renders in markdown as-is)."""
    params = {
        "width": str(width),
        "height": str(height),
        "nologo": "true",
    }
    if model:
        params["model"] = model
    if seed is not None:
        params["seed"] = str(seed)
    return f"{BASE_URL}/{urllib.parse.quote(prompt, safe='')}?{urllib.parse.urlencode(params)}"


def slugify_prompt(prompt, max_words=4):
    """Turn a prompt into a short filesystem-safe slug (e.g. 'heroic-portrait-usopp')."""
    words = re.sub(r"[^a-z0-9 ]", "", prompt.lower()).split()
    slug = "-".join(words[:max_words])
    slug = re.sub(r"(^-+|-+$)", "", slug)[:40]
    return slug or "image"


def sniff_ext(img_bytes: bytes) -> str:
    """'.png' for PNG magic bytes, '.jpg' otherwise."""
    return ".png" if img_bytes[:8] == b"\x89PNG\r\n\x1a\n" else ".jpg"


def data_uri(img_bytes: bytes) -> str:
    """Data-URI with a MIME sniffed from magic bytes (PNG vs JPEG)."""
    ctype = "image/png" if img_bytes[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
    return f"data:{ctype};base64," + base64.b64encode(img_bytes).decode()


def save_public(img_bytes: bytes, prompt: str) -> str:
    """Write image bytes under PUBLIC_DIR and return the display URL.

    Falls back to a data URI (with a warning) only if the public folder is
    unavailable — e.g. when the script runs outside the app containers.
    """
    try:
        os.makedirs(PUBLIC_DIR, exist_ok=True)
        filename = f"{slugify_prompt(prompt)}-{uuid.uuid4().hex[:6]}{sniff_ext(img_bytes)}"
        with open(os.path.join(PUBLIC_DIR, filename), "wb") as f:
            f.write(img_bytes)
        return f"/generated/{filename}"
    except OSError as e:
        print(
            f"WARNING: could not write {PUBLIC_DIR} ({e}); using data URI",
            file=sys.stderr,
        )
        return data_uri(img_bytes)


def generate_authed(prompt, model, width, height, key):
    """Generate via the authenticated endpoint (real model, e.g. zimage).

    gen.pollinations.ai image URLs require the API key to view, so fetch
    the bytes, save them into the frontend public folder, and return the
    /generated/<file> display URL (viewable without auth).
    """
    payload = {
        "model": model,
        "prompt": prompt,
        "size": f"{width}x{height}",
        "response_format": "url",
    }
    req = urllib.request.Request(
        AUTH_URL,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=180)
    except urllib.error.HTTPError as e:
        print(
            f"ERROR: HTTP {e.code}: {e.read().decode(errors='replace')[:500]}",
            file=sys.stderr,
        )
        sys.exit(1)
    data = json.loads(resp.read().decode(errors="replace"))
    item = data.get("data", [{}])[0] if isinstance(data.get("data"), list) else data
    if not isinstance(item, dict):
        print(f"ERROR: no image in response: {json.dumps(data)[:500]}", file=sys.stderr)
        sys.exit(1)

    url = item.get("url")
    if url and url.startswith(("http://", "https://")):
        img_req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {key}", "User-Agent": USER_AGENT},
        )
        try:
            img = urllib.request.urlopen(img_req, timeout=180).read()
        except urllib.error.HTTPError as e:
            print(
                f"ERROR: fetching image HTTP {e.code}: "
                f"{e.read().decode(errors='replace')[:300]}",
                file=sys.stderr,
            )
            sys.exit(1)
        return save_public(img, prompt)

    b64 = item.get("b64_json")
    if b64:
        return save_public(base64.b64decode(b64), prompt)
    print(f"ERROR: no image in response: {json.dumps(data)[:500]}", file=sys.stderr)
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Generate images via Pollinations."
    )
    parser.add_argument("prompt", nargs="?", default=None, help="Image prompt")
    parser.add_argument("--prompt", dest="prompt_opt", default=None,
                        help="Image prompt (alternative to positional argument)")
    parser.add_argument("--model", "-m", default=DEFAULT_MODEL,
                        choices=[DEFAULT_MODEL],
                        help=f"Image model (locked to {DEFAULT_MODEL}; no other models allowed)")
    parser.add_argument("--width", type=positive_int, default=1024, help="Width in px")
    parser.add_argument("--height", type=positive_int, default=1024, help="Height in px")
    parser.add_argument("--seed", type=int, default=None, help="Reproducibility seed")

    args = parser.parse_args()

    prompt = args.prompt_opt or args.prompt
    if not prompt:
        parser.error("a prompt is required (positional or --prompt)")

    key = os.environ.get("POLLEN_API_KEY")
    if key:
        print(f"Authenticated generation via {AUTH_URL} (model={args.model})...",
              file=sys.stderr)
        print(generate_authed(prompt, args.model, args.width, args.height, key))
    else:
        print(f"Free endpoint (no POLLEN_API_KEY \u2014 server may substitute model)",
              file=sys.stderr)
        print(build_url(prompt, args.model, args.width, args.height, args.seed))


if __name__ == "__main__":
    main()
