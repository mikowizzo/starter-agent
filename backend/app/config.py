"""Application configuration — constants, paths, and env validation."""

import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent.parent

# Database
DB_FILE = "agno.db"
DB_PATH = str(BASE_DIR / DB_FILE)

# ── Scheduler ─────────────────────────────────────────────────────────
SCHEDULER_BASE_URL = os.environ.get("SCHEDULER_BASE_URL", "http://localhost:8000")
SCHEDULER_POLL_INTERVAL = 15


def validate_env() -> None:
    """Exit early if required environment variables are missing."""
    if not os.environ.get("OPENCODE_API_KEY"):
        sys.exit("OPENCODE_API_KEY is required")

    # Loudly log the scheduler base URL so misconfiguration is visible at startup.
    # In Docker Compose, localhost from the scheduler container != the API container.
    logger.warning("Scheduler base URL: %s", SCHEDULER_BASE_URL)
