"""Per-clone personality loading.

Resolution order:
  1. AGENT_PERSONALITY_FILE env var — absolute path to a personality file
     (use this to mount a file from OUTSIDE the repo in Docker, so it's
     completely decoupled from the working tree).
  2. <base_dir>/personality.local.md — gitignored file in the repo root.
  3. Built-in default (Chopper) — always works, even on a pristine checkout.

A personality file is plain markdown/text with optional YAML frontmatter:

    ---
    accent_color: "#ff8c00"
    ---
    Respond in the tone of Nami from One Piece: bold, sharp-tongued, ...

The frontmatter carries per-clone UI settings (currently just accent_color).
The body (everything after frontmatter) becomes one personality instruction.
This means updating a clone's personality or colours never requires editing
tracked code, so `git pull` can never clobber it.
"""

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

LOCAL_PERSONALITY_FILENAME = "personality.local.md"
DEFAULT_ACCENT_COLOR = "#ff6b9d"

DEFAULT_PERSONALITY = (
    "Respond in the tone of Tony Tony Chopper from One Piece: cute, eager, "
    "and a little shy, but fiercely proud of being the Straw Hats' doctor. "
    "Get flustered when complimented ('I'm not going to be flattered by your "
    "compliments!'), insist 'I'm a reindeer, not a raccoon dog!' when "
    "mistaken for one, and geek out about medicine and healing."
)


def _parse_personality_file(text: str) -> tuple[str, str | None]:
    """Split frontmatter from body.

    Returns (personality_text, accent_color_or_None).
    Frontmatter is optional. If present, it must be YAML-ish ``key: value``
    lines delimited by ``---`` on their own line. We only extract
    ``accent_color`` — everything else is ignored.
    """
    accent_color: str | None = None
    body = text.strip()

    # Optional YAML frontmatter: ---\n key: val \n---\n rest
    if body.startswith("---"):
        parts = body.split("---", 2)
        if len(parts) >= 3:
            frontmatter = parts[1].strip()
            body = parts[2].strip()
            for line in frontmatter.splitlines():
                line = line.strip()
                if line.lower().startswith("accent_color:"):
                    accent_color = line.split(":", 1)[1].strip().strip('"').strip("'")
                    break

    return body, accent_color


def load_personality(base_dir: Path) -> str:
    """Return the personality instruction for this agent instance."""

    raw, _ = _load_raw(base_dir)
    return raw


def _load_raw(base_dir: Path) -> tuple[str, str | None]:
    """Return (personality_text, accent_color) for this agent instance."""

    # 1. Explicit env var (e.g. a Docker bind-mount outside the repo)
    env_path = os.environ.get("AGENT_PERSONALITY_FILE")
    if env_path:
        p = Path(env_path)
        if p.is_file():
            logger.info("Loading personality from AGENT_PERSONALITY_FILE=%s", p)
            text = p.read_text(encoding="utf-8")
            personality, accent = _parse_personality_file(text)
            return personality, accent
        logger.warning(
            "AGENT_PERSONALITY_FILE=%s does not exist; falling back", env_path
        )

    # 2. Gitignored local file in the repo root
    local = base_dir / LOCAL_PERSONALITY_FILENAME
    if local.is_file():
        logger.info("Loading personality from %s", local)
        text = local.read_text(encoding="utf-8")
        personality, accent = _parse_personality_file(text)
        return personality, accent

    # 3. Default
    logger.info("No personality file found; using default personality")
    return DEFAULT_PERSONALITY, None


# ── Accessors ─────────────────────────────────────────────────────────

# Cache so we only read the file once per process.
_cache: tuple[str, str | None] | None = None


def get_personality(base_dir: Path) -> str:
    global _cache
    if _cache is None:
        _cache = _load_raw(base_dir)
    return _cache[0]


def get_accent_color(base_dir: Path) -> str:
    global _cache
    if _cache is None:
        _cache = _load_raw(base_dir)
    return _cache[1] or DEFAULT_ACCENT_COLOR
