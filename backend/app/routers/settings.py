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
                clones = data
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
