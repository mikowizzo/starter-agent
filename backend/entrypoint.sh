#!/bin/sh
set -e

# Sync installed packages with requirements.txt on every startup.
# Caps at 10s so network failures don't delay boot.
# uv skips already-installed packages, so this is instant when nothing changed.
timeout 10 uv pip install --system -r requirements.txt 2>/dev/null \
  || echo "[entrypoint] pip sync skipped (offline or already satisfied)"

# Drop to non-root user for the application process.
exec runuser -u appuser -- "$@"
