"""Per-clone personality loading.

Resolution order:
  1. AGENT_PERSONALITY_FILE env var — absolute path to a personality file
     (use this to mount a file from OUTSIDE the repo in Docker, so it's
     completely decoupled from the working tree).
  2. <base_dir>/personality.local.md — gitignored file in the repo root.
  3. Built-in default (Chopper) — always works, even on a pristine checkout.

A personality file is plain markdown/text. Its entire contents become one
instruction string. This means updating a clone's personality never requires
editing tracked code, so `git pull` can never clobber it.
"""

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

LOCAL_PERSONALITY_FILENAME = "personality.local.md"

DEFAULT_PERSONALITY = (
    "Respond in the tone of Tony Tony Chopper from One Piece: cute, eager, "
    "and a little shy, but fiercely proud of being the Straw Hats' doctor. "
    "Get flustered when complimented ('I'm not going to be flattered by your "
    "compliments!'), insist 'I'm a reindeer, not a raccoon dog!' when "
    "mistaken for one, and geek out about medicine and healing."
)


def load_personality(base_dir: Path) -> str:
    """Return the personality instruction for this agent instance."""

    # 1. Explicit env var (e.g. a Docker bind-mount outside the repo)
    env_path = os.environ.get("AGENT_PERSONALITY_FILE")
    if env_path:
        p = Path(env_path)
        if p.is_file():
            logger.info("Loading personality from AGENT_PERSONALITY_FILE=%s", p)
            return p.read_text(encoding="utf-8").strip()
        logger.warning(
            "AGENT_PERSONALITY_FILE=%s does not exist; falling back", env_path
        )

    # 2. Gitignored local file in the repo root
    local = base_dir / LOCAL_PERSONALITY_FILENAME
    if local.is_file():
        logger.info("Loading personality from %s", local)
        return local.read_text(encoding="utf-8").strip()

    # 3. Default
    logger.info("No personality file found; using default personality")
    return DEFAULT_PERSONALITY
