---
name: scout
description: "Scout — multi-engine web search and URL content extraction. Use for 'scout X', 'look up X', 'research X', or any web search."
---

# Scout — Research Engine

Two scripts: `scout.py` (multi-engine search across Brave, Tavily, Exa) and `scrape.py` (single-URL content extraction). For stock fundamentals, use the `market-data` skill. For prediction market data, use the `polymarket` skill.

**Note:** The `read` tool now natively fetches URLs — you can use `read("https://example.com/article")` or `read("https://youtube.com/watch?v=...")` directly instead of scrape.py. The read tool uses the same trafilatura → Jina Reader → YouTube transcript pipeline.

---

## Research Loop

1. **Search** — Fire `scout.py` with the topic. Search only; no scraping yet.
   *Done when:* results JSON returned.
2. **Rank** — Scan ALL results before scraping any. Sort by information density (long-form, transcripts, research reports over short news), diversity of viewpoint, and authority (engine overlap = signal).
   *Done when:* ordered candidate list exists.
3. **Scrape one** — `read()` the top-ranked URL (or use `scrape.py` if you need the `saved_to` file path for council `--files`).
   *Done when:* content extracted, or failure recorded.
4. **Read & judge** — Does it add unique, high-signal value beyond what you already hold? Keep → add to curated list. No → discard.
   *Done when:* keep/discard decision recorded.
5. **Repeat** — Next-ranked URL. Continue until stop threshold met.
   *Done when:* stop threshold met.

### When to Stop

- **Council prep:** ~6–10 dense articles or 4–5 video transcripts
- **Quick lookup:** 2–3 high-quality sources
- **Deep research:** until new sources repeat what you already know

---

## scout.py — Multi-Engine Search + Dedup

Fires Brave, Tavily, and Exa in parallel. Merges and deduplicates by normalised URL (strips tracking params, trailing slashes, fragments). Engine overlap is a signal proxy.

**Usage:**
```
get_skill_script('scout', 'scout.py', execute=True, args=[
    'QUERY',
    '--engine', 'all',
    '--count', '20',
    '--time-range', 'month'
])
```

**Arguments:**

| Arg | Required | Default | Description |
|-----|----------|---------|-------------|
| `query` | Yes | — | Search query / topic — single positional; pass the whole query as one arg element (no quotes needed) |
| `--engine` | No | `all` | `all`, `brave`, `tavily`, `exa` |
| `--count` | No | `20` | Per-engine result cap |
| `--time-range` | No | None | `day`, `week`, `month`, `year` — passed to engines that support it |

**Output:** JSON to stdout — `results` array (`url`, `title`, `snippet`, `domain`, `engines`) and `errors` dict for failed engines. Sorted by engine count descending, then alphabetically by title. Engines fail open: partial results returned with errors dict; exits 1 only if all engines fail.

---

## scrape.py — Single-URL Content Extraction (legacy)

> **Prefer the `read` tool** for URL fetching: `read("https://example.com")`. It uses the same extraction pipeline (trafilatura → Jina Reader → YouTube transcript) and integrates seamlessly with offset/limit pagination.

`scrape.py` remains for when you need the auto-saved file path (`saved_to`) to pass to council via `--files`.

**Usage:**
```
get_skill_script('scout', 'scrape.py', execute=True, args=[
    'https://example.com/article'
])
```

**Arguments:** Single positional URL. No flags.

**Output:** JSON to stdout — `url`, `title`, `content`, `method` (`trafilatura`/`playwright`/`playwright_dom`/`jina`/`youtube`), `error`, `saved_to`. Exits 0 on success, 1 on failure.

**Auto-save:** Every successful scrape saves to `/tmp/scout-scrape/<slug>.txt`. Pass `saved_to` path to council via `--files`:
```
result = get_skill_script('scout', 'scrape.py', execute=True, args=['https://example.com'])
get_skill_script('council', 'council_run.py', execute=True, args=['PROMPT', '--files', result['saved_to']])
```

**YouTube:** URLs matching `youtube.com/watch`, `youtu.be/`, `youtube.com/shorts/` auto-route to transcript extraction with timestamps.
