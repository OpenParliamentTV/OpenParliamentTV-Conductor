from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from src.auth.dependencies import require_role
from src.config import AppConfig, get_config, save_schedules
from src.services.registry import registry

router = APIRouter(prefix="/api/parliaments/{parliament_id}/schedules", tags=["schedules"])


class EnableBody(BaseModel):
    enabled: bool


@router.get("")
async def list_schedules(
    parliament_id: str,
    config: AppConfig = Depends(get_config),
    _user: dict = Depends(require_role("viewer")),
) -> dict:
    if parliament_id not in config.parliaments:
        raise HTTPException(status_code=404, detail=f"Unknown parliament {parliament_id}")
    next_runs = registry.scheduler.next_run_times() if registry.scheduler else {}
    return {
        "schedules": [
            {
                "id": sid,
                "enabled": sched.enabled,
                "parliament": sched.parliament,
                "cron": sched.cron,
                "stages": sched.stages,
                "description": sched.description,
                "publish_on_success": sched.publish_on_success,
                "next_run": next_runs.get(sid),
            }
            for sid, sched in config.schedules.items()
            if sched.parliament == parliament_id
        ]
    }


@router.post("/{schedule_id}/enable")
async def enable_schedule(
    parliament_id: str,
    schedule_id: str,
    body: EnableBody,
    config: AppConfig = Depends(get_config),
    _user: dict = Depends(require_role("admin")),
) -> dict:
    sched = config.schedules.get(schedule_id)
    if not sched or sched.parliament != parliament_id:
        raise HTTPException(status_code=404, detail="Unknown schedule")
    sched.enabled = body.enabled
    try:
        save_schedules(config)
    except OSError as exc:
        # Persisting failed (e.g. config mounted read-only). Roll back the
        # in-memory flag so the UI doesn't show a pause that won't survive a
        # restart, and surface the cause instead of a misleading success.
        sched.enabled = not body.enabled
        raise HTTPException(
            status_code=500,
            detail=f"Could not persist schedule change to schedules.yaml: {exc}",
        )
    if registry.scheduler:
        registry.scheduler.sync_jobs()
    return {"id": schedule_id, "enabled": sched.enabled}


@router.post("/{schedule_id}/trigger")
async def trigger_schedule(
    parliament_id: str,
    schedule_id: str,
    config: AppConfig = Depends(get_config),
    _user: dict = Depends(require_role("editor")),
) -> dict:
    sched = config.schedules.get(schedule_id)
    if not sched or sched.parliament != parliament_id:
        raise HTTPException(status_code=404, detail="Unknown schedule")
    if not registry.scheduler or not registry.scheduler.trigger_now(schedule_id):
        raise HTTPException(status_code=404, detail="Unknown schedule")
    return {"triggered": schedule_id}
