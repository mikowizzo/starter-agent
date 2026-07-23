"""Backend — FastAPI app entrypoint.

Thin bootstrap: env validation, team construction, router wiring.
All route logic lives in app/routers/.
"""

from agno.db.sqlite import SqliteDb
from agno.os import AgentOS

from app.config import BASE_DIR, DB_FILE, validate_env
from app.agents.coordinator import build_team
from app.routers import sessions, settings

validate_env()

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
