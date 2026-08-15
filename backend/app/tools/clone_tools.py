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

from app.routers.settings import _own_frontend_port

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

# Compose projects owned by the host/parent — never touched by GC.
# The parent stack is "repo" (compose sets project name from cwd basename).
# Extend this set if the host runs other non-clone compose stacks.
_PROTECTED_PROJECTS = {
    os.environ.get("CLONE_NAME", "repo"),       # this agent's own stack
    "miko",                                       # the orchestrator
    "eva",                                        # other host stacks
    "ibgateway", "ib_gateway",
}
def _add_backend_net_alias(compose: str, name: str) -> str:
    """Give the clone's backend a unique network alias (``backend-<name>``).

    All clone stacks share the ``starter-app-net`` network, and every one
    declares a service named ``backend``. Docker DNS therefore resolves the
    bare name ``backend`` to *every* clone's backend — a frontend proxying to
    ``http://backend:8000`` round-robins between all of them, so conversations
    bounce between crew members (the "identity crisis"). The alias makes
    ``backend-<name>`` resolve only to this clone's own backend, and the
    clone-creation code points the frontend proxy at it.

    The service keeps its ``backend`` name (so compose reuses the existing
    image — renaming would trigger a fresh build); only the network alias is
    added, which compose applies on recreate without rebuilding.
    """
    if f'"backend-{name}"' in compose or f"backend-{name}:" in compose:
        return compose  # already patched — idempotent

    # 1. Give the backend service a unique alias so the parent and other
    #    clones can address it as backend-<name>. Convert the backend's
    #    list-form networks entry into a map form that carries the alias.
    compose = re.sub(
        r"(^\s*)networks:\n\s*-\s*starter-app-net\n",
        lambda m: (
            f"{m.group(1)}networks:\n"
            f"{m.group(1)}  starter-app-net:\n"
            f"{m.group(1)}    aliases:\n"
            f'{m.group(1)}      - "backend-{name}"\n'
        ),
        compose,
        count=1,
        flags=re.MULTILINE,
    )

    # 2. Mark the shared network EXTERNAL so compose reuses the REAL,
    #    pre-existing starter-app-net instead of silently creating a
    #    project-scoped copy (e.g. nami_starter-app-net). Without this the
    #    alias from step 1 lands on the private copy, invisible to the
    #    parent — the exact name-resolution bug we hit with team_comms.
    compose = re.sub(
        r"(?m)^(\s*)starter-app-net:\n\s*driver:\s*bridge\n",
        rf"\1starter-app-net:\n\1  external: true\n",
        compose,
        count=1,
    )
    return compose


