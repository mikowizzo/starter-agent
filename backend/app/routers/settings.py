"""Model settings — list, view, switch, and restart."""

import os
import sys
import threading

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


@router.post("/restart")
async def restart_server():
    """Restart the uvicorn server in-place."""
    def _do_restart():
        import time
        time.sleep(0.5)
        os.execv(sys.executable, [sys.executable] + sys.argv)

    threading.Thread(target=_do_restart, daemon=True).start()
    return JSONResponse({"status": "restarting"}, status_code=202)
