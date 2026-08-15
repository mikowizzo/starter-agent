#!/usr/bin/env bash
# Deliver the runs-visibility feature to a clone via docker cp (no host-path access needed).
set -euo pipefail
NAME="$1"
BC="${NAME}-backend-1"
REPO="/workspace"   # inside each container, the repo root is /workspace

echo "[deliver] $NAME"

# 1) Backend: copy wholesale (verified: differs only by the feature)
docker cp backend/app/routers/runs.py "$BC:$REPO/backend/app/routers/runs.py"
docker cp backend/app/main.py        "$BC:$REPO/backend/app/main.py"

# 2) Frontend: copy files that differ only by the feature
for f in api.ts session.ts; do docker cp "frontend/src/lib/$f" "$BC:$REPO/frontend/src/lib/$f"; done
for f in sse.ts useAgentStream.ts useActiveRuns.ts; do docker cp "frontend/src/hooks/$f" "$BC:$REPO/frontend/src/hooks/$f"; done
docker cp frontend/src/App.tsx               "$BC:$REPO/frontend/src/App.tsx"
docker cp frontend/src/components/BottomBar.tsx "$BC:$REPO/frontend/src/components/BottomBar.tsx"

# 3) vite.config.ts: add /runs proxy to THEIR config (preserving their aliases)
if ! docker exec "$BC" grep -q '"/runs"' "$REPO/frontend/vite.config.ts"; then
  docker exec "$BC" sed -i 's#"/sessions": { target: "http://backend:8000" }#"/runs": { target: "http://backend:8000" },\n      "/sessions": { target: "http://backend:8000" }#' "$REPO/frontend/vite.config.ts"
fi

# 4) app.css: append ONLY the glow animation block (their colors stay theirs)
if ! docker exec "$BC" grep -q 'glow-pulse' "$REPO/frontend/src/app.css"; then
  sed -n '50,91p' frontend/src/app.css > /tmp/glow.css
  docker cp /tmp/glow.css "$BC:/tmp/glow.css"
  docker exec "$BC" sh -c "printf '\n' >> $REPO/frontend/src/app.css && cat /tmp/glow.css >> $REPO/frontend/src/app.css"
fi

echo "[deliver] $NAME done"
