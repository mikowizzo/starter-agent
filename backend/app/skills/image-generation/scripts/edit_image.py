#!/usr/bin/env python3
"""Image editing via the Pollinations API (OpenAI-compatible endpoint).

Usage:
  # Edit a local file (uploaded as multipart/form-data)
  python edit_image.py "add a wide-brimmed hat" --image-file /workspace/frontend/public/generated/source.jpg

  # Edit from one or more image URLs (JSON body)
  python edit_image.py "change to warm tones" --image-url https://example.com/img.jpg
  python edit_image.py "blend these two scenes" \
      --image-url https://example.com/a.jpg --image-url https://example.com/b.jpg

Auth: reads POLLEN_API_KEY from environment.
Model: p-image-edit (the only model on the edit endpoint - no fallback).
Output: saves edited image under /workspace/frontend/public/generated/
        (served by the frontend at /generated/...), prints the display
        URL (/generated/<file>) as the last line of stdout.
"""

import argparse
import base64
import json
import mimetypes
import os
import re
import sys
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = "https://gen.pollinations.ai"
EDIT_URL = f"{BASE_URL}/v1/images/edits"
MODEL = "p-image-edit"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
# Frontend Vite dev server serves this directory at the site root, so
# /generated/<file> resolves in the chat UI (same origin, any port).
SAFE_OUTPUT_DIR = "/workspace/frontend/public/generated"
MAX_ATTEMPTS = 2
ACCEPTABLE_CONTENT_TYPES = ("image/", "application/octet-stream")


class EditError(Exception):
    """Raised when image editing fails."""


def positive_int(value):
    """argparse type: reject zero, negatives, and non-integers."""
    try:
        ivalue = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not an integer")
    if ivalue <= 0:
        raise argparse.ArgumentTypeError(f"{value!r} must be positive")
    return ivalue


def get_api_key():
    key = os.environ.get("POLLEN_API_KEY")
    if not key:
        print("ERROR: POLLEN_API_KEY environment variable not set.", file=sys.stderr)
        sys.exit(1)
    return key


def build_auth_headers(key):
    """Return HTTP headers with the API key in the Authorization header."""
    return {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Authorization": f"Bearer {key}",
    }


def slugify_prompt(prompt, max_words=4):
    """Turn a prompt into a short filesystem-safe slug (e.g. 'add-hat-wider')."""
    words = re.sub(r"[^a-z0-9 ]", "", prompt.lower()).split()
    slug = "-".join(words[:max_words])
    slug = re.sub(r"(^-+|-+$)", "", slug)[:40]
    return slug or "edit"


def resolve_output_path(output, prompt=None):
    """Return a safe output path under SAFE_OUTPUT_DIR."""
    if output is None:
        name = slugify_prompt(prompt) if prompt else "edit"
        suffix = uuid.uuid4().hex[:6]
        filename = f"{name}-{suffix}.jpg"
        return os.path.join(SAFE_OUTPUT_DIR, filename)

    resolved = os.path.realpath(output)
    safe_root = os.path.realpath(SAFE_OUTPUT_DIR)
    if not resolved.startswith(safe_root + os.sep):
        print(f"ERROR: --output must resolve under {SAFE_OUTPUT_DIR}/", file=sys.stderr)
        sys.exit(1)
    return resolved


def public_url(path):
    """Map an absolute path under SAFE_OUTPUT_DIR to its /generated/ URL."""
    return "/generated/" + os.path.basename(os.path.realpath(path))


def guess_content_type(filepath):
    """Guess MIME type from filename, defaulting to image/jpeg."""
    ct, _ = mimetypes.guess_type(filepath)
    return ct or "image/jpeg"


def build_multipart(fields, files):
    """Build a multipart/form-data body.

    Args:
        fields: dict of {name: value} for text fields.
        files:  list of (field_name, filepath, content_type) tuples.

    Returns (body_bytes, boundary_str).
    """
    boundary = uuid.uuid4().hex
    parts = []

    for name, value in fields.items():
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        )
        parts.append(f"{value}\r\n".encode())

    for name, filepath, content_type in files:
        filename = os.path.basename(filepath)
        with open(filepath, "rb") as f:
            file_data = f.read()
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(
            f'Content-Disposition: form-data; name="{name}"; '
            f'filename="{filename}"\r\n'.encode()
        )
        parts.append(f"Content-Type: {content_type}\r\n\r\n".encode())
        parts.append(file_data)
        parts.append(b"\r\n")

    parts.append(f"--{boundary}--\r\n".encode())

    return b"".join(parts), boundary


