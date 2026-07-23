"""Application configuration — constants, paths, and env validation."""

import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent

# Database
DB_FILE = "agno.db"
DB_PATH = str(BASE_DIR / DB_FILE)

def validate_env() -> None:
    """Exit early if required environment variables are missing."""
    if not os.environ.get("OPENCODE_API_KEY"):
        sys.exit("OPENCODE_API_KEY is required")
