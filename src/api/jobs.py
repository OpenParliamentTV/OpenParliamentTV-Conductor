from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.auth.dependencies import require_role
from src.config import AppConfig, get_config, stage_disable_reasons
from src.services.job_manager import Job, JobManager
from src.services.log_streamer import LogStreamer
from src.services.registry import get_job_manager, get_log_streamer

router = APIRouter(prefix="/api/parliaments/{parliament_id}/jobs", tags=["jobs"])


class JobCreate(BaseModel):
    period: int | None = None
    stages: list[str] = Field(..., min_length=1)
    session_filter: str | None = None
    force: bool = False
    rebuild: bool = False  # re-derive from scratch (implies force in Tools)
    publish_on_success: bool = False


def _parliament_jobs(jm: JobManager, parliament_id: str, include_queue: bool, limit: int) -> dict:
    current = jm.current()
    out: dict = {}
    if current and current.parliament == parliament_id:
        out["current"] = current.to_dict()
    else:
        out["current"] = None
    if include_queue:
        out["queue"] = [j for j in jm.list_queue() if j.get("parliament") == parliament_id]
    out["recent"] = [j for j in jm.list_history(limit=limit * 3) if j.get("parliament") == parliament_id][:limit]
    return out


@router.post("")
async def create_job(
    parliament_id: str,
    payload: JobCreate,
    config: AppConfig = Depends(get_config),
    jm: JobManager = Depends(get_job_manager),
    user: dict = Depends(require_role("editor")),
) -> dict:
    if parliament_id not in config.parliaments:
        raise HTTPException(status_code=404, detail=f"Unknown parliament {parliament_id}")
    valid = {"download", "parse", "merge", "nel", "align", "ner", "publish"}
    bad = [s for s in payload.stages if s not in valid]
    if bad:
        raise HTTPException(status_code=400, detail=f"Unknown stages: {bad}")
    # `publish` is handled separately by the runner (git push) and isn't gated by
    # parliaments.yaml `stages:` — only the pipeline stages get the runnable check.
    reasons = stage_disable_reasons(config.parliaments[parliament_id], config.settings)
    not_runnable = {s: reasons[s] for s in payload.stages if reasons.get(s)}
    if not_runnable:
        detail = "; ".join(f"{s} ({r})" for s, r in not_runnable.items())
        raise HTTPException(status_code=400, detail=f"Stage(s) not runnable: {detail}")
    job = Job.new(
        parliament=parliament_id,
        stages=payload.stages,
        period=payload.period,
        session_filter=payload.session_filter,
        force=payload.force,
        rebuild=payload.rebuild,
        publish_on_success=payload.publish_on_success,
        source="manual",
    )
    position = jm.enqueue(job)
    return {"job_id": job.id, "position": position}


@router.get("")
async def list_jobs(
    parliament_id: str,
    limit: int = 20,
    config: AppConfig = Depends(get_config),
    jm: JobManager = Depends(get_job_manager),
    _user: dict = Depends(require_role("viewer")),
) -> dict:
    if parliament_id not in config.parliaments:
        raise HTTPException(status_code=404, detail=f"Unknown parliament {parliament_id}")
    return _parliament_jobs(jm, parliament_id, include_queue=True, limit=limit)


@router.get("/current")
async def get_current(
    parliament_id: str,
    jm: JobManager = Depends(get_job_manager),
    _user: dict = Depends(require_role("viewer")),
) -> dict:
    current = jm.current()
    if current and current.parliament == parliament_id:
        return current.to_dict()
    return {}


@router.post("/queue/clear")
async def clear_queue(
    parliament_id: str,
    config: AppConfig = Depends(get_config),
    jm: JobManager = Depends(get_job_manager),
    _user: dict = Depends(require_role("editor")),
) -> dict:
    if parliament_id not in config.parliaments:
        raise HTTPException(status_code=404, detail=f"Unknown parliament {parliament_id}")
    removed = jm.clear_queue(parliament=parliament_id)
    return {"removed": removed}


@router.post("/history/clear")
async def clear_history(
    parliament_id: str,
    config: AppConfig = Depends(get_config),
    jm: JobManager = Depends(get_job_manager),
    _user: dict = Depends(require_role("editor")),
) -> dict:
    if parliament_id not in config.parliaments:
        raise HTTPException(status_code=404, detail=f"Unknown parliament {parliament_id}")
    deleted = jm.clear_history(parliament=parliament_id)
    return {"deleted": deleted}


@router.get("/{job_id}")
async def get_job(
    parliament_id: str,
    job_id: str,
    jm: JobManager = Depends(get_job_manager),
    _user: dict = Depends(require_role("viewer")),
) -> dict:
    job = jm.get(job_id)
    if not job or job.get("parliament") != parliament_id:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.post("/{job_id}/cancel")
async def cancel_job(
    parliament_id: str,
    job_id: str,
    jm: JobManager = Depends(get_job_manager),
    _user: dict = Depends(require_role("editor")),
) -> dict:
    job = jm.get(job_id)
    if not job or job.get("parliament") != parliament_id:
        raise HTTPException(status_code=404, detail="Job not found")
    cancelled_immediately = jm.cancel(job_id)
    return {"cancelled": True, "immediate": cancelled_immediately}


@router.get("/{job_id}/logs")
async def get_job_logs(
    parliament_id: str,
    job_id: str,
    streamer: LogStreamer = Depends(get_log_streamer),
    jm: JobManager = Depends(get_job_manager),
    _user: dict = Depends(require_role("viewer")),
) -> dict:
    job = jm.get(job_id)
    if not job or job.get("parliament") != parliament_id:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"logs": streamer.read(job_id)}
