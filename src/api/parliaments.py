from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException

from src.auth.dependencies import require_role
from src.config import AppConfig, get_config
from src.services.parliament_stats import get_parliament_stats
from src.services.status_tracker import get_tracker

router = APIRouter(prefix="/api", tags=["parliaments"])


@router.get("/parliaments")
async def list_parliaments(
    config: AppConfig = Depends(get_config),
    _user: dict = Depends(require_role("viewer")),
) -> dict:
    return {
        "parliaments": [
            {
                "id": pid,
                "name": p.name,
                "language": p.language,
                "current_period": p.current_period,
                "periods": p.periods,
            }
            for pid, p in config.parliaments.items()
        ]
    }


@router.get("/parliaments/{parliament_id}/stats")
async def parliament_stats(
    parliament_id: str,
    config: AppConfig = Depends(get_config),
    _user: dict = Depends(require_role("viewer")),
) -> dict:
    parliament = config.parliaments.get(parliament_id)
    if not parliament:
        raise HTTPException(status_code=404, detail=f"Unknown parliament {parliament_id}")
    overview = get_parliament_stats().overview(parliament_id, parliament, config)
    return asdict(overview)


@router.get("/parliaments/{parliament_id}/periods/{period}/sessions")
async def list_sessions(
    parliament_id: str,
    period: int,
    config: AppConfig = Depends(get_config),
    _user: dict = Depends(require_role("viewer")),
) -> dict:
    parliament = config.parliaments.get(parliament_id)
    if not parliament:
        raise HTTPException(status_code=404, detail=f"Unknown parliament {parliament_id}")
    tracker = get_tracker()
    sessions = [s for s in tracker.sessions(parliament_id, parliament) if s.startswith(str(period))]
    return {
        "sessions": [
            {"id": sid, "status": tracker.session_status(parliament_id, parliament, sid)}
            for sid in sessions
        ]
    }
