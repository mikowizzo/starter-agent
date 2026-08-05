---
name: scout
description: "Scout — multi-engine web search and URL scraping. Use for 'scout X', 'look up X', 'research X', or any web search + content extraction."
---

# Scout — Research Engine (Miko Only)

Two scripts: `scout.py` (multi-engine search across Brave, Tavily, Exa) and `scrape.py` (single-URL content extraction). For stock fundamentals, use the `market-data` skill. For prediction market data, use the `polymarket` skill.

**Owner: Miko.**

---

## Research Loop

1. **Search** — Fire `scout.py search` with the topic. Search only; no scraping yet.
   *Done when:* results JSON returned.
2. **Rank** — Scan ALL results before scraping any. Sort by information density (long-form, transcripts, research reports over short news), diversity of viewpoint, and authority (engine overlap = signal).
   *Done when:* ordered candidate list exists.
3. **Scrape one** — `scrape.py` the top-ranked URL.
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
    'search', 'QUERY',
    '--engine', 'all',
    '--count', '20',
    '--time-range', 'month'
])
```

**Arguments:**

| Arg | Required | Default | Description |
|-----|----------|---------|-------------|
| `search` | Yes | — | Subcommand |
| `query` | Yes | — | Search query / topic |
| `--engine` | No | `all` | `all`, `brave`, `tavily`, `exa` |
| `--count` | No | `20` | Per-engine result cap |
| `--time-range` | No | None | `day`, `week`, `month`, `year` — passed to engines that support it |

**Output:** JSON to stdout — `results` array (`url`, `title`, `snippet`, `domain`, `engines`) and `errors` dict for failed engines. Sorted by engine count descending, then alphabetically by title. Engines fail open: partial results returned with errors dict; exits 1 only if all engines fail.

---

## scrape.py — Single-URL Content Extraction

Extract content from a single URL. Tries trafilatura (primary) → Jina Reader API (fallback) → YouTube transcript (auto-detected). No domain blocklist, no batch scraping, no session state.

**Usage:**
```
get_skill_script('scout', 'scrape.py', execute=True, args=[
    'https://example.com/article'
])
```

**Arguments:** Single positional URL. No flags.

**Output:** JSON to stdout — `url`, `title`, `content`, `method` (`trafilatura`/`jina`/`youtube`), `error`, `saved_to`. Always exits 0; check `content` and `error` for success.

**Auto-save:** Every successful scrape saves to `/tmp/miko-scrape/<slug>.txt` (override with `SCRAPE_SAVE_DIR`). Pass `saved_to` path to council via `--files`:
```
result = get_skill_script('scout', 'scrape.py', execute=True, args=['https://example.com'])
get_skill_script('council', 'council_run.py', execute=True, args=['PROMPT', '--files', result['saved_to']])
```

**YouTube:** URLs matching `youtube.com/watch`, `youtu.be/`, `youtube.com/shorts/` auto-route to transcript extraction with timestamps.
