#!/bin/sh
set -e

# Only sync packages when requirements.txt has actually changed.
# Compares a hash of requirements.txt against the last successful sync.
NEW_HASH=$(sha256sum requirements.txt | cut -d' ' -f1)
OLD_HASH=$(cat /tmp/.req-hash 2>/dev/null || echo "")

if [ "$NEW_HASH" != "$OLD_HASH" ]; then
    echo "[entrypoint] requirements.txt changed, syncing packages..."
    if timeout 30 uv pip install --system -r requirements.txt; then
        echo "$NEW_HASH" > /tmp/.req-hash
    else
        echo "[entrypoint] pip sync failed (offline?), booting with existing packages"
    fi
else
    echo "[entrypoint] requirements.txt unchanged, skipping pip sync"
fi

# Drop to non-root user for the application process.
exec runuser -u appuser -- "$@"
