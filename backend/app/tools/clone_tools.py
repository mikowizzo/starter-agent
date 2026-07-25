"""Self-cloning toolkit — let the agent spin up copies of itself.

Requires the Docker socket to be mounted and the ``docker`` CLI available
inside the container (see ``docker-compose.yml`` and ``Dockerfile``).
Each clone is a full docker-compose stack with unique ports, its own
database, and its own Docker socket — so clones can spawn clones recursively.
"""

import json
import asyncio
import os
import re
import shutil
import subprocess
from pathlib import Path

from agno.tools import Toolkit

_CLONES_DIR = Path("/workspace/.clones")
_REGISTRY = _CLONES_DIR / "registry.json"
_BASE_PORT_BACKEND = 8100
_BASE_PORT_FRONTEND = 3100

_EXCLUDE = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    ".clones", ".env", ".ruff_cache",
}

# Canonical source — clones always start from the public template, not from
# this agent's live working directory. Clones are experiments; they start fresh.
_CLONE_REPO = "https://github.com/mikowizzo/starter-agent.git"


class CloneTools(Toolkit):
    """Create, list, stop, start, and destroy clones of this agent."""

    def __init__(self) -> None:
        super().__init__(
            name="clone_tools",
            tools=[
                self.create_clone,
                self.list_clones,
                self.stop_clone,
                self.start_clone,
                self.destroy_clone,
            ],
        )

    @staticmethod
    def _load_registry() -> list[dict]:
        if _REGISTRY.exists():
            return json.loads(_REGISTRY.read_text())
        return []

    @staticmethod
    def _save_registry(clones: list[dict]) -> None:
        _CLONES_DIR.mkdir(parents=True, exist_ok=True)
        _REGISTRY.write_text(json.dumps(clones, indent=2))

    @staticmethod
    def _next_ports(registry: list[dict]) -> tuple[int, int]:
        if not registry:
            return _BASE_PORT_BACKEND, _BASE_PORT_FRONTEND
        max_b = max(c["ports"]["backend"] for c in registry)
        max_f = max(c["ports"]["frontend"] for c in registry)
        return max_b + 1, max_f + 1

    @staticmethod
    async def _compose_up(clone_dir: Path, name: str) -> subprocess.CompletedProcess:
        return await asyncio.to_thread(
            subprocess.run,
            ["docker", "compose", "-p", name, "up", "--build", "-d"],
            capture_output=True, text=True, timeout=300,
            cwd=str(clone_dir),
        )

    @staticmethod
    async def _docker_check() -> tuple[bool, str]:
        """Check docker access. Returns (ok, message).

        Distinguishes the three common failure modes so the agent can point
        at the actual cause instead of one generic "not available" message:

          * CLI not installed            -> install docker in the image
          * permission denied on socket  -> wrong DOCKER_GID at build time
          * daemon unreachable           -> host docker not running
        """
        try:
            r = await asyncio.to_thread(
                subprocess.run,
                ["docker", "info"], capture_output=True, text=True, timeout=10,
            )
        except FileNotFoundError:
            return False, (
                "Docker CLI is not installed in this container. "
                "Install it in the Dockerfile (see the static-binary step)."
            )
        except subprocess.TimeoutExpired:
            return False, "Docker daemon did not respond within 10s — is the host docker running?"

        if r.returncode == 0:
            return True, "ok"

        err = (r.stderr or r.stdout or "").lower()
        if "permission denied" in err:
            return False, (
                "Permission denied on the Docker socket. The container's docker "
                "group GID does not match the host's. Rebuild the image with "
                "--build-arg DOCKER_GID=$(getent group docker | cut -d: -f3)."
            )
        if "cannot connect" in err or "no such file" in err:
            return False, (
                "Cannot reach the Docker daemon. Check that /var/run/docker.sock "
                "is mounted in docker-compose.yml and the host docker service is up."
            )
        return False, f"Docker check failed (exit {r.returncode}): {r.stderr.strip()[:300]}"

    @staticmethod
    async def _docker_available() -> bool:
        """Backwards-compat shim. Prefer _docker_check() for actionable errors."""
        ok, _ = await CloneTools._docker_check()
        return ok

    @staticmethod
    async def _compose_cmd(name: str, *args: str) -> subprocess.CompletedProcess:
        clone_dir = _CLONES_DIR / name
        return await asyncio.to_thread(
            subprocess.run,
            ["docker", "compose", "-p", name, *args],
            capture_output=True, text=True, timeout=120,
            cwd=str(clone_dir),
        )

    async def create_clone(self, name: str) -> str:
        """Create a clone of this agent.

        Copies the current codebase, assigns unique ports, and starts the
        clone with docker compose up. The clone gets its own database
        and Docker socket, so it can spawn its own clones.

        Args:
            name: A short identifier for the clone (lowercase, no spaces).
        """
        name = name.strip().lower().replace(" ", "-")
        if not name:
            return "Error: Clone name cannot be empty."

        ok, msg = await self._docker_check()
        if not ok:
            return f"Error: {msg}"

        registry = self._load_registry()
        if any(c["name"] == name for c in registry):
            return f"Error: Clone '{name}' already exists."

        clone_dir = _CLONES_DIR / name
        if clone_dir.exists():
            return f"Error: Directory {clone_dir} already exists."

        port_b, port_f = self._next_ports(registry)
        _CLONES_DIR.mkdir(parents=True, exist_ok=True)

        # Clone from the canonical starter-agent repo so every clone starts
        # from a clean, unmodified template — not this agent's live working
        # directory. Clones are experiments; they should start fresh.
        def _git_clone():
            return subprocess.run(
                ["git", "clone", "--depth", "1", _CLONE_REPO, str(clone_dir)],
                capture_output=True, text=True, timeout=60,
            )

        git_result = await asyncio.to_thread(_git_clone)
        if git_result.returncode != 0:
            shutil.rmtree(clone_dir, ignore_errors=True)
            return f"Error: git clone failed: {git_result.stderr[-500:]}"

        # docker-compose.yml references .env — ensure it exists in the clone.
        # Inherit the same env vars as this container so clones have the same
        # API keys and configuration as the parent.
        env_dst = clone_dir / ".env"
        if not env_dst.exists():
            env_src = Path("/workspace/.env")
            if env_src.exists():
                shutil.copy2(env_src, env_dst)
            else:
                # No .env on disk — dump env vars from this process so the
                # clone inherits the same API keys and settings.
                _SYS_ENV = frozenset({
                    "PATH", "HOME", "HOSTNAME", "PWD", "SHLVL", "TERM",
                    "LANG", "LC_ALL", "PYTHON_VERSION", "PYTHON_PIP_VERSION",
                    "PYTHONUNBUFFERED", "PYTHONDONTWRITEBYTECODE", "GPG_KEY",
                    "_", "PYTHONPATH", "VIRTUAL_ENV", "CLONE_NAME",
                    "ENVIRONMENT", "FRONTEND_MODE",
                })
                lines_env = [
                    f"{k}={os.environ[k]}"
                    for k in sorted(os.environ)
                    if k not in _SYS_ENV
                ]
                env_dst.write_text("\n".join(lines_env) + "\n")

        compose_path = clone_dir / "docker-compose.yml"
        if not compose_path.exists():
            shutil.rmtree(clone_dir)
            return "Error: No docker-compose.yml found."

        compose = compose_path.read_text()
        compose = re.sub(r'"127\.0\.0\.1:8000:8000"', f'"0.0.0.0:{port_b}:8000"', compose)
        compose = re.sub(r'"127\.0\.0\.1:3000:5173"', f'"0.0.0.0:{port_f}:5173"', compose)
        compose = re.sub(r'"8000:8000"', f'"0.0.0.0:{port_b}:8000"', compose)
        compose = re.sub(r'"3000:5173"', f'"0.0.0.0:{port_f}:5173"', compose)
        # Strip bind-mount volumes — in Docker-in-Docker, bind mounts resolve
        # to host paths, not container paths, so they overlay the COPY-baked
        # code with empty host dirs. Remove relative bind mounts so clones use
        # only code baked into the image. Keep /var/run/docker.sock (absolute
        # host path — always valid) and anonymous volumes (node_modules etc).
        compose = re.sub(
            r"^\s*-\s+\.(?=:|/)",
            "# bind-mount stripped (DinD): ",
            compose,
            flags=re.MULTILINE,
        )

        compose_path.write_text(compose)

        result = await self._compose_up(clone_dir, name)
        if result.returncode != 0:
            shutil.rmtree(clone_dir, ignore_errors=True)
            return f"Error: Failed to start clone: {result.stderr[-500:]}"

        registry.append({
            "name": name,
            "ports": {"backend": port_b, "frontend": port_f},
            "status": "running",
        })
        self._save_registry(registry)

        return (
            f"Clone '{name}' is running!\n"
            f"   Backend:  http://localhost:{port_b}\n"
            f"   Frontend: http://localhost:{port_f}\n"
            f"   The clone has its own database and can spawn its own clones."
        )

    async def list_clones(self) -> str:
        """List all clones with their ports and status."""
        registry = self._load_registry()
        if not registry:
            return "No clones yet. Use create_clone to make one."

        for c in registry:
            try:
                ps = await self._compose_cmd(c["name"], "ps", "--format", "json")
                c["status"] = "running" if ps.returncode == 0 and ps.stdout.strip() else "stopped"
            except Exception:
                c["status"] = "unknown"
        self._save_registry(registry)

        lines = [f"{'Name':<20} {'Backend':<8} {'Frontend':<9} {'Status':<10}", "-" * 50]
        for c in registry:
            lines.append(
                f"{c['name']:<20} {c['ports']['backend']:<8} {c['ports']['frontend']:<9} {c['status']:<10}"
            )
        return "\n".join(lines)

    async def stop_clone(self, name: str) -> str:
        """Stop a clone's containers (keeps code and data)."""
        name = name.strip().lower()
        registry = self._load_registry()
        if not any(c["name"] == name for c in registry):
            return f"Error: Clone '{name}' not found."
        result = await self._compose_cmd(name, "stop")
        if result.returncode != 0:
            return f"Error: Failed to stop: {result.stderr[-300:]}"
        for c in registry:
            if c["name"] == name:
                c["status"] = "stopped"
        self._save_registry(registry)
        return f"Clone '{name}' stopped."

    async def start_clone(self, name: str) -> str:
        """Start a previously stopped clone."""
        name = name.strip().lower()
        registry = self._load_registry()
        clone = next((c for c in registry if c["name"] == name), None)
        if not clone:
            return f"Error: Clone '{name}' not found."
        result = await self._compose_cmd(name, "start")
        if result.returncode != 0:
            return f"Error: Failed to start: {result.stderr[-300:]}"
        clone["status"] = "running"
        self._save_registry(registry)
        return f"Clone '{name}' started."

    async def destroy_clone(self, name: str) -> str:
        """Destroy a clone — stops containers, removes code and data permanently."""
        name = name.strip().lower()
        registry = self._load_registry()
        if not any(c["name"] == name for c in registry):
            return f"Error: Clone '{name}' not found."
        errors = []
        try:
            await self._compose_cmd(name, "down", "-v")
        except Exception as e:
            errors.append(f"container shutdown: {e}")
        shutil.rmtree(_CLONES_DIR / name, ignore_errors=True)
        # Always remove from registry, even if containers/files failed
        registry = [c for c in registry if c["name"] != name]
        self._save_registry(registry)
        if errors:
            return f"Clone '{name}' destroyed (with warnings: {'; '.join(errors)})."
        return f"Clone '{name}' destroyed."
