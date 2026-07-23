"""Lightweight session list/detail endpoints.

Overrides Agno's native GET /sessions and GET /sessions/{id}, which SELECT *
and parse the runs blob (tens of MB for long sessions). These versions select
only the thin columns needed.
"""

import json
import sqlite3
from datetime import datetime, timezone

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.config import DB_PATH

router = APIRouter(tags=["sessions"])


def _to_iso(ts) -> str | None:
    """Convert a Unix timestamp to ISO-8601 UTC string."""
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError):
        return str(ts) if ts else None


@router.get("/sessions")
async def list_sessions(limit: int = 20):
    """List sessions, newest first."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT session_id, session_type, "
            "COALESCE(json_extract(session_data, '$.session_name'), 'Untitled chat') AS session_name, "
            "created_at, COALESCE(updated_at, created_at) AS updated_at "
            "FROM agno_sessions "
            "ORDER BY updated_at DESC "
            "LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()

    return {
        "data": [
            {
                "session_id": row["session_id"],
                "session_name": row["session_name"],
                "session_type": row["session_type"],
                "created_at": _to_iso(row["created_at"]),
                "updated_at": _to_iso(row["updated_at"]),
            }
            for row in rows
        ]
    }


@router.get("/sessions/{session_id}")
async def get_session(session_id: str):
    """Get full session with chat history."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM agno_sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return JSONResponse({"error": "Session not found"}, status_code=404)

    session_data = json.loads(row["session_data"] or "{}")
    runs = json.loads(row["runs"] or "[]")

    chat_history: list[dict] = []
    for run in runs:
        msgs = (
            run.get("run_response", {}).get("messages")
            or run.get("response", {}).get("messages")
            or []
        )
        for msg in msgs:
            role = msg.get("role")
            content = msg.get("content")
            if role in ("user", "assistant") and content is not None:
                chat_history.append({"role": role, "content": content})

    return {
        "session_id": row["session_id"],
        "session_name": session_data.get("session_name") or "Untitled chat",
        "session_type": row["session_type"],
        "created_at": _to_iso(row["created_at"]),
        "updated_at": _to_iso(row["updated_at"]),
        "chat_history": chat_history,
    }


@router.delete("/sessions/{session_id}/runs/last")
async def delete_last_run(session_id: str):
    """Remove the last run (user + assistant exchange) from a session."""
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute(
            "SELECT runs FROM agno_sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            return JSONResponse({"error": "Session not found"}, status_code=404)

        runs = json.loads(row[0] or "[]")
        if not runs:
            return {"session_id": session_id, "remaining_runs": 0}

        runs.pop()
        conn.execute(
            "UPDATE agno_sessions SET runs = ?, updated_at = ? WHERE session_id = ?",
            (json.dumps(runs), datetime.now(timezone.utc).timestamp(), session_id),
        )
        conn.commit()
        return {"session_id": session_id, "remaining_runs": len(runs)}
    finally:
        conn.close()
