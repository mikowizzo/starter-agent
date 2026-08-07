#!/usr/bin/env python3
"""Single-URL content extraction — trafilatura → Jina Reader → YouTube transcript."""

import json
import logging
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

import httpx

# ── Config ───────────────────────────────────────────────────────────────────

JINA_READER_URL = "https://r.jina.ai/"
JINA_API_KEY = os.environ.get("JINA_API_KEY", "")
USER_AGENT = "Scope-Scrape/1.0"
MAX_SCRAPE_CHARS = 30_000

# Auto-save scraped content to this dir so other tools (council) can attach as --files
SCRAPE_SAVE_DIR = Path(os.environ.get("SCRAPE_SAVE_DIR", "/tmp/scout-scrape"))

logger = logging.getLogger("scrape")

# ── Output helpers ───────────────────────────────────────────────────────────


def _result(url, *, title=None, content=None, method=None, error=None):
    """Build a normalised output dict."""
    if content and len(content) > MAX_SCRAPE_CHARS:
        content = content[:MAX_SCRAPE_CHARS] + "\n\n[... truncated at 30,000 chars]"
    return {
        "url": url,
        "title": title,
        "content": content,
        "method": method,
        "error": error,
    }


# ── URL validation ───────────────────────────────────────────────────────────

def _validate_url(url: str) -> str | None:
    """Return an error message if URL is invalid, or None if valid."""
    try:
        parsed = urlparse(url.strip())
    except Exception:
        return f"Could not parse URL: '{url}'"

    if parsed.scheme not in ("http", "https"):
        return f"Unsupported scheme '{parsed.scheme}' — only http/https allowed"

    if not parsed.hostname:
        return f"No hostname in URL: '{url}'"

    return None


# ── YouTube detection ────────────────────────────────────────────────────────

_YT_ID_RE = re.compile(
    r"""
    (?:
        youtube\.com/(?:watch\?(?:.*&)?v=|embed/|shorts/)
        |youtu\.be/
        |m\.youtube\.com/watch\?(?:.*&)?v=
    )
    ([A-Za-z0-9_-]{11})
    """,
    re.VERBOSE,
)


def _extract_video_id(url: str) -> str | None:
    """Return the 11-character YouTube video ID from a URL."""
    m = _YT_ID_RE.search(url.strip())
    return m.group(1) if m else None


def _yt_seconds_to_timestamp(seconds: float) -> str:
    """Convert float seconds to [H:MM:SS] or [M:SS] string."""
    total = int(seconds)
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    if h > 0:
        return f"[{h}:{m:02d}:{s:02d}]"
    return f"[{m}:{s:02d}]"


def _scrape_youtube(url: str) -> dict:
    """Extract YouTube transcript."""
    video_id = _extract_video_id(url)
    if not video_id:
        return _result(url, error=f"Could not extract YouTube video ID from '{url}'")

    try:
        from youtube_transcript_api import NoTranscriptFound, YouTubeTranscriptApi
    except ImportError:
        return _result(url, error="youtube-transcript-api is not installed")

    # Fetch transcript list
    try:
        api = YouTubeTranscriptApi()
        transcript_list = api.list(video_id)
    except Exception as exc:
        exc_name = type(exc).__name__
        friendly = {
            "VideoUnavailable": f"Video '{video_id}' is unavailable or does not exist.",
            "TranscriptsDisabled": f"Transcripts/captions are disabled for video '{video_id}'.",
        }
        msg = friendly.get(
            exc_name, f"Could not retrieve transcripts for '{video_id}' — {exc}"
        )
        return _result(url, error=msg)

    # Resolve language with cascading fallback
    transcript = None
    try:
        transcript = transcript_list.find_transcript(["en"])
    except NoTranscriptFound:
        pass

    if transcript is None:
        try:
            available_codes = [t.language_code for t in transcript_list]
            transcript = transcript_list.find_generated_transcript(available_codes)
            logger.info(
                "YouTube: 'en' not found for %s; fell back to '%s'",
                video_id, transcript.language_code,
            )
        except (NoTranscriptFound, StopIteration):
            try:
                transcript = next(iter(transcript_list))
                logger.info(
                    "YouTube: using first available transcript '%s' for %s",
                    transcript.language_code, video_id,
                )
            except StopIteration:
                return _result(
                    url, error=f"No transcripts available for video '{video_id}'."
                )

    # Fetch snippets
    try:
        fetched = transcript.fetch()
    except Exception as exc:
        return _result(url, error=f"Could not fetch transcript data — {exc}")

    # Cast to list for reliable empty check (FetchedTranscript isn't a list in ≥0.6)
    snippets = list(fetched)
    if not snippets:
        return _result(url, error=f"Transcript for video '{video_id}' is empty.")

    # Build transcript text
    body_lines = []
    for snippet in snippets:
        text = snippet.text.replace("\n", " ").strip()
        if not text:
            continue
        body_lines.append(f"{_yt_seconds_to_timestamp(snippet.start)} {text}")

    full_text = "\n".join(body_lines)
    title = f"YouTube Transcript — {video_id}"

    return _result(url, title=title, content=full_text, method="youtube")


