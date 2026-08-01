FROM python:3.12-slim

# Multi-arch support: BuildKit provides TARGETARCH (amd64/arm64).
# Map Go-style arch names to Docker's static binary naming.
ARG TARGETARCH
COPY --from=ghcr.io/astral-sh/uv:0.6.14 /uv /usr/local/bin/uv

RUN apt-get update && apt-get install -y --no-install-recommends \
      git ca-certificates curl ripgrep \
    && DOCKER_ARCH=$(echo "${TARGETARCH:-$(uname -m)}" | sed 's/amd64/x86_64/;s/arm64/aarch64/') \
    && curl -fsSL "https://download.docker.com/linux/static/stable/${DOCKER_ARCH}/docker-27.5.1.tgz" \
      | tar xz -C /tmp \
    && mv /tmp/docker/docker /usr/local/bin/docker \
    && rm -rf /tmp/docker \
    && mkdir -p /usr/local/lib/docker/cli-plugins \
    && curl -fsSL "https://github.com/docker/compose/releases/download/v2.29.2/docker-compose-linux-${DOCKER_ARCH}" \
      -o /usr/local/lib/docker/cli-plugins/docker-compose \
    && chmod +x /usr/local/lib/docker/cli-plugins/docker-compose \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user early so COPY --chown avoids a separate chown layer.
RUN groupadd -g 1000 appuser && useradd -m -u 1000 -g appuser appuser

WORKDIR /workspace/backend

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/workspace/backend

COPY --chown=appuser:appuser backend/requirements.txt ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --system -r requirements.txt

# Copy the full codebase — both backend and frontend — so the agent can
# read/edit frontend files even when bind mounts are stripped (DinD clones).
COPY --chown=appuser:appuser backend/ ./
COPY --chown=appuser:appuser frontend/ /workspace/frontend/

# Entrypoint syncs installed packages with requirements.txt before booting.
# This makes `docker restart` safe even when new packages were added to
# requirements.txt since the last image build.
COPY backend/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Container starts as root so the entrypoint can run uv pip install --system,
# then drops to appuser for the application process.
ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-graceful-shutdown", "30"]