def parse_response_and_save(data, content_type, output_path, key):
    """Parse the API response and return (saved_path, display_url).

    ``display_url`` is the /generated/<file> URL of the copy saved under
    SAFE_OUTPUT_DIR - same origin as the chat UI, renders in markdown
    directly; no backend router or auth needed to view.

    Handles several response formats:
      - Raw image bytes (image/* content type)
      - JSON ``{"data": [{"b64_json": "..."} | {"url": "..."}]}``
      - JSON ``{"b64_json": "..."}`` or ``{"url": "..."}``

    Raises EditError if no image can be extracted.
    """
    # Direct image bytes
    if any(content_type.startswith(t) for t in ACCEPTABLE_CONTENT_TYPES):
        with open(output_path, "wb") as f:
            f.write(data)
        return output_path, public_url(output_path)

    # JSON response
    try:
        resp_json = json.loads(data.decode())
    except (json.JSONDecodeError, UnicodeDecodeError):
        print(
            f"ERROR: Unexpected response (content-type: {content_type})",
            file=sys.stderr,
        )
        print(f"Response: {data[:500].decode(errors='replace')}", file=sys.stderr)
        raise EditError(f"Unexpected content type: {content_type}")

    image_b64 = None
    image_url = None

    if isinstance(resp_json, dict):
        if "data" in resp_json and resp_json["data"]:
            item = resp_json["data"][0] if isinstance(resp_json["data"], list) else resp_json["data"]
            if isinstance(item, dict):
                image_b64 = item.get("b64_json")
                image_url = item.get("url")
        # Fallback to top-level fields
        if not image_b64 and not image_url:
            image_b64 = resp_json.get("b64_json")
            image_url = resp_json.get("url")

    if image_url:
        # Download the result image so --output still gets a local copy.
        # gen.pollinations.ai URLs require the API key to view.
        req = urllib.request.Request(
            image_url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "*/*",
                "Authorization": f"Bearer {key}",
            },
        )
        resp = urllib.request.urlopen(req, timeout=60)
        img_bytes = resp.read()
        with open(output_path, "wb") as f:
            f.write(img_bytes)
        return output_path, public_url(output_path)

    if image_b64:
        img_bytes = base64.b64decode(image_b64)
        with open(output_path, "wb") as f:
            f.write(img_bytes)
        return output_path, public_url(output_path)

    print("ERROR: Could not extract image from response.", file=sys.stderr)
    print(
        f"Response: {json.dumps(resp_json, indent=2)[:1000]}",
        file=sys.stderr,
    )
    raise EditError("No image data in response")


def edit_image(
    prompt,
    image_files=None,
    image_urls=None,
    seed=None,
    output=None,
    width=None,
    height=None,
):
    """Edit an image via the Pollinations edit endpoint.

    If *image_files* is provided, sends multipart/form-data (file upload).
    Otherwise sends JSON with image URLs.

    Returns the output path of the saved image.
    """
    if not prompt or not prompt.strip():
        print("ERROR: prompt is empty", file=sys.stderr)
        sys.exit(1)

    if not image_files and not image_urls:
        print("ERROR: provide at least one --image-file or --image-url", file=sys.stderr)
        sys.exit(1)

    key = get_api_key()
    output_path = resolve_output_path(output, prompt)
    parent = os.path.dirname(output_path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    # Build text fields shared by both modes
    fields = {"model": MODEL, "prompt": prompt}
    if seed is not None:
        fields["seed"] = str(seed)
    if (width is None) != (height is None):
        print("ERROR: provide both --width and --height (or neither)", file=sys.stderr)
        sys.exit(1)
    if width and height:
        fields["size"] = f"{width}x{height}"

    print(f"Editing with model={MODEL}...", file=sys.stderr)
    print(f"Prompt: {prompt}", file=sys.stderr)

    # --- Multipart mode (local files) ---
    if image_files:
        # Validate all files exist before sending
        for path in image_files:
            if not os.path.isfile(path):
                print(f"ERROR: file not found: {path}", file=sys.stderr)
                sys.exit(1)

        files_meta = [
            ("image", path, guess_content_type(path)) for path in image_files
        ]
        body, boundary = build_multipart(fields, files_meta)

        headers = build_auth_headers(key)
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"

    # --- JSON mode (URLs) ---
    else:
        payload = dict(fields)
        if len(image_urls) == 1:
            payload["image"] = image_urls[0]
        else:
            payload["image"] = image_urls

        body = json.dumps(payload).encode()
        headers = build_auth_headers(key)
        headers["Content-Type"] = "application/json"

    # --- Send request with retry ---
    for attempt in range(1, MAX_ATTEMPTS + 1):
        req = urllib.request.Request(
            EDIT_URL,
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            resp = urllib.request.urlopen(req, timeout=180)
            data = resp.read()
            content_type = resp.headers.get("Content-Type", "")

            _, display = parse_response_and_save(data, content_type, output_path, key)
            print(f"Saved to {output_path}", file=sys.stderr)
            print(display)
            return output_path

        except urllib.error.HTTPError as e:
            resp_body = e.read().decode(errors="replace")[:500]
            if e.code in (429, 503) and attempt < MAX_ATTEMPTS:
                wait = 2 ** attempt
                print(
                    f"HTTP {e.code} (attempt {attempt}/{MAX_ATTEMPTS}); "
                    f"retrying in {wait}s...",
                    file=sys.stderr,
                )
                time.sleep(wait)
                continue
            print(f"HTTP {e.code}: {resp_body}", file=sys.stderr)
            if e.code == 402:
                print("Insufficient pollen balance.", file=sys.stderr)
            raise EditError(f"HTTP {e.code}: {resp_body}")
        except EditError:
            raise
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            raise EditError(str(e))

    raise EditError("All attempts exhausted")


def main():
    parser = argparse.ArgumentParser(
        description="Edit images via the Pollinations API (p-image-edit model)."
    )
    parser.add_argument(
        "prompt", help="Edit instructions",
    )
    parser.add_argument(
        "--image-file", action="append", default=None,
        help="Local image file to edit (can be repeated for multiple images)",
    )
    parser.add_argument(
        "--image-url", action="append", default=None,
        help="Image URL to edit (can be repeated for multiple images)",
    )
    parser.add_argument(
        "--seed", type=int, default=None,
        help="Reproducibility seed",
    )
    parser.add_argument(
        "--output", "-o", default=None,
        help=f"Output file path (must resolve under {SAFE_OUTPUT_DIR}/)",
    )
    parser.add_argument("--width", type=positive_int, default=None, help="Width in px")
    parser.add_argument("--height", type=positive_int, default=None, help="Height in px")

    args = parser.parse_args()

    prompt = args.prompt

    try:
        edit_image(
            prompt=prompt,
            image_files=args.image_file,
            image_urls=args.image_url,
            seed=args.seed,
            output=args.output,
            width=args.width,
            height=args.height,
        )
    except EditError as e:
        print(f"ERROR: Edit failed: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