# ── Trafilatura extraction ──────────────────────────────────────────────────


def _scrape_trafilatura(url: str) -> dict | None:
    """Try trafilatura extraction. Returns result dict or None."""
    try:
        import trafilatura
    except ImportError:
        return None

    try:
        with httpx.Client(timeout=15, follow_redirects=True) as client:
            resp = client.get(url, headers={"User-Agent": USER_AGENT})
            resp.raise_for_status()

        # Explicit encoding to avoid mojibake
        encoding = resp.charset_encoding or "utf-8"
        html = resp.content.decode(encoding, errors="replace")

        # Single-pass extraction via bare_extraction (title + content together)
        doc = trafilatura.bare_extraction(html, include_links=True, favor_precision=True)
        if doc is None or not doc.text:
            return None

        return _result(url, title=doc.title or None, content=doc.text, method="trafilatura")
    except httpx.TimeoutException:
        logger.warning("trafilatura timed out for %s", url)
        return None
    except httpx.HTTPStatusError as exc:
        logger.warning("trafilatura got HTTP %s for %s", exc.response.status_code, url)
        return None
    except httpx.RequestError as exc:
        logger.warning("trafilatura request failed for %s: %s", url, exc)
        return None


# ── Jina Reader fallback ────────────────────────────────────────────────────


def _scrape_jina(url: str) -> dict | None:
    """Try Jina Reader API. Returns result dict or None."""
    try:
        headers = {
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        if JINA_API_KEY:
            headers["Authorization"] = f"Bearer {JINA_API_KEY}"

        with httpx.Client(timeout=30, follow_redirects=True) as client:
            resp = client.get(f"{JINA_READER_URL}{url}", headers=headers)
            resp.raise_for_status()

        # Validate JSON response (Jina sometimes returns HTML on errors)
        content_type = resp.headers.get("content-type", "")
        if "application/json" not in content_type:
            logger.warning("Jina returned non-JSON content-type: %s", content_type)
            return None

        data = resp.json().get("data", {})
        jina_content = data.get("content", "")
        if not jina_content:
            return None

        return _result(
            url,
            title=data.get("title") or None,
            content=jina_content,
            method="jina",
        )
    except httpx.TimeoutException:
        logger.warning("Jina timed out for %s", url)
        return None
    except httpx.HTTPStatusError as exc:
        logger.warning("Jina got HTTP %s for %s", exc.response.status_code, url)
        return None
    except httpx.RequestError as exc:
        logger.warning("Jina request failed for %s: %s", url, exc)
        return None


# ── Main ─────────────────────────────────────────────────────────────────────


def scrape(url: str) -> dict:
    """Scrape a single URL and return a result dict with title, content, method, error."""
    # URL validation — fail fast
    err = _validate_url(url)
    if err:
        return _result(url, error=err)

    # YouTube detection
    if _extract_video_id(url):
        return _scrape_youtube(url)

    # Trafilatura → Jina fallback chain
    result = _scrape_trafilatura(url)
    if result is None:
        result = _scrape_jina(url)
    if result is None:
        result = _result(url, error="all extraction methods failed")
    return result



def _save_scraped_content(result: dict) -> str | None:
    """Save scraped content to a temp file. Returns the file path, or None if nothing to save."""
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
    logger.info("saved scraped content to %s", filepath)
    return str(filepath)

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(name)s %(levelname)s: %(message)s",
    )

    if len(sys.argv) < 2:
        print("Usage: python scrape.py <url>", file=sys.stderr)
        sys.exit(1)

    result = scrape(sys.argv[1])

    # Auto-save to temp dir
    saved_path = _save_scraped_content(result)
    if saved_path:
        result["saved_to"] = saved_path

    print(json.dumps(result, indent=2, ensure_ascii=False))

    # Non-zero exit on error so downstream scripts can detect failure
    if result.get("error"):
        sys.exit(1)


if __name__ == "__main__":
    main()