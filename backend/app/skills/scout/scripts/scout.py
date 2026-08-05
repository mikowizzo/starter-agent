#!/usr/bin/env python3
"""Multi-engine search with dedup and engine provenance."""

import argparse
import gzip
import json
import logging
import os
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone as _tz
from typing import Callable

logger = logging.getLogger("scout")

# ── Config ───────────────────────────────────────────────────────────────────

BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
TAVILY_URL = "https://api.tavily.com/search"
EXA_URL = "https://api.exa.ai/search"
USER_AGENT = "Scout-Skill/2.0"
MAX_SNIPPET = 500
MAX_SEARCH_WORKERS = 3
MAX_RETRIES = 2
RETRY_BACKOFF = 1.0

# Tracking parameters to strip during URL normalisation
_TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "fbclid", "gclid", "gclsrc", "dclid", "msclkid",
    "mc_eid", "mc_cid", "_ga", "_gl", "_hsenc", "_hsmi",
    "hsCtaTracking", "vero_id", "oly_anon_id", "oly_enc_id",
    "wickedid", "twclid", "ttclid", "igshid", "si",
})


_BRAVE_TIME_MAP = {
    "day": "pd",
    "week": "pw",
    "month": "pm",
    "year": "py",
}

_TAVILY_DAYS_MAP = {
    "day": 1,
    "week": 7,
    "month": 30,
    "year": 365,
}


# ── Data classes ─────────────────────────────────────────────────────────────


@dataclass
class SearchResult:
    """A single search result with engine provenance."""
    title: str
    url: str
    snippet: str
    engine: str
    domain: str = ""

    def truncated_snippet(self) -> str:
        """Return snippet truncated to MAX_SNIPPET at word boundary."""
        if len(self.snippet) <= MAX_SNIPPET:
            return self.snippet
        cut = self.snippet[:MAX_SNIPPET]
        # Walk back to last space for clean break
        last_space = cut.rfind(" ")
        if last_space > MAX_SNIPPET // 2:
            cut = cut[:last_space]
        return cut + "..."


@dataclass
class LegResult:
    """Outcome of a single engine leg — success or failure."""
    engine: str
    results: list[SearchResult] = field(default_factory=list)
    error: str | None = None
    duration_ms: int = 0



# ── URL normalisation ────────────────────────────────────────────────────────


def _normalize_url(url: str) -> str:
    """Normalise URL for dedup: strip tracking params, trailing slash, fragment, lowercase domain."""
    if not url:
        return url
    parsed = urllib.parse.urlparse(url)

    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()

    path = parsed.path.rstrip("/")
    if not path:
        path = "/"

    params = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    clean_params = [(k, v) for k, v in params if k.lower() not in _TRACKING_PARAMS]
    query = urllib.parse.urlencode(clean_params) if clean_params else ""

    return urllib.parse.urlunparse((scheme, netloc, path, parsed.params, query, ""))


def _extract_domain(url: str) -> str:
    """Extract hostname from URL."""
    try:
        return urllib.parse.urlparse(url).hostname or ""
    except Exception:
        return ""


# ── HTTP helpers ─────────────────────────────────────────────────────────────

# Shared opener for connection reuse
_opener = urllib.request.build_opener()


def _is_retryable_status(code: int) -> bool:
    """Check if an HTTP status code is worth retrying."""
    return code in (429, 500, 502, 503, 504)


def _read_response(resp) -> bytes:
    """Read HTTP response body, decompressing gzip if needed."""
    raw = resp.read()
    if resp.headers.get("Content-Encoding", "").lower() == "gzip":
        return gzip.decompress(raw)
    return raw


def _http_get(url: str, headers: dict, timeout: int = 15) -> dict:
    """HTTP GET returning parsed JSON."""
    req = urllib.request.Request(url, headers=headers)
    with _opener.open(req, timeout=timeout) as resp:
        return json.loads(_read_response(resp).decode("utf-8"))


