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

# Strict name pattern — prevents path traversal (../) and invalid Docker
# project names. Lowercase alphanumeric + hyphens, must start/end with
# alphanumeric.
_NAME_RE = re.compile(r'^[a-z0-9]([a-z0-9-]*[a-z0-9])?$')


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
        self._lock = asyncio.Lock()

    @staticmethod
    def _load_registry() -> list[dict]:
        if _REGISTRY.exists():
            data = json.loads(_REGISTRY.read_text())
            # Guard against corrupted registry (dict instead of list).
            if isinstance(data, list):
                return data
            return []
        return []

    @staticmethod
    def _save_registry(clones: list[dict]) -> None:
        _CLONES_DIR.mkdir(parents=True, exist_ok=True)
        _REGISTRY.write_text(json.dumps(clones, indent=2))

    @staticmethod
    def _port_in_use(port: int) -> bool:
        """Check if a port is already bound by a running container OR on the host.

        Docker-in-Docker means our docker ps shows host containers, but a
        socket test catches non-Docker listeners too (e.g. dev servers,
        databases, or orphaned processes).
        """
        # 1. Check docker containers
        try:
            r = subprocess.run(
                ["docker", "ps", "--format", "{{.Ports}}"],
                capture_output=True, text=True, timeout=10,
            )
            if f":{port}->" in r.stdout:
                return True
        except Exception:
            pass
        # 2. Check host-level socket binding
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1)
        try:
            s.bind(("0.0.0.0", port))
            s.close()
            return False
        except OSError:
            s.close()
            return True

    @staticmethod
    def _next_ports(registry: list[dict]) -> tuple[int, int]:
        b, f = _BASE_PORT_BACKEND, _BASE_PORT_FRONTEND
        if registry:
            b = max(c["ports"]["backend"] for c in registry) + 1
            f = max(c["ports"]["frontend"] for c in registry) + 1
        # Advance past any ports already bound by running containers
        # (orphaned containers from a failed destroy, manual docker run, etc.)
        while CloneTools._port_in_use(b) or CloneTools._port_in_use(f):
            b += 1
            f += 1
        return b, f

    @staticmethod
    def _host_workspace_dir() -> str | None:
        """Detect the host path that maps to /workspace in this container.

        Uses `docker inspect` on ourselves to read the bind mount.
        Returns the absolute host path or None if not found.
        """
        hostname = os.environ.get("HOSTNAME", "")
        if not hostname:
            return None
        try:
            r = subprocess.run(
                ["docker", "inspect", hostname,
                 '--format', '{{range .Mounts}}{{if eq .Destination "/workspace"}}{{.Source}}{{end}}{{end}}'],
                capture_output=True, text=True, timeout=10,
            )
            path = r.stdout.strip()
            return path or None
        except Exception:
            return None

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
        if not _NAME_RE.match(name):
            return (
                "Error: Invalid name. Use lowercase letters, numbers, and "
                "hyphens only (e.g. 'my-clone')."
            )

        ok, msg = await self._docker_check()
        if not ok:
            return f"Error: {msg}"

        # Atomically reserve the name and ports so concurrent create_clone
        # calls don't collide. Heavy work (git clone, docker build) happens
        # outside the lock.
        async with self._lock:
            registry = self._load_registry()
            if any(c["name"] == name for c in registry):
                return f"Error: Clone '{name}' already exists."
            clone_dir = _CLONES_DIR / name
            if clone_dir.exists():
                return f"Error: Directory {clone_dir} already exists."
            port_b, port_f = self._next_ports(registry)
            # Pre-register as pending so concurrent calls get different ports
            registry.append({
                "name": name,
                "ports": {"backend": port_b, "frontend": port_f},
                "status": "pending",
            })
            self._save_registry(registry)

        clone_dir = _CLONES_DIR / name

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
            await self._remove_from_registry(name)
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

        # Overwrite port assignments in .env. The parent's .env has the
        # parent's ports (e.g. 8001/3001), and .env values override the
        # compose file defaults — so without this, the clone tries to bind
        # the SAME ports as the parent and fails with "port already allocated".
        env_file = clone_dir / ".env"
        if env_file.exists():
            env_text = env_file.read_text()
            if re.search(r'(?m)^BACKEND_PORT=', env_text):
                env_text = re.sub(r'(?m)^BACKEND_PORT=.*', f'BACKEND_PORT={port_b}', env_text)
            else:
                env_text += f'\nBACKEND_PORT={port_b}\n'
            if re.search(r'(?m)^FRONTEND_PORT=', env_text):
                env_text = re.sub(r'(?m)^FRONTEND_PORT=.*', f'FRONTEND_PORT={port_f}', env_text)
            else:
                env_text += f'\nFRONTEND_PORT={port_f}\n'
            env_file.write_text(env_text)

        compose_path = clone_dir / "docker-compose.yml"
        if not compose_path.exists():
            await self._remove_from_registry(name)
            shutil.rmtree(clone_dir)
            return "Error: No docker-compose.yml found."

        compose = compose_path.read_text()
        # Swap only the port numbers — preserve the ${BIND_HOST:-127.0.0.1}
        # prefix so clones inherit the parent's binding (localhost-only by
        # default, Tailscale if BIND_HOST is set). NEVER fall back to 0.0.0.0.
        # Handle both old hardcoded format (:8000:8000") and new variable
        # format (:${BACKEND_PORT:-8000}:8000").
        b_pattern = r':(\$\{BACKEND_PORT:-)?8000\}?:8000"'
        f_pattern = r':(\$\{FRONTEND_PORT:-)?3000\}?:5173"'
        if not re.search(b_pattern, compose):
            await self._remove_from_registry(name)
            shutil.rmtree(clone_dir)
            return f"Error: Backend port pattern not found in docker-compose.yml."
        if not re.search(f_pattern, compose):
            await self._remove_from_registry(name)
            shutil.rmtree(clone_dir)
            return f"Error: Frontend port pattern not found in docker-compose.yml."
        # Replace the port number in both patterns
        compose = re.sub(
            r'BACKEND_PORT:-(\d+)}',
            f'BACKEND_PORT:-{port_b}}}',
            compose,
        )
        compose = re.sub(
            r'FRONTEND_PORT:-(\d+)}',
            f'FRONTEND_PORT:-{port_f}}}',
            compose,
        )
        # Also handle old hardcoded format (fallback)
        compose = re.sub(r':8000:8000"', f':{port_b}:8000"', compose)
        compose = re.sub(r':3000:5173"', f':{port_f}:5173"', compose)
        # Rewrite bind-mount volumes to absolute host paths.
        # In Docker-in-Docker, relative bind mounts (./backend, ./frontend)
        # resolve against the HOST filesystem, not the container — so they
        # point to wrong/empty directories. Instead of stripping them (which
        # kills HMR), detect our own host workspace path and rewrite relative
        # mounts to absolute host paths. This preserves live file watching.
        host_ws = self._host_workspace_dir()
        if host_ws:
            def _rewrite_mount(m):
                # m.group(3) is the relative path like ./frontend or ./.env
                clean = m.group(3).strip("'").strip('"')
                host_path = str(Path(host_ws) / clean)
                return f"{m.group(1)}{host_path}{m.group(4)}"

            compose = re.sub(
                r'(\s*-\s+)(["\']?)(\.{1,2}/[^:\s]+)(:)',
                _rewrite_mount,
                compose,
                flags=re.MULTILINE,
            )
        else:
            # Fallback: strip bind mounts if we can't detect host path
            compose = re.sub(
                r'^\s*-\s+["\']?\.(?=:|/|["\'])',
                "# bind-mount stripped (DinD): ",
                compose,
                flags=re.MULTILINE,
            )

        compose_path.write_text(compose)

        result = await self._compose_up(clone_dir, name)
        if result.returncode != 0:
            await self._remove_from_registry(name)
            shutil.rmtree(clone_dir, ignore_errors=True)
            return f"Error: Failed to start clone: {result.stderr[-500:]}"

        async with self._lock:
            registry = self._load_registry()
            for c in registry:
                if c["name"] == name:
                    c["status"] = "running"
            self._save_registry(registry)

        return (
            f"Clone '{name}' is running!\n"
            f"   Backend:  http://localhost:{port_b}\n"
            f"   Frontend: http://localhost:{port_f}\n"
            f"   The clone has its own database and can spawn its own clones."
        )

    async def _remove_from_registry(self, name: str) -> None:
        """Thread-safe removal of a clone from the registry."""
        async with self._lock:
            registry = self._load_registry()
            registry = [c for c in registry if c["name"] != name]
            self._save_registry(registry)

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
            await self._compose_cmd(name, "down", "-v", "--rmi", "local")
        except Exception as e:
            errors.append(f"container shutdown: {e}")
        clone_dir = _CLONES_DIR / name
        try:
            shutil.rmtree(clone_dir)
        except Exception as e:
            errors.append(f"file removal: {e}")
        # Always remove from registry, even if containers/files failed
        registry = [c for c in registry if c["name"] != name]
        self._save_registry(registry)
        if errors:
            return f"Clone '{name}' destroyed (with warnings: {'; '.join(errors)})."
        return f"Clone '{name}' destroyed."
