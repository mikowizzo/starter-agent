FROM python:3.12-slim

# Host docker group GID — the socket is mounted root:docker mode 660, so
# appuser must be in a group whose GID matches the host docker group.
# Build with: --build-arg DOCKER_GID=$(getent group docker | cut -d: -f3)
# Defaults to 124 (common on Debian/Ubuntu). Override for your host.
ARG DOCKER_GID=124

COPY --from=ghcr.io/astral-sh/uv:0.6.14 /uv /bin/uv

RUN apt-get update && apt-get install -y --no-install-recommends \
      git ca-certificates curl ripgrep \
    && DOCKER_ARCH=$(uname -m | sed 's/x86_64/x86_64/;s/aarch64/aarch64/') \
    && curl -fsSL "https://download.docker.com/linux/static/stable/${DOCKER_ARCH}/docker-27.5.1.tgz" \
      | tar xz -C /tmp \
    && mv /tmp/docker/docker /usr/local/bin/docker \
    && rm -rf /tmp/docker \
    && mkdir -p /usr/local/lib/docker/cli-plugins \
    && curl -fsSL "https://github.com/docker/compose/releases/download/v2.29.2/docker-compose-linux-${DOCKER_ARCH}" \
      -o /usr/local/lib/docker/cli-plugins/docker-compose \
    && chmod +x /usr/local/lib/docker/cli-plugins/docker-compose \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace/backend

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/workspace/backend

COPY backend/requirements.txt ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --system -r requirements.txt

# Copy the full codebase — both backend and frontend — so the agent can
# read/edit frontend files even when bind mounts are stripped (DinD clones).
COPY backend/ ./
COPY frontend/ /workspace/frontend/

RUN groupadd -g 1000 appuser && \
    useradd -m -u 1000 -g appuser appuser && \
    groupadd -g ${DOCKER_GID} docker && \
    usermod -aG docker appuser && \
    chown -R appuser:appuser /workspace
USER appuser

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--loop", "asyncio", "--timeout-graceful-shutdown", "30"]