def _http_post(url: str, body: bytes, headers: dict, timeout: int = 15) -> dict:
    """HTTP POST with exponential backoff retry on retryable errors."""
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    for attempt in range(MAX_RETRIES + 1):
        try:
            with _opener.open(req, timeout=timeout) as resp:
                return json.loads(_read_response(resp).decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError) as e:
            code = getattr(e, 'code', None)
            if code and not _is_retryable_status(code):
                raise
            if attempt >= MAX_RETRIES:
                raise
            backoff = RETRY_BACKOFF * (2 ** attempt) + (time.time() % 1.0)
            logger.warning("Retryable error from %s, retrying in %.1fs (%d/%d)",
                           url, backoff, attempt + 1, MAX_RETRIES + 1)
            time.sleep(backoff)
            continue
    # Should never reach here
    raise RuntimeError(f"All retries exhausted for POST {url}")


# ── Engine functions ─────────────────────────────────────────────────────────


def _parse_results(items, *, title_key, url_key, snippet_key, engine) -> list[SearchResult]:
    """Parse raw API items into SearchResult objects."""
    results = []
    for item in items:
        sr = SearchResult(
            title=item.get(title_key, ""),
            url=item.get(url_key, ""),
            snippet=item.get(snippet_key, "") or "",
            engine=engine,
            domain=_extract_domain(item.get(url_key, "")),
        )
        sr.snippet = sr.truncated_snippet()
        results.append(sr)
    return results


def _search_brave(query: str, count: int, time_range: str | None) -> list[SearchResult]:
    """Query Brave Search and return results."""
    api_key = os.environ.get("BRAVE_API_KEY", "")
    if not api_key:
        raise RuntimeError("BRAVE_API_KEY not set")

    params = {
        "q": query,
        "count": count,
    }
    if time_range and time_range in _BRAVE_TIME_MAP:
        params["freshness"] = _BRAVE_TIME_MAP[time_range]

    url = f"{BRAVE_URL}?{urllib.parse.urlencode(params)}"
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": api_key,
        "User-Agent": USER_AGENT,
    }
    data = _http_get(url, headers)

    return _parse_results(
        data.get("web", {}).get("results", []),
        title_key="title", url_key="url", snippet_key="description",
        engine="brave",
    )


def _search_tavily(query: str, count: int, time_range: str | None) -> list[SearchResult]:
    """Query Tavily and return results."""
    api_key = os.environ.get("TAVILY_API_KEY", "")
    if not api_key:
        raise RuntimeError("TAVILY_API_KEY not set")

    body = {
        "query": query,
        "max_results": count,
        "search_depth": "basic",
        "topic": "general",
        "include_answer": False,
    }
    if time_range and time_range in _TAVILY_DAYS_MAP:
        body["days"] = _TAVILY_DAYS_MAP[time_range]

    body_bytes = json.dumps(body).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "User-Agent": USER_AGENT,
    }
    data = _http_post(TAVILY_URL, body_bytes, headers)

    return _parse_results(
        data.get("results", []),
        title_key="title", url_key="url", snippet_key="content",
        engine="tavily",
    )


def _search_exa(query: str, count: int, time_range: str | None) -> list[SearchResult]:
    """Query Exa and return results."""
    api_key = os.environ.get("EXA_API_KEY", "")
    if not api_key:
        raise RuntimeError("EXA_API_KEY not set")

    body = {
        "query": query,
        "num_results": count,
        "use_autoprompt": False,
        "type": "auto",
        "contents": {"text": True},
    }
    if time_range:
        days = _TAVILY_DAYS_MAP.get(time_range, 30)
        start_date = datetime.now(_tz.utc) - timedelta(days=days)
        body["startPublishedDate"] = start_date.strftime("%Y-%m-%dT%H:%M:%SZ")

    body_bytes = json.dumps(body).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "User-Agent": USER_AGENT,
    }
    data = _http_post(EXA_URL, body_bytes, headers)

    return _parse_results(
        data.get("results", []),
        title_key="title", url_key="url", snippet_key="text",
        engine="exa",
    )


