from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.auth.dependencies import require_role
from src.config import AppConfig, ParliamentConfig, get_config, stage_disable_reasons
from src.services.job_manager import Job, JobManager
from src.services.registry import get_job_manager
from src.services.session_content import get_session_content
from src.services.status_tracker import get_tracker

router = APIRouter(prefix="/api/parliaments/{parliament_id}/sessions", tags=["sessions"])


class SessionRerun(BaseModel):
    stages: list[str] = Field(..., min_length=1)
    force: bool = False


class DateRangeRerun(BaseModel):
    date_from: str | None = None
    date_to: str | None = None
    stages: list[str] = Field(..., min_length=1)
    force: bool = False
    period: int | None = None


def _resolve(parliament_id: str, config: AppConfig) -> ParliamentConfig:
    parliament = config.parliaments.get(parliament_id)
    if not parliament:
        raise HTTPException(status_code=404, detail=f"Unknown parliament {parliament_id}")
    return parliament


def _check_runnable(parliament: ParliamentConfig, config: AppConfig, stages: list[str]) -> None:
    """Reject stages that are unknown or not runnable for this parliament/deployment
    (disabled in parliaments.yaml, unsupported, or NER without an endpoint)."""
    reasons = stage_disable_reasons(parliament, config.settings)
    bad = {
        s: (reasons[s] if s in reasons else "unknown stage")
        for s in stages
        if s not in reasons or reasons[s]
    }
    if bad:
        detail = "; ".join(f"{s} ({r})" for s, r in bad.items())
        raise HTTPException(status_code=400, detail=f"Stage(s) not runnable: {detail}")


def _period_for_session(parliament_id: str, parliament: ParliamentConfig, session_id: str) -> int:
    """Return the electoral period for a session.

    Prefers the authoritative `electoralPeriod.number` read from the session
    file (correct for any ID format). Falls back to the DE-style ID-prefix
    convention, then `current_period`, when the file is missing or empty.
    """
    period = get_session_content().session_period(parliament_id, parliament, session_id)
    if period is not None:
        return period
    for p in parliament.periods:
        if session_id.startswith(str(p)):
            return p
    return parliament.current_period


@router.get("/{session_id}")
async def get_session(
    parliament_id: str,
    session_id: str,
    config: AppConfig = Depends(get_config),
    _user: dict = Depends(require_role("viewer")),
) -> dict:
    parliament = _resolve(parliament_id, config)
    tracker = get_tracker()
    return {
        "id": session_id,
        "parliament": parliament_id,
        "status": tracker.session_status(parliament_id, parliament, session_id),
    }


@router.get("/{session_id}/summary")
async def session_summary(
    parliament_id: str,
    session_id: str,
    config: AppConfig = Depends(get_config),
    _user: dict = Depends(require_role("viewer")),
) -> dict:
    parliament = _resolve(parliament_id, config)
    summary = get_session_content().summary(parliament_id, parliament, session_id)
    if summary is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return asdict(summary)


@router.get("/{session_id}/content")
async def session_content(
    parliament_id: str,
    session_id: str,
    config: AppConfig = Depends(get_config),
    _user: dict = Depends(require_role("viewer")),
) -> dict:
    parliament = _resolve(parliament_id, config)
    groups = get_session_content().content(parliament_id, parliament, session_id)
    if groups is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"agenda_items": groups}


@router.get("/{session_id}/speeches/{speech_index}")
async def session_speech(
    parliament_id: str,
    session_id: str,
    speech_index: int,
    config: AppConfig = Depends(get_config),
    _user: dict = Depends(require_role("viewer")),
) -> dict:
    parliament = _resolve(parliament_id, config)
    speech = get_session_content().speech(parliament_id, parliament, session_id, speech_index)
    if speech is None:
        raise HTTPException(status_code=404, detail="Speech not found")
    return speech


@router.post("/{session_id}/rerun")
async def rerun_session(
    parliament_id: str,
    session_id: str,
    payload: SessionRerun,
    config: AppConfig = Depends(get_config),
    jm: JobManager = Depends(get_job_manager),
    _user: dict = Depends(require_role("editor")),
) -> dict:
    parliament = _resolve(parliament_id, config)
    _check_runnable(parliament, config, payload.stages)
    job = Job.new(
        parliament=parliament_id,
        stages=payload.stages,
        period=_period_for_session(parliament_id, parliament, session_id),
        session_filter=f"^{session_id}$",
        force=payload.force,
        source="manual",
    )
    position = jm.enqueue(job)
    return {"job_id": job.id, "position": position}


@router.post("/rerun-by-date")
async def rerun_by_date(
    parliament_id: str,
    payload: DateRangeRerun,
    config: AppConfig = Depends(get_config),
    jm: JobManager = Depends(get_job_manager),
    _user: dict = Depends(require_role("editor")),
) -> dict:
    parliament = _resolve(parliament_id, config)
    _check_runnable(parliament, config, payload.stages)
    period = payload.period or parliament.current_period
    matches = get_session_content().sessions_in_range(
        parliament_id, parliament, period, payload.date_from, payload.date_to,
    )
    if not matches:
        raise HTTPException(status_code=400, detail="No sessions match the selected date range")
    import re as _re

    pattern = f"^({'|'.join(_re.escape(s) for s in matches)})$"
    job = Job.new(
        parliament=parliament_id,
        stages=payload.stages,
        period=period,
        session_filter=pattern,
        force=payload.force,
        source="manual",
    )
    position = jm.enqueue(job)
    return {"job_id": job.id, "position": position, "session_count": len(matches)}
