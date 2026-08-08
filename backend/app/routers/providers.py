"""Provider credit/quota endpoints."""

import os

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/providers", tags=["providers"])


@router.get("/zai/quota")
async def get_zai_quota():
    """Fetch ZAI API usage percentage and reset time."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.z.ai/api/monitor/usage/quota/limit",
                headers={"Authorization": f"Bearer {os.environ['ZAI_API_KEY']}"},
            )
        resp.raise_for_status()
        tokens = next(
            e for e in resp.json()["data"]["limits"] if e.get("type") == "TOKENS_LIMIT"
        )
        return {
            "percentage": tokens.get("percentage"),
            "reset_at": tokens.get("nextResetTime"),
        }
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=200)


@router.get("/synthetic/quota")
async def get_synthetic_quota():
    """Fetch Synthetic API credit usage percentage and reset time."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.synthetic.new/v2/quotas",
                headers={"Authorization": f"Bearer {os.environ['SYNTHETIC_API_KEY']}"},
            )
        resp.raise_for_status()
        data = resp.json()
        weekly = data.get("weeklyTokenLimit", {})
        # percentRemaining is remaining (not used), so used = 100 - remaining
        remaining = weekly.get("percentRemaining")
        used_pct = round(100 - remaining, 1) if remaining is not None else None
        reset_at = None
        if weekly.get("nextRegenAt"):
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(weekly["nextRegenAt"].replace("Z", "+00:00"))
            reset_at = int(dt.timestamp() * 1000)
        return {
            "percentage": used_pct,
            "reset_at": reset_at,
        }
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=200)