
## RENAME — 2026-08-16: gi2 → Poneglyph

The skill was renamed by the analyst and the archaeologist, together.
**Poneglyph** (from One Piece): stone steles carved with true history —
indestructible, scattered, and meaningful only when connected. The name
fits the tool's soul: an append-only hash-chained journal no one can
rewrite, whose truth emerges from linking the fragments.

- Folder: `backend/app/skills/poneglyph` (was `gi2`)
- Binary: `scripts/poneglyph.py` (was `gi2.py`)
- Extractor: `scripts/poneglyph_extract.py` (was `gi2_extract.py`) +
  its golden suite `test_poneglyph_extract.py` — all 20 tests pass
  post-rename; EXTRACTOR_VERSION unchanged (`html-visible-v1` — the
  version stamp is bytes, not filename)
- SKILL.md frontmatter: `name: poneglyph`
- Case `ai` traveled with the folder; chain head unchanged
  (`ac07a90e…`) — identity changed, history survived. Path-independence
  proven by the rename itself.
- **Frozen identifiers NOT renamed** (on-disk format / operating
  contract, renaming would break live cases mid-flight):
  - `.gi2.lock` lockfile name
  - `GI2_*` environment variables (ALLOW_FILE_URI, ALLOW_PRIVATE_FETCH,
    IGNORE_CLAIM_ID_CONFLICT)
  - `gi2/1.0` fetch User-Agent string
  These may be aliased in a future version (read `GI2_*` or
  `PONEGLYPH_*`), never removed.
- The Saturday `openrouter-weekly-watch` schedule was recreated with
  updated prose pointing at the new skill name.
- Deferred to v2 (banked 2026-08-16): semantic-search embeddings sidecar
  over artifact prose (design sketched in session notes: SQLite sidecar,
  embed-v1 versioning, hybrid FTS+vector ranking, trigger = FTS5
  measurably failing conceptual queries at ~50MB+ corpus).
