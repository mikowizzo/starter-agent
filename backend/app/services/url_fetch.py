"""URL content extraction — trafilatura → Playwright render → Jina Reader.

Shared by the `read` tool (code_tools) and the scout skill's scrape.py.

Fallback chain:
    YouTube transcript (auto-detected) → trafilatura (raw fetch)
    → headless Chromium render + trafilatura → Jina Reader
"""

import atexit
import contextlib
import logging
import os
import re
import threading
import time
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

# ── Config ───────────────────────────────────────────────────────────

JINA_READER_URL = "https://r.jina.ai/"
JINA_API_KEY = os.environ.get("JINA_API_KEY", "")
USER_AGENT = "Scope-Read/1.0"
MAX_FETCH_CHARS = 30_000

# Playwright rendering
RENDER_TIMEOUT_MS = 30_000
RENDER_SETTLE_MS = 5_000      # grace period for SPA hydration after DOMContentLoaded
MIN_RENDERED_TEXT = 200       # below this, the visible-DOM fallback is a JS shell
RENDER_MAX_WAIT_MS = 15_000   # cap on the SPA stabilization poll (adaptive wait)
RENDER_POLL_INTERVAL_MS = 1_000  # poll cadence while page content is still growing
RENDER_STABLE_READS = 2       # consecutive equal text lengths = content settled
_BLOCKED_RESOURCES = ("image", "media", "font")

# ── Result helper ────────────────────────────────────────────────────


def _result(url, *, title=None, content=None, method=None, error=None):
    """Build a normalised output dict."""
    if content and len(content) > MAX_FETCH_CHARS:
        content = content[:MAX_FETCH_CHARS] + "\n\n[... truncated at 30,000 chars]"
    return {
        "url": url,
        "title": title,
        "content": content,
        "method": method,
        "error": error,
    }


# ── URL validation ───────────────────────────────────────────────────


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


# ── YouTube detection ────────────────────────────────────────────────

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


def _fetch_youtube(url: str) -> dict:
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


# ── Trafilatura extraction ───────────────────────────────────────────


def _extract_trafilatura(html: str) -> tuple[str | None, str | None]:
    """Extract (title, text) from HTML via trafilatura. (None, None) on failure."""
    try:
        import trafilatura
    except ImportError:
        return None, None

    doc = trafilatura.bare_extraction(html, include_links=True, favor_precision=True)
    if doc is None or not doc.text:
        return None, None
    return doc.title or None, doc.text


def _fetch_trafilatura(url: str) -> dict | None:
    """Plain-GET + trafilatura extraction. Returns result dict or None."""
    try:
        with httpx.Client(timeout=15, follow_redirects=True) as client:
            resp = client.get(url, headers={"User-Agent": USER_AGENT})
            resp.raise_for_status()

        # Explicit encoding to avoid mojibake
        encoding = resp.charset_encoding or "utf-8"
        html = resp.content.decode(encoding, errors="replace")

        title, text = _extract_trafilatura(html)
        if text is None:
            return None
        return _result(url, title=title, content=text, method="trafilatura")
    except httpx.TimeoutException:
        logger.warning("trafilatura timed out for %s", url)
        return None
    except httpx.HTTPStatusError as exc:
        logger.warning("trafilatura got HTTP %s for %s", exc.response.status_code, url)
        return None
    except httpx.RequestError as exc:
        logger.warning("trafilatura request failed for %s: %s", url, exc)
        return None


# ── Playwright rendering (singleton browser) ─────────────────────────
#
# The sync Playwright API is bound to the thread that starts it, so the
# browser lives forever once launched — never hand it across threads.

_pw_lock = threading.Lock()
_pw = None            # sync_playwright() handle
_pw_browser = None    # singleton Chromium instance
_pw_broken = False    # True once launch has failed → skip rendering for good


def _close_browser_locked() -> None:
    """Tear down Playwright silently. Caller must hold _pw_lock."""
    global _pw, _pw_browser
    if _pw_browser is not None:
        with contextlib.suppress(Exception):
            _pw_browser.close()
    if _pw is not None:
        with contextlib.suppress(Exception):
            _pw.stop()
    _pw = _pw_browser = None


def _get_browser():
    """Return the singleton Chromium, (re)starting it if needed.

    Self-healing: a crashed browser (is_connected() → False) is closed and
    relaunched. An outright launch failure disables rendering for the
    process lifetime — the fallback chain simply moves on to Jina.
    """
    global _pw, _pw_browser, _pw_broken

    if _pw_broken:
        return None

    with _pw_lock:
        if _pw_browser is not None:
            if _pw_browser.is_connected():
                return _pw_browser
            logger.warning("Playwright browser disconnected — relaunching")
            _close_browser_locked()

        try:
            from playwright.sync_api import sync_playwright

            _pw = sync_playwright().start()
            _pw_browser = _pw.chromium.launch(headless=True)
            logger.info("Playwright Chromium launched")
            return _pw_browser
        except Exception as exc:
            logger.warning("Playwright unavailable (%s) — rendering disabled", exc)
            _close_browser_locked()
            _pw_broken = True
            return None


def _shutdown_browser() -> None:
    """atexit hook: release Chromium cleanly on process exit."""
    with _pw_lock:
        _close_browser_locked()


atexit.register(_shutdown_browser)


