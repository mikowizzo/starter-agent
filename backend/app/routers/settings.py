"""Model settings — list, view, switch, and restart."""

import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.models import MODELS, make_model

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("/models")
async def get_models():
    """Return all available models."""
    return {"models": {k: {"key": k, **v} for k, v in MODELS.items()}}


@router.get("/model")
async def get_model(request: Request):
    """Return the currently active model info."""
    team = request.app.state.team
    current_id = team.model.id if team.model else None
    for key, info in MODELS.items():
        if info["id"] == current_id:
            return {"current": key, **info}
    return {"current": "unknown", "id": current_id, "name": current_id, "provider": "unknown"}


@router.post("/model")
async def set_model(request: Request):
    """Switch the agent to a different model."""
    body = await request.json()
    model_key = body.get("model", "").strip()
    if model_key not in MODELS:
        return JSONResponse(
            {"error": f"Unknown model '{model_key}'. Available: {list(MODELS.keys())}"},
            status_code=400,
        )
    # Check if the model requires an API key that isn't set
    info = MODELS[model_key]
    api_key_env = info.get("api_key_env", "OPENCODE_API_KEY")
    if not os.environ.get(api_key_env):
        return JSONResponse(
            {"error": f"Missing API key: {api_key_env}. Add it to your .env file and restart."},
            status_code=500,
        )
    
    new_model = make_model(model_key)
    request.app.state.team.model = new_model
    return {"current": model_key, **MODELS[model_key]}


def _own_frontend_port() -> int:
    """This instance's published frontend port.

    Clones have FRONTEND_PORT in their .env (via env_file). The parent has no
    .env, so fall back to inspecting our compose project's frontend container.
    """
    env_port = os.environ.get("FRONTEND_PORT", "")
    if env_port.isdigit():
        return int(env_port)
    try:
        project = subprocess.run(
            ["docker", "inspect", os.environ.get("HOSTNAME", ""),
             "--format", "{{index .Config.Labels \"com.docker.compose.project\"}}"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        if project:
            ports = subprocess.run(
                ["docker", "ps",
                 "--filter", f"label=com.docker.compose.project={project}",
                 "--filter", "label=com.docker.compose.service=frontend",
                 "--format", "{{.Ports}}"],
                capture_output=True, text=True, timeout=10,
            ).stdout
            m = re.search(r":(\d+)->", ports)
            if m:
                return int(m.group(1))
    except Exception:
        pass
    return 3000


def _reconcile_clone_status(clones: list) -> list:
    """Self-heal clone statuses against Docker's live state.

    Registry writes can be lost (crashed backend mid-write, manual edits,
    historical races) leaving e.g. "stopped" for containers that actually
    run — which hides clones from the bottombar switcher. One `docker ps`
    gives the truth: a clone is running iff its compose project has any
    running container. Mismatches are fixed in the registry so the file
    converges to reality too. Docker failures are non-fatal — registry
    values pass through untouched.
    """
    try:
        r = subprocess.run(
            ["docker", "ps", "--format",
             "{{.Label \"com.docker.compose.project\"}}"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return clones
        running_projects = {p.strip() for p in r.stdout.splitlines() if p.strip()}
    except Exception:
        return clones

    changed = False
    for c in clones:
        live = "running" if c.get("name") in running_projects else "stopped"
        if c.get("status") != live:
            c["status"] = live
            changed = True

    if changed:
        try:
            reg = Path("/workspace/.clones/registry.json")
            reg.parent.mkdir(parents=True, exist_ok=True)
            reg.write_text(json.dumps(clones, indent=2))
        except Exception:
            pass  # Report the live view regardless; persisting is best-effort
    return clones


@router.get("/clones")
async def get_clones(request: Request):
    """This instance's frontend port plus all registered clones — powers the
    bottombar instance switcher. Each container's /workspace/.clones/registry.json
    is its own registry (clones keep their own), so this works on any instance.
    A parent.json written by the creating instance carries the parent's name and
    frontend port, so this instance's switcher can link back to it.
    """
    clones: list = []
    reg = Path("/workspace/.clones/registry.json")
    if reg.exists():
        try:
            data = json.loads(reg.read_text())
            if isinstance(data, list):
                clones = _reconcile_clone_status(data)
        except Exception:
            pass

    parent = None
    pf = Path("/workspace/parent.json")
    if pf.exists():
        try:
            data = json.loads(pf.read_text())
            if data.get("name") and str(data.get("frontend_port", "")).isdigit():
                parent = {"name": data["name"], "frontend_port": int(data["frontend_port"])}
        except Exception:
            pass

    return {
        "self_name": request.app.state.team.name,
        "self_port": _own_frontend_port(),
        "parent": parent,
        "clones": clones,
    }


@router.post("/restart")
async def restart_server():
    """Restart the uvicorn server in-place."""
    def _do_restart():
        import time
        time.sleep(0.5)
        os.execv(sys.executable, [sys.executable] + sys.argv)

    threading.Thread(target=_do_restart, daemon=True).start()
    return JSONResponse({"status": "restarting"}, status_code=202)
