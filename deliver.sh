#!/usr/bin/env bash
# Deliver the runs-visibility feature to a clone via docker cp (no host-path access needed).
#
# LESSONS BAKED IN (2026-08-15 bite):
#   - NEVER graft CSS by line numbers — clone files have different layouts
#     (a sed -n '50,91p' slice started mid-media-query on some clones and
#     crashed Tailwind with "Missing opening {"). The glow block is embedded
#     below, whole, via heredoc instead.
#   - NEVER `git pull` inside clones — their personalizations (colors, vite
#     aliases, placeholders) live in tracked files. Targeted copies only.
#   - Check for active runs before restarting a backend (restarting kills
#     the run — the exact thing this feature exists to prevent!).
set -euo pipefail
NAME="$1"
UI_PORT="${2:-}"
BC="${NAME}-backend-1"
FC="${NAME}-frontend-1"
REPO="/workspace"   # inside each container, the repo root is /workspace

echo "[deliver] $NAME"

# 0) Preflight: warn loudly if the clone has an active run
ACTIVE=$(docker exec "$BC" python -c "
import sqlite3
db = sqlite3.connect('/workspace/backend/agno.db')
print(sum(1 for (b,) in db.execute('SELECT runs FROM agno_sessions') if b and 'RUNNING' in b))
" 2>/dev/null || echo "?")
if [ "$ACTIVE" != "0" ] && [ "$ACTIVE" != "?" ]; then
  echo "[deliver] WARNING: $NAME may have ~$ACTIVE active run(s) — a backend restart would kill them!"
  read -r -p "Continue anyway? [y/N] " yn </dev/tty
  [ "$yn" = "y" ] || exit 1
fi

# 1) Backend: copy wholesale (verified: differs only by the feature)
docker cp backend/app/routers/runs.py "$BC:$REPO/backend/app/routers/runs.py"
docker cp backend/app/main.py        "$BC:$REPO/backend/app/main.py"

# 2) Frontend: copy files that differ only by the feature
for f in api.ts session.ts; do docker cp "frontend/src/lib/$f" "$BC:$REPO/frontend/src/lib/$f"; done
for f in sse.ts useAgentStream.ts useActiveRuns.ts; do docker cp "frontend/src/hooks/$f" "$BC:$REPO/frontend/src/hooks/$f"; done
docker cp frontend/src/App.tsx                  "$BC:$REPO/frontend/src/App.tsx"
docker cp frontend/src/components/BottomBar.tsx "$BC:$REPO/frontend/src/components/BottomBar.tsx"

# 3) vite.config.ts: add /runs proxy to THEIR config (preserving their aliases)
if ! docker exec "$BC" grep -q '"/runs"' "$REPO/frontend/vite.config.ts"; then
  docker exec "$BC" sed -i 's#"/sessions": { target: "http://backend:8000" }#"/runs": { target: "http://backend:8000" },\n      "/sessions": { target: "http://backend:8000" }#' "$REPO/frontend/vite.config.ts"
fi

# 4) app.css: append the glow animation block — EMBEDDED WHOLE (heredoc),
#    never sliced by line numbers. Their colors stay theirs: the block only
#    uses var(--glow-color)/currentColor, so no color substitution needed.
if ! docker exec "$BC" grep -q 'glow-pulse' "$REPO/frontend/src/app.css"; then
  cat <<'GLOW' | docker cp - "$BC:$REPO/glow.css"

@keyframes glow-pulse {
  0%,
  100% {
    opacity: 0.2;
    transform: scale(1);
  }
  50% {
    opacity: 0.8;
    transform: scale(1.5);
  }
}

@keyframes dot-breathe {
  0%,
  100% {
    box-shadow: 0 0 1px 1px color-mix(in srgb, var(--glow-color) 45%, transparent);
  }
  50% {
    box-shadow:
      0 0 3px 1px color-mix(in srgb, var(--glow-color) 60%, transparent),
      0 0 8px 2px color-mix(in srgb, var(--glow-color) 25%, transparent);
  }
}

/* Pulsing status dot: expanding halo ring + breathing glow on the core.
   The halo animates transform+opacity but never overlaps other content
   (overflow-safe, pointer-events-none parent), so no WebKit compositing
   issues with cards. box-shadow glow uses no transform at all. */
.animate-glow-pulse {
  animation: glow-pulse 1.8s ease-in-out infinite;
}

.animate-dot-glow {
  animation: dot-breathe 1.8s ease-in-out infinite;
}
GLOW
  docker exec "$BC" sh -c "cat $REPO/glow.css >> $REPO/frontend/src/app.css && rm $REPO/glow.css"
fi

# 5) Verify Tailwind can still compile the CSS (catches any graft damage)
if [ -n "$UI_PORT" ]; then
  sleep 2
  CSS_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 15 "http://100.102.77.20:$UI_PORT/src/app.css" || true)
  echo "[deliver] CSS check (UI_PORT=$UI_PORT): $CSS_CODE"
else
  echo "[deliver] (pass UI port as \$2 to verify CSS, e.g. ./deliver.sh nami 3100)"
fi

echo "[deliver] $NAME done — restart backends when safe: docker restart $BC"