def _block_heavy_assets(route) -> None:
    """Route handler: drop images/media/fonts for speed."""
    if route.request.resource_type in _BLOCKED_RESOURCES:
        route.abort()
    else:
        route.continue_()


def _fetch_playwright(
    url: str,
    *,
    wait_for_selector: str | None = None,
    max_wait_ms: int | None = None,
) -> dict | None:
    """Render in headless Chromium, then extract via trafilatura.

    This exists for JS SPAs (e.g. openrouter.ai/models) where a plain GET
    returns an empty shell. trafilatura gets first crack at the rendered
    DOM; if it finds nothing, we fall back to the page's *visible* text
    via inner_text — real rendered content, not raw-HTML garbage.

    Heavy SPAs that fetch their data after hydration get an adaptive
    wait: we poll the visible text length until it stops growing (or
    max_wait_ms elapses) so async content actually lands before we
    extract. Callers can also pass an explicit `wait_for_selector` for
    pages where the interesting content only mounts after a known
    element appears.
    """
    browser = _get_browser()
    if browser is None:
        return None

    page = None
    try:
        page = browser.new_page()
        page.route("**/*", _block_heavy_assets)

        resp = page.goto(url, timeout=RENDER_TIMEOUT_MS, wait_until="domcontentloaded")
        if resp is not None and not resp.ok:
            logger.warning("Playwright got HTTP %s for %s", resp.status, url)
            return None

        # Explicit selector wait (opt-in) — the interesting content only
        # mounts after this element appears.
        if wait_for_selector:
            with contextlib.suppress(Exception):
                page.wait_for_selector(wait_for_selector, timeout=RENDER_TIMEOUT_MS)

        # Let client-side routing/hydration settle. networkidle can hang
        # forever on analytics sockets — treat its timeout as non-fatal.
        with contextlib.suppress(Exception):
            page.wait_for_load_state("networkidle", timeout=RENDER_SETTLE_MS)

        # Adaptive wait: poll the visible text until it stops growing, so
        # heavy SPAs get time to fetch their data. Fast pages break out
        # after RENDER_STABLE_READS equal readings (~1s overhead).
        if max_wait_ms is None:
            max_wait_ms = RENDER_MAX_WAIT_MS
        deadline = time.monotonic() + max_wait_ms / 1000.0
        last_len = -1
        stable = 0
        while time.monotonic() < deadline:
            try:
                cur_len = len(page.locator("body").inner_text().strip())
            except Exception:
                break
            if cur_len == last_len:
                stable += 1
                if stable >= RENDER_STABLE_READS and cur_len >= MIN_RENDERED_TEXT:
                    break  # settled with meaningful content
            else:
                stable = 0
            last_len = cur_len
            time.sleep(RENDER_POLL_INTERVAL_MS / 1000.0)

        title = page.title() or None
        visible = page.locator("body").inner_text().strip()
        t_title, text = _extract_trafilatura(page.content())

        # Prefer trafilatura's clean article extraction — unless it
        # cherry-picked a tiny slice of a far richer rendered page (e.g.
        # one model card out of a list, like openrouter.ai/models).
        if text is not None:
            dwarfed = (
                len(visible) >= MIN_RENDERED_TEXT * 3
                and len(visible) >= len(text) * 2
            )
            if not dwarfed:
                return _result(
                    url, title=t_title or title, content=text, method="playwright"
                )

        # Trafilatura found nothing (or too little) — use the rendered
        # DOM's visible text: real rendered content, not raw-HTML garbage.
        if len(visible) >= MIN_RENDERED_TEXT:
            return _result(url, title=title, content=visible, method="playwright_dom")

        if text is not None:
            return _result(
                url, title=t_title or title, content=text, method="playwright"
            )
        return None  # still just a JS shell
    except Exception as exc:
        logger.warning("Playwright rendering failed for %s: %s", url, exc)
        return None
    finally:
        if page is not None:
            with contextlib.suppress(Exception):
                page.close()


# ── Jina Reader fallback ─────────────────────────────────────────────


def _fetch_jina(url: str) -> dict | None:
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


# ── Public API ───────────────────────────────────────────────────────


def fetch_url(
    url: str,
    *,
    wait_for_selector: str | None = None,
    max_wait_ms: int | None = None,
) -> dict:
    """Fetch and extract content from a URL.

    Tries: YouTube transcript (auto-detected) → trafilatura
    → Playwright render → Jina Reader.

    For JS-heavy SPAs, `wait_for_selector` (e.g. ".model-card") waits for
    a specific element to mount before extracting, and `max_wait_ms`
    caps how long we poll for async content to settle (default 15s).

    Returns dict with keys: url, title, content, method, error.
    """
    # URL validation — fail fast
    err = _validate_url(url)
    if err:
        return _result(url, error=err)

    # YouTube detection
    if _extract_video_id(url):
        return _fetch_youtube(url)

    # Trafilatura → Playwright → Jina fallback chain
    result = _fetch_trafilatura(url)
    if result is None:
        result = _fetch_playwright(
            url,
            wait_for_selector=wait_for_selector,
            max_wait_ms=max_wait_ms,
        )
    if result is None:
        result = _fetch_jina(url)
    if result is None:
        result = _result(url, error="all extraction methods failed")
    return result


def is_url(s: str) -> bool:
    """Quick check if a string looks like an http(s) URL."""
    return s.startswith(("http://", "https://"))
