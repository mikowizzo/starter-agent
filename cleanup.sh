# quick check
docker ps -a --filter name=starter2 --format "{{.Names}} {{.Ports}}"
docker rm -f starter2-frontend-1 starter2-backend-1 2>/dev/null
docker network rm starter2_default 2>/dev/null
# what's on 3100?
docker ps --filter publish=3100 --format "table {{.Names}}\t{{.Ports}}"
