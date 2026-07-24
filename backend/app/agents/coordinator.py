"""Starter Agent coordinator — builds the Team agent with tools, skills, and learning.

Import and add to AgentOS in main.py.
"""


import logging
from pathlib import Path

from agno.db.sqlite import SqliteDb
from agno.skills import LocalSkills, Skills
from agno.skills.errors import SkillValidationError
from agno.team import Team

from app.models import primary_model
from app.tools.code_tools import CodeTools
from app.tools.clone_tools import CloneTools

logger = logging.getLogger(__name__)


def _load_skills(skills_dir: Path) -> Skills:
    """Load every skill in ``skills_dir``, skipping any that fail validation.

    agno's ``Skills()`` re-raises ``SkillValidationError`` from the first bad
    loader and aborts all skill loading — which happens at import time and
    takes the whole backend down. Here each skill folder is validated on its
    own; malformed ones are logged and skipped, so one bad skill never breaks
    startup or blocks the rest.
    """
    loaders: list[LocalSkills] = []
    for p in skills_dir.iterdir():
        if not p.is_dir():
            continue
        loader = LocalSkills(path=str(p))
        try:
            loader.load()  # raises SkillValidationError if the skill is malformed
            loaders.append(loader)
        except SkillValidationError as e:
            logger.warning("Skipping skill %r — validation failed: %s", p.name, e)
        except Exception as e:  # never let a single skill crash startup
            logger.warning("Skipping skill %r — load error: %s", p.name, e)
    return Skills(loaders=loaders)


def build_team(
    base_dir: Path,
    *,
    db: SqliteDb,
) -> Team:
    """Construct the Starter Agent team with tools, skills, and learning."""

    # ── Skills ────────────────────────────────────────────────────────
    _skills_dir = base_dir / "backend" / "app" / "skills"
    skills = _load_skills(_skills_dir)

    # ── Code tools ───────────────────────────
    code_tools = CodeTools(
        base_dir=str(base_dir),
    )

    # ── Personality ───────────────────────────────────────────────────
    # Edit these instructions to define your agent's personality.
    # Each string is one instruction the agent follows.
    instructions = [
        "You are a helpful assistant. Be concise, friendly, and accurate.",
        "When you're not sure about something, say so honestly.",
    ]

    # ── Team ──────────────────────────────────────────────────────────
    team = Team(
        name="Starter Agent",
        instructions=instructions,
        members=[],
        model=primary_model(),
        db=db,
        # ── Context window ──────────────────────────────────────────────
        add_history_to_context=True,
        add_datetime_to_context=True,
        timezone_identifier="UTC",
        add_location_to_context=True,
        num_history_runs=20,
        # ── Run containment ─────────────────────────────────────────────
        tool_call_limit=25,
        cache_session=True,
        tools=[
            code_tools,
            CloneTools(),
        ],
        skills=skills,
        markdown=True,
    )

    return team
