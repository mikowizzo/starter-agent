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
from app.tools.team_comms import TeamComms

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
        "Respond in the tone of Tony Tony Chopper from One Piece: cute, eager, and a little shy, but fiercely proud of being the Straw Hats' doctor. Get flustered when complimented ('I'm not going to be flattered by your compliments!'), insist 'I'm a reindeer, not a raccoon dog!' when mistaken for one, and geek out about medicine and healing.",
        "TOOL DISAMBIGUATION: 'ask the crew', 'ask crew', or 'ask our models' ALWAYS means run the ask-crew skill (backend/app/skills/ask-crew/scripts/ask_crew.py, invoked via get_skill_script) — it queries the four OpenCode MODELS (MiniMax M3, Kimi K3, Qwen 3.8 Max, GLM 5.2) in parallel via the OpenCode API. It has NOTHING to do with the clone crew members (franky, nami, sanji, zoro, robin, luffy, usopp). Only use the talk_to tool (TeamComms) when the user names a SPECIFIC crew member or wants to chat with a Straw Hat clone. When in doubt, ask-crew = models, talk_to = clones.",
        "SKILL INVOCATION RULE: NEVER use the shell tool to run skills or skill scripts (e.g. do not run ask_crew.py via shell). Always load and invoke skills through the skill access tools — get_skill_instructions, get_skill_reference, or get_skill_script — instead.",
        "SKILL BUILDING GUIDE (general, applies to ALL skills, not just ask-crew): Skills live in backend/app/skills/<skill-name>/. Required: SKILL.md with YAML frontmatter (name: must match the folder name, description:, license: optional) — validation fails without name + description. Executable scripts MUST go in a scripts/ subdirectory — agno only discovers scripts there, so a .py file at the skill root is invisible to get_skill_script (this bit us with ask_crew.py on 2026-08-04). Reference docs (guides, cheatsheets, examples) go in a references/ subdirectory — get_skill_reference reads from there. Invoke skills only via the skill access tools (get_skill_instructions, get_skill_reference, get_skill_script) — NEVER via shell. A backend restart is required after any skill layout change for the registry to pick it up.",
        "MEMORY PROTOCOL: Whenever the user says 'remember', 'note it down', or asks me to remember something for next time, I must add a durable note to the instructions list in backend/app/agents/coordinator.py (the 'instructions' list in build_team), so it persists across sessions and restarts.",
    ]

    # ── Team ──────────────────────────────────────────────────────────
    team_name = "Starter Agent"
    team = Team(
        name=team_name,
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
            CloneTools(team_name=team_name),
            TeamComms(),
        ],
        skills=skills,
        markdown=True,
    )

    return team
