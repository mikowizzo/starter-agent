"""Backend — FastAPI app entrypoint.

Thin bootstrap: patches, env validation, team construction, router wiring.
All route logic lives in app/routers/.
"""

from fastapi import Request
from fastapi.responses import JSONResponse
from agno.db.sqlite import SqliteDb
from agno.os import AgentOS

import app.patches  # noqa: F401 — agno compat patches (see app/patches.py)
from app.config import (
    BASE_DIR, DB_FILE,
    SCHEDULER_BASE_URL, SCHEDULER_POLL_INTERVAL,
    validate_env,
)
from app.db import init_attachment_tables
from app.agents.coordinator import build_team
from app.routers import sessions, settings, convert, attachments, providers, files, runs

validate_env()

# Create app-owned tables (attachments, message_attachments) before serving.
# Idempotent — safe to run at every startup.
init_attachment_tables()

db = SqliteDb(db_file=DB_FILE)
team = build_team(base_dir=BASE_DIR, db=db)

app = AgentOS(
    teams=[team],
    db=db,
    scheduler=True,
    scheduler_poll_interval=SCHEDULER_POLL_INTERVAL,
    scheduler_base_url=SCHEDULER_BASE_URL,
).get_app()

# Make team available to routers via app.state
app.state.team = team


# ── One-run-per-agent guard ─────────────────────────────────────────
# A single agent instance runs one user-facing run at a time. New runs
# are refused with 409 + the active run's info so the UI can attach to
# the existing run instead of spawning a parallel one. Only genuinely
# LIVE runs block (see runs.get_blocking_run). Matches exactly the run
# creation route (POST /teams/{id}/runs), not resume/cancel paths.


# Compiled once: matches /teams/{id}/runs and /agents/{id}/runs (the two
# run-creation routes this instance serves), with or without an /v1 prefix,
# and NOT subpaths like .../runs/{run_id}/resume or .../runs/{id}/cancel.
import re as _re

_RUN_CREATE_RE = _re.compile(r"(?:/v1)?/(?:teams|agents)/[^/]+/runs/?$")


@app.middleware("http")
async def one_run_per_agent(request: Request, call_next):
    # agno's scheduler POSTs scheduled runs to itself over loopback — those
    # are machine-initiated and must never be blocked by a live user run.
    if (
        request.method == "POST"
        and not (
            request.client and request.client.host in ("127.0.0.1", "::1")
        )
        and _RUN_CREATE_RE.fullmatch(request.url.path)
    ):
        blocking = await runs.get_blocking_run()
        if blocking:
            # JSONResponse, not HTTPException — raised inside middleware an
            # HTTPException would bypass FastAPI's handlers and surface as 500.
            return JSONResponse(
                status_code=409,
                content={
                    "message": "An active run is already executing for this agent",
                    "active_run": blocking,
                },
            )
    return await call_next(request)

# Include custom routers (sessions override Agno's defaults via first-match)
app.include_router(sessions.router)
app.include_router(runs.router)
app.include_router(settings.router)
app.include_router(convert.router)
app.include_router(attachments.router)
app.include_router(providers.router)
app.include_router(files.router)
files.register_exception_handlers(app)
