"""Backend — FastAPI app entrypoint.

Thin bootstrap: env validation, team construction, router wiring.
All route logic lives in app/routers/.
"""

from agno.db.sqlite import SqliteDb
from agno.os import AgentOS

from app.config import BASE_DIR, DB_FILE, validate_env
from app.db import init_attachment_tables
from app.agents.coordinator import build_team
from app.routers import sessions, settings, convert, attachments, providers, files

validate_env()

# Create app-owned tables (attachments, message_attachments) before serving.
# Idempotent — safe to run at every startup.
init_attachment_tables()

db = SqliteDb(db_file=DB_FILE)
team = build_team(base_dir=BASE_DIR, db=db)

app = AgentOS(
    teams=[team],
    db=db,
).get_app()

# Make team available to routers via app.state
app.state.team = team

# Include custom routers (sessions override Agno's defaults via first-match)
app.include_router(sessions.router)
app.include_router(settings.router)
app.include_router(convert.router)
app.include_router(attachments.router)
app.include_router(providers.router)
app.include_router(files.router)
files.register_exception_handlers(app)
