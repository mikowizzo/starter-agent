FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.6.14 /uv /bin/uv

RUN apt-get update && apt-get install -y --no-install-recommends \
      git ca-certificates ripgrep \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace/backend

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/workspace/backend

COPY requirements.txt ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --system -r requirements.txt

RUN groupadd -g 1000 appuser && \
    useradd -m -u 1000 -g appuser appuser && \
    chown -R appuser:appuser /workspace
USER appuser

# NOTE: no --reload. The file watcher re-execs the whole process on every backend
# edit, which kills in-flight background streaming runs. Restart manually to pick
# up code changes: `docker compose restart backend`.
# --timeout-graceful-shutdown 30 >= agno's 30s cancel-persist drain so a manual
# restart lets in-flight DB writes finish cleanly.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--loop", "asyncio", "--timeout-graceful-shutdown", "30"]
