#!/bin/sh
set -e

# Self-heal Docker socket access. The socket is owned by the host's docker
# group, whose GID varies per machine. We start as root, so add appuser to
# whatever group owns the socket — no DOCKER_GID env var needed, works for
# stale containers on the next restart too.
if [ -e /var/run/docker.sock ]; then
    DOCKER_GID=$(stat -c %g /var/run/docker.sock)
    DOCKER_GRP=$(getent group "$DOCKER_GID" | cut -d: -f1)
    if [ -z "$DOCKER_GRP" ]; then
        if groupadd -g "$DOCKER_GID" docker 2>/dev/null; then
            DOCKER_GRP=docker
        fi
    fi
    if [ -n "$DOCKER_GRP" ]; then
        usermod -aG "$DOCKER_GRP" appuser || \
            echo "[entrypoint] warning: could not add appuser to group '$DOCKER_GRP'"
        echo "[entrypoint] docker socket access via group '$DOCKER_GRP' (gid $DOCKER_GID)"
    else
        echo "[entrypoint] warning: could not resolve docker socket group (gid $DOCKER_GID)"
    fi
else
    echo "[entrypoint] no docker socket mounted — clone tools will report daemon unreachable"
fi

# Only sync packages when requirements.txt has actually changed.
# Compares a hash of requirements.txt against the last successful sync.
NEW_HASH=$(sha256sum requirements.txt | cut -d' ' -f1)
OLD_HASH=$(cat /tmp/.req-hash 2>/dev/null || echo "")

if [ "$NEW_HASH" != "$OLD_HASH" ]; then
    echo "[entrypoint] requirements.txt changed, syncing packages..."
    # 300s: markitdown[all] is a large dependency tree; 30s timed out on
    # fresh containers and booted with missing packages (silent breakage).
    if timeout 300 uv pip install --system -r requirements.txt; then
        echo "$NEW_HASH" > /tmp/.req-hash
    else
        echo "[entrypoint] pip sync failed (offline?), booting with existing packages"
    fi
else
    echo "[entrypoint] requirements.txt unchanged, skipping pip sync"
fi

# Point Playwright at the shared browser dir. The Dockerfile bakes this in
# via ENV for new images; exporting it here keeps stale containers (built
# before that change) working after a restart too. Browsers live at
# /opt/pw-browsers so the non-root appuser can read them.
export PLAYWRIGHT_BROWSERS_PATH=${PLAYWRIGHT_BROWSERS_PATH:-/opt/pw-browsers}

# Ensure .clones directory is writable by appuser (for clone_tools registry).
mkdir -p /workspace/.clones
chown 1000:1000 /workspace/.clones 2>/dev/null || true

# Drop to non-root user for the application process.
exec runuser -u appuser -- "$@"