# ── Engine dispatch ──────────────────────────────────────────────────────────

# Engine name → search function
_ENGINE_LEGS: dict[str, Callable] = {
    "brave": _search_brave,
    "tavily": _search_tavily,
    "exa": _search_exa,
}


def _run_leg(name: str, fn: Callable, query: str, count: int,
             time_range: str | None) -> LegResult:
    """Run a single search leg, catching errors. Always returns a LegResult."""
    start = time.monotonic()
    try:
        results = fn(query, count, time_range)
        elapsed_ms = int((time.monotonic() - start) * 1000)
        logger.info("%s: %d results in %dms", name, len(results), elapsed_ms)
        return LegResult(engine=name, results=results, duration_ms=elapsed_ms)
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        logger.error("%s failed in %dms: %s", name, elapsed_ms, exc)
        return LegResult(engine=name, error=str(exc), duration_ms=elapsed_ms)


# ── Deduplication ────────────────────────────────────────────────────────────


def _dedup(all_results: list[SearchResult]) -> list[dict]:
    """Deduplicate by normalised URL, tracking engine provenance.

    Sort by engine count descending (multi-engine hits first), then alphabetically
    by title.
    """
    seen: dict[str, dict] = {}

    for r in all_results:
        norm = _normalize_url(r.url)
        if not norm:
            continue

        if norm not in seen:
            seen[norm] = {
                "title": r.title,
                "url": r.url,
                "snippet": r.snippet,
                "domain": r.domain,
                "engines": [r.engine] if r.engine else [],
            }
        else:
            existing = seen[norm]
            if r.engine and r.engine not in existing["engines"]:
                existing["engines"].append(r.engine)
            if not existing["title"] and r.title:
                existing["title"] = r.title
            if not existing["snippet"] and r.snippet:
                existing["snippet"] = r.snippet

    results = list(seen.values())
    for r in results:
        r["engines"] = sorted(r["engines"])
    results.sort(key=lambda r: (-len(r["engines"]), r["title"].lower()))

    return results


# ── CLI ──────────────────────────────────────────────────────────────────────


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(
        description="Scout: multi-engine search with dedup",
    )
    parser.add_argument("query", help="Search query")
    parser.add_argument(
        "--engine", default="all",
        choices=["all", "brave", "tavily", "exa"],
        help="Engine to use (default: all)",
    )
    parser.add_argument(
        "--count", type=int, default=20,
        help="Per-engine result cap (default: 20)",
    )
    parser.add_argument(
        "--time-range", default=None,
        choices=["day", "week", "month", "year"],
        help="Time range filter — passed through to engines that support it",
    )

    return parser.parse_args(argv)


def cmd_search(args: argparse.Namespace) -> None:
    """Run multi-engine search, dedup, print JSON."""
    count = max(1, min(args.count, 100))
    legs = list(_ENGINE_LEGS) if args.engine == "all" else [args.engine]

    leg_results: list[LegResult] = []
    with ThreadPoolExecutor(max_workers=MAX_SEARCH_WORKERS) as pool:
        futures = {
            pool.submit(_run_leg, name, fn, args.query, count, args.time_range): name
            for name, fn in _ENGINE_LEGS.items() if name in legs
        }
        for future in as_completed(futures):
            leg_results.append(future.result())

    all_results: list[SearchResult] = []
    errors: dict[str, str] = {}
    for lr in leg_results:
        if lr.error is None:
            all_results.extend(lr.results)
        else:
            errors[lr.engine] = lr.error

    if not all_results:
        logger.error("All engines failed")
        sys.exit(1)

    deduped = _dedup(all_results)

    output: dict = {"results": deduped}
    if errors:
        output["errors"] = errors

    print(json.dumps(output, indent=2, ensure_ascii=False))


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(name)s: %(message)s",
        stream=sys.stderr,
    )
    cmd_search(_parse_args())


if __name__ == "__main__":
    main()