class CloneTools(Toolkit):
    """Create, list, stop, start, rebuild, destroy, and garbage-collect clones."""

    def __init__(self, team_name: str = "") -> None:
        super().__init__(
            name="clone_tools",
            tools=[
                self.create_clone,
                self.list_clones,
                self.stop_clone,
                self.start_clone,
                self.rebuild_clone,
                self.destroy_clone,
                self.gc_orphans,
            ],
        )
        self._lock = asyncio.Lock()
        self._team_name = team_name

    # ------------------------------------------------------------------
    # Registry helpers
    # ------------------------------------------------------------------

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

    async def _remove_from_registry(self, name: str) -> None:
        """Thread-safe removal of a clone from the registry."""
        async with self._lock:
            registry = self._load_registry()
            registry = [c for c in registry if c["name"] != name]
            self._save_registry(registry)

    # ------------------------------------------------------------------
    # Docker helpers
    # ------------------------------------------------------------------

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
    async def _next_ports(registry: list[dict]) -> tuple[int, int]:
        """Pick the next free port pair for a new clone.

        Scans EVERY agent's clone registry (not just ours) so ports never
        collide with clones parented to other agents (e.g. sanji under nami,
        icio's crew) — even when those clones are currently stopped, which
        a live port-bind probe alone would miss.
        """
        # Union ports from every running agent's registry
        all_b = {c["ports"]["backend"] for c in registry}
        all_f = {c["ports"]["frontend"] for c in registry}
        try:
            r = await asyncio.to_thread(
                subprocess.run,
                ["docker", "ps", "--format", "{{.Names}}"],
                capture_output=True, text=True, timeout=15,
            )
            self_backend = f"{os.environ.get('CLONE_NAME', 'repo')}-backend-1"
            for cname in (n.strip() for n in r.stdout.splitlines()):
                if not cname.endswith("-backend-1") or cname == self_backend:
                    continue
                try:
                    rr = await asyncio.to_thread(
                        subprocess.run,
                        ["docker", "exec", cname, "cat",
                         "/workspace/.clones/registry.json"],
                        capture_output=True, text=True, timeout=10,
                    )
                    for c in json.loads(rr.stdout or "[]"):
                        if isinstance(c, dict):
                            p = c.get("ports") or {}
                            if p.get("backend"):
                                all_b.add(p["backend"])
                            if p.get("frontend"):
                                all_f.add(p["frontend"])
                except Exception:
                    continue
        except Exception:
            pass

        b = max(all_b, default=_BASE_PORT_BACKEND - 1) + 1
        f = max(all_f, default=_BASE_PORT_FRONTEND - 1) + 1
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
                "Permission denied on the Docker socket. The container was "
                "started without the host's docker group. Recreate it from the "
                "host: docker compose up -d --build --force-recreate "
                "(newer images self-heal socket access in the entrypoint, so "
                "a plain restart suffices once rebuilt)."
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
    async def _compose_cmd(name: str, *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
        clone_dir = _CLONES_DIR / name
        return await asyncio.to_thread(
            subprocess.run,
            ["docker", "compose", "-p", name, *args],
            capture_output=True, text=True, timeout=timeout,
            cwd=str(clone_dir),
        )

    @staticmethod
    async def _compose_up(clone_dir: Path, name: str, port_b: int, port_f: int) -> subprocess.CompletedProcess:
        # Ensure the shared external network exists BEFORE compose up, since
        # clone compose files reference it with external: true (compose will
        # refuse to start if it's missing). Create is idempotent (`|| true`).
        try:
            await asyncio.to_thread(
                subprocess.run,
                ["docker", "network", "create", "starter-app-net"],
                capture_output=True, text=True, timeout=30,
            )
        except Exception:
            pass  # Non-fatal — compose will still try, and it often already exists.

        return await asyncio.to_thread(
            subprocess.run,
            ["docker", "compose", "-p", name, "up", "--build", "-d"],
            capture_output=True, text=True, timeout=300,
            env={**os.environ, "BACKEND_PORT": str(port_b), "FRONTEND_PORT": str(port_f)},
            cwd=str(clone_dir),
        )

    # ------------------------------------------------------------------
    # GC primitive — label-scoped teardown (works even after dir is gone)
    # ------------------------------------------------------------------

    @staticmethod
    async def _sweep_project(name: str) -> list[str]:
        """Force-remove ALL docker artifacts for a compose project by label.

        Works even after the clone directory has been deleted, because it
        queries the Docker daemon directly via compose project labels.
        Idempotent — if nothing exists, returns empty list. Never raises.
        """
        errors: list[str] = []
        label = f"label=com.docker.compose.project={name}"

        async def _run(cmd: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
            return await asyncio.to_thread(
                subprocess.run, cmd, capture_output=True, text=True, timeout=timeout
            )

        for ls_cmd, rm_cmd, kind in [
            (["docker", "ps", "-aq", "--filter", label], ["docker", "rm", "-f"], "container"),
            (["docker", "images", "-aq", "--filter", label], ["docker", "rmi", "-f"], "image"),
            (["docker", "volume", "ls", "-q", "--filter", label], ["docker", "volume", "rm"], "volume"),
            (["docker", "network", "ls", "-q", "--filter", label], ["docker", "network", "rm"], "network"),
        ]:
            try:
                ids = (await _run(ls_cmd)).stdout.split()
                if ids:
                    r = await _run(rm_cmd + ids)
                    if r.returncode != 0 and r.stderr.strip():
                        errors.append(f"{kind}: {r.stderr.strip()[:200]}")
            except Exception as e:
                errors.append(f"{kind}: {e}")

        return errors

    @staticmethod
    async def _safe_teardown(clone_dir: Path, name: str) -> None:
        """Best-effort Docker teardown for a failed create or destroy.

        Tries `docker compose down` first (graceful, removes volumes/images).
        Falls back to label-based force removal if the directory is gone or
        compose down fails. Never raises — caller handles errors.
        """
        # Try compose down first (graceful path)
        if clone_dir.exists():
            try:
                await asyncio.to_thread(
                    subprocess.run,
                    ["docker", "compose", "-p", name, "down", "-v",
                     "--rmi", "local", "--remove-orphans"],
                    capture_output=True, text=True, timeout=60,
                    cwd=str(clone_dir),
                )
            except Exception:
                pass  # Fall through to label sweep

        # Always run label sweep as a backstop — catches anything compose
        # down missed, and works even if the directory is already gone.
        await CloneTools._sweep_project(name)

    # ------------------------------------------------------------------
    # Public tools
    # ------------------------------------------------------------------

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
            port_b, port_f = await self._next_ports(registry)
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
            await self._safe_teardown(clone_dir, name)
            await self._remove_from_registry(name)
            shutil.rmtree(clone_dir, ignore_errors=True)
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
            await self._safe_teardown(clone_dir, name)
            await self._remove_from_registry(name)
            shutil.rmtree(clone_dir, ignore_errors=True)
            return "Error: Backend port pattern not found in docker-compose.yml."
        if not re.search(f_pattern, compose):
            await self._safe_teardown(clone_dir, name)
            await self._remove_from_registry(name)
            shutil.rmtree(clone_dir, ignore_errors=True)
            return "Error: Frontend port pattern not found in docker-compose.yml."

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
        parent_host_ws = self._host_workspace_dir()
        clone_host_ws = str(Path(parent_host_ws) / ".clones" / name) if parent_host_ws else None
        if clone_host_ws:
            def _rewrite_mount(m):
                # m.group(3) is the relative path like ./frontend, ./.env, or .
                clean = m.group(3).strip("'").strip('"')
                host_path = str(Path(clone_host_ws) / clean)
                return f"{m.group(1)}{host_path}{m.group(4)}"

            compose = re.sub(
                r'(\s*-\s+)(["\']?)(\.{1,2}(?:/[^:\s]+)?)(:)',
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

        # Every clone joins the shared starter-app-net, where the bare
        # service name "backend" is aliased on every backend container. A
        # frontend proxying to http://backend:8000 would round-robin across
        # ALL clones (crew members answering each other's calls!). Give this
        # clone a unique alias and point its frontend at it, so a clone only
        # ever talks to itself.
        compose = _add_backend_net_alias(compose, name)
        compose_path.write_text(compose)

        # Point the clone's frontend proxy at its own unique backend alias.
        vite_config = clone_dir / "frontend" / "vite.config.ts"
        if vite_config.exists():
            vite_text = vite_config.read_text()
            vite_text = vite_text.replace(
                "http://backend:8000", f"http://backend-{name}:8000"
            )
            vite_config.write_text(vite_text)

        # Tell the clone who its parent is, so its instance switcher can link
        # back to us. Read by /settings/clones on the clone's side.
        try:
            (clone_dir / "parent.json").write_text(json.dumps({
                "name": self._team_name or "parent",
                "frontend_port": _own_frontend_port(),
            }))
        except Exception:
            pass  # Non-fatal — switcher just shows no parent pill

        result = await self._compose_up(clone_dir, name, port_b, port_f)
        if result.returncode != 0:
            # LAYER 1: Full teardown before removing dir/registry.
            # Stops the leak at the source — no orphaned containers/images.
            await self._safe_teardown(clone_dir, name)
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

    async def list_clones(self) -> str:
        """List all clones with their ports and status."""
        # GC on read: cheap insurance that catches orphans between explicit calls
        await self.gc_orphans()

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
        if not any(c["name"] == name for c in self._load_registry()):
            return f"Error: Clone '{name}' not found."
        # The lock spans the compose command AND the registry write. Unlocked,
        # a stop racing a start double-SIGTERMed a freshly started backend,
        # and unsynchronised load-modify-save cycles silently lost status
        # updates (registry said "stopped" while containers ran, hiding
        # clones from the switcher).
        async with self._lock:
            result = await self._compose_cmd(name, "stop")
            if result.returncode != 0:
                return f"Error: Failed to stop: {result.stderr[-300:]}"
            registry = self._load_registry()
            for c in registry:
                if c["name"] == name:
                    c["status"] = "stopped"
            self._save_registry(registry)
        return f"Clone '{name}' stopped."

    async def start_clone(self, name: str) -> str:
        """Start a previously stopped clone."""
        name = name.strip().lower()
        if not any(c["name"] == name for c in self._load_registry()):
            return f"Error: Clone '{name}' not found."
        # Same lock discipline as stop_clone: start must never interleave
        # with a stop, and the registry write must be read-modify-write
        # under the lock or concurrent updates get lost.
        async with self._lock:
            result = await self._compose_cmd(name, "start")
            if result.returncode != 0:
                return f"Error: Failed to start: {result.stderr[-300:]}"
            registry = self._load_registry()
            for c in registry:
                if c["name"] == name:
                    c["status"] = "running"
            self._save_registry(registry)
        return f"Clone '{name}' started."

    async def rebuild_clone(self, name: str) -> str:
        """Rebuild a clone's images and restart it.

        Picks up changes baked into the image at build time (e.g. new deps in
        requirements.txt) — docker compose start does NOT.
        """
        name = name.strip().lower()
        if not any(c["name"] == name for c in self._load_registry()):
            return f"Error: Clone '{name}' not found."
        # Lock held across the (slow) rebuild too — a rebuild racing a
        # stop/start is the same interleaving hazard, and clone ops are rare
        # enough that serialising them is cheaper than debugging the races.
        async with self._lock:
            result = await self._compose_cmd(name, "up", "--build", "-d", timeout=300)
            if result.returncode != 0:
                return f"Error: Rebuild failed: {result.stderr[-300:]}"
            registry = self._load_registry()
            for c in registry:
                if c["name"] == name:
                    c["status"] = "running"
            self._save_registry(registry)
        return f"Clone '{name}' rebuilt and restarted."


    async def destroy_clone(self, name: str) -> str:
        """Destroy a clone — stops containers, removes code and data permanently.

        If Docker teardown fails (timeout, daemon error), the clone is marked
        as 'zombie' in the registry. The gc_orphans tool will retry cleanup
        later. This prevents orphaned resources from accumulating silently.
        """
        name = name.strip().lower()
        registry = self._load_registry()
        clone = next((c for c in registry if c["name"] == name), None)
        if not clone:
            return f"Error: Clone '{name}' not found."

        errors = []
        clone_dir = _CLONES_DIR / name

        # Retry compose down up to 3 times with increasing timeout
        down_ok = False
        for attempt in range(3):
            timeout = 60 + attempt * 30  # 60s, 90s, 120s
            try:
                r = await self._compose_cmd(
                    name, "down", "-v", "--rmi", "local", "--remove-orphans",
                    timeout=timeout,
                )
                if r.returncode == 0:
                    down_ok = True
                    break
                if attempt < 2:
                    await asyncio.sleep(2)
            except subprocess.TimeoutExpired:
                if attempt < 2:
                    await asyncio.sleep(2)
            except Exception as e:
                errors.append(f"compose down: {e}")
                break

        # Label-based backstop — catches anything compose down missed
        sweep_errors = await self._sweep_project(name)
        errors.extend(sweep_errors)

        if down_ok and not sweep_errors:
            # Clean teardown — remove from registry and delete dir
            async with self._lock:
                registry = [c for c in self._load_registry() if c["name"] != name]
                self._save_registry(registry)
            shutil.rmtree(clone_dir, ignore_errors=True)
            return f"Clone '{name}' destroyed."
        elif errors:
            # Partial failure — mark as zombie, keep registry entry for GC retry
            async with self._lock:
                registry = self._load_registry()
                for c in registry:
                    if c["name"] == name:
                        c["status"] = "zombie"
                self._save_registry(registry)
            shutil.rmtree(clone_dir, ignore_errors=True)
            return (
                f"Clone '{name}' partially destroyed — marked as zombie. "
                f"Warnings: {'; '.join(errors)}. Run gc_orphans to finish cleanup."
            )
        else:
            # compose down said success but sweep found leftovers
            async with self._lock:
                registry = [c for c in self._load_registry() if c["name"] != name]
                self._save_registry(registry)
            shutil.rmtree(clone_dir, ignore_errors=True)
            return f"Clone '{name}' destroyed."

    # ------------------------------------------------------------------
    # Layer 3: Reconciler GC
    # ------------------------------------------------------------------

    async def gc_orphans(self) -> str:
        """Garbage-collect orphaned Docker resources from failed clone operations.

        Scans for compose projects, containers, and images that exist in Docker
        but are NOT tracked in the clone registry — then removes them. Also
        retries cleanup of any clones marked as 'zombie' (destroyed but not
        fully cleaned up).

        Safe to call anytime. Never touches running containers, registered
        clones, or protected host stacks.

        Returns:
            A summary of what was cleaned up.
        """
        registry = self._load_registry()
        registry_names = {c["name"] for c in registry}

        # --- 1.5 Union with other agents' registries (delegated clones) ---
        # A clone may be parented to another agent (e.g. icio's crew): it is
        # tracked in THAT agent's registry, not ours. Sweep only what no
        # registry anywhere tracks, so delegated clones are never orphans.
        try:
            r = await asyncio.to_thread(
                subprocess.run,
                ["docker", "ps", "--format", "{{.Names}}"],
                capture_output=True, text=True, timeout=15,
            )
            self_backend = f"{os.environ.get('CLONE_NAME', 'repo')}-backend-1"
            other_backends = [
                n.strip() for n in r.stdout.splitlines()
                if n.strip().endswith("-backend-1") and n.strip() != self_backend
            ]
            for cname in other_backends:
                try:
                    rr = await asyncio.to_thread(
                        subprocess.run,
                        ["docker", "exec", cname, "cat",
                         "/workspace/.clones/registry.json"],
                        capture_output=True, text=True, timeout=10,
                    )
                    for c in json.loads(rr.stdout or "[]"):
                        if isinstance(c, dict) and c.get("name"):
                            registry_names.add(c["name"])
                except Exception:
                    continue
        except Exception:
            pass

        removed: list[str] = []

        # --- 1. Retry zombie clones ---
        zombies = [c for c in registry if c.get("status") == "zombie"]
        for z in zombies:
            errors = await self._sweep_project(z["name"])
            if errors:
                removed.append(f"zombie {z['name']}: {len(errors)} errors remain")
            else:
                registry = [c for c in registry if c["name"] != z["name"]]
                removed.append(f"zombie {z['name']}: cleaned")

        if zombies:
            self._save_registry(registry)

        # --- 2. Find orphaned compose projects ---
        # List all containers and extract their compose project labels.
        # Any project NOT in the registry and NOT protected is an orphan.
        try:
            r = await asyncio.to_thread(
                subprocess.run,
                ["docker", "ps", "-a", "--format",
                 "{{.Label \"com.docker.compose.project\"}}"],
                capture_output=True, text=True, timeout=15,
            )
            all_projects = {
                p.strip() for p in r.stdout.splitlines()
                if p.strip() and p.strip() != "default"
            }
        except Exception:
            all_projects = set()

        orphaned_projects = all_projects - registry_names - _PROTECTED_PROJECTS

        for proj in orphaned_projects:
            # Skip if any container in this project is currently running
            # (could be a clone that's mid-creation or manually started)
            try:
                ps = await asyncio.to_thread(
                    subprocess.run,
                    ["docker", "ps", "--filter", f"label=com.docker.compose.project={proj}",
                     "--format", "{{.ID}}"],
                    capture_output=True, text=True, timeout=10,
                )
                if ps.stdout.strip():
                    continue  # Something is still running, don't touch it
            except Exception:
                continue

            errors = await self._sweep_project(proj)
            if errors:
                removed.append(f"orphan {proj}: partial ({len(errors)} errors)")
            else:
                removed.append(f"orphan {proj}: cleaned")

            # Remove orphaned clone directory if it exists
            orphan_dir = _CLONES_DIR / proj
            if orphan_dir.exists():
                shutil.rmtree(orphan_dir, ignore_errors=True)

        # --- 3. Prune dangling images (safe — they're unreferenced by definition) ---
        try:
            r = await asyncio.to_thread(
                subprocess.run,
                ["docker", "image", "prune", "-f"],
                capture_output=True, text=True, timeout=60,
            )
            if r.returncode == 0:
                # Parse "Total reclaimed space: 811MB" if present
                for line in r.stdout.splitlines():
                    if "reclaimed" in line.lower():
                        removed.append(f"images: {line.strip()}")
                        break
                else:
                    # Only report if something was actually pruned
                    if "deleted" in r.stdout.lower() or "total" in r.stdout.lower():
                        removed.append("images: dangling pruned")
        except Exception:
            pass  # Non-critical

        if not removed:
            return "GC: nothing to clean up."
        return "GC complete:\n  - " + "\n  - ".join(removed)
