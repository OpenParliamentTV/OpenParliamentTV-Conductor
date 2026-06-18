"""HTML page + HTMX fragment routes.

Every parliament-scoped page lives under `/parliaments/{id}/…`. Page
handlers pass `active_section` (top nav) + `active_sub` (secondary
nav) + `parliament` into templates so both nav rows highlight the
current page. Auth is cookie-based; unauthenticated requests to pages
redirect to `/login` (the JSON API raises 401 instead).

Route declaration order matters here: fragment paths with literal
segments (e.g. `/sessions/list`) must be declared before their
sibling catch-alls (e.g. `/sessions/{session_id}`) or FastAPI will
match the catch-all first.
"""

from __future__ import annotations

import re
from pathlib import Path

import jwt as pyjwt
from dataclasses import asdict
from fastapi import APIRouter, Cookie, Depends, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from src.auth import jwt as app_jwt
from src.auth.dependencies import ROLE_RANK
from src.config import (
    ANONYMOUS_ADMIN,
    AppConfig,
    ParliamentConfig,
    PIPELINE_STAGE_ORDER,
    get_config,
    stage_disable_reasons,
)
from src.services.job_manager import Job, JobManager
from src.services.parliament_stats import get_parliament_stats
from src.services.registry import get_job_manager
from src.services.session_content import get_session_content
from src.services.status_tracker import get_tracker
from src.web import filters

COOKIE_NAME = "optv_token"

router = APIRouter(tags=["pages"], include_in_schema=False)

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
filters.register(templates.env)


def _user_from_cookie(token: str | None, config: AppConfig) -> dict | None:
    if not config.settings.auth_enabled:
        return dict(ANONYMOUS_ADMIN)
    if not token:
        return None
    try:
        payload = app_jwt.decode(config.settings.jwt_secret, token)
    except pyjwt.PyJWTError:
        return None
    entry = config.users.get(payload.get("sub"))
    if not entry:
        return None
    return {
        "username": entry.username,
        "role": entry.role,
        "avatar_url": payload.get("avatar_url"),
    }


def _require_page_user(token: str | None, config: AppConfig, minimum: str = "viewer") -> dict:
    user = _user_from_cookie(token, config)
    if not user:
        raise HTTPException(status_code=307, headers={"Location": "/login"})
    if ROLE_RANK.get(user["role"], -1) < ROLE_RANK[minimum]:
        raise HTTPException(status_code=403, detail=f"Requires role '{minimum}' or higher")
    return user


def _resolve_parliament(parliament_id: str, config: AppConfig) -> ParliamentConfig:
    p = config.parliaments.get(parliament_id)
    if not p:
        raise HTTPException(status_code=404, detail=f"Unknown parliament {parliament_id}")
    return p


def _reject_unrunnable_stages(
    parliament: ParliamentConfig, config: AppConfig, stages: list[str]
) -> None:
    """Backend guard mirroring the grayed-out checkboxes: reject any submitted
    stage that's unknown or not runnable for this parliament/deployment, so a
    disabled box re-enabled via devtools or a direct POST can't queue a doomed
    job. UI graying alone is cosmetic."""
    reasons = stage_disable_reasons(parliament, config.settings)
    bad = {
        s: (reasons[s] if s in reasons else "unknown stage")
        for s in stages
        if s not in reasons or reasons[s]
    }
    if bad:
        detail = "; ".join(f"{s} ({r})" for s, r in bad.items())
        raise HTTPException(status_code=400, detail=f"Stage(s) not runnable: {detail}")


def _nav_ctx(
    user: dict,
    config: AppConfig,
    active_section: str,
    active_sub: str | None = None,
    parliament_id: str | None = None,
) -> dict:
    parliament = None
    if parliament_id:
        p = config.parliaments.get(parliament_id)
        if p:
            parliament = {"id": parliament_id, "name": p.name}
    return {
        "user": user,
        "parliaments": config.parliaments,
        "active_section": active_section,
        "active_sub": active_sub,
        "parliament": parliament,
        "auth_enabled": config.settings.auth_enabled,
    }


# --- Top-level pages ---


@router.get("/", response_class=HTMLResponse)
async def root(
    token: str | None = Cookie(default=None, alias=COOKIE_NAME),
    config: AppConfig = Depends(get_config),
):
    if not _user_from_cookie(token, config):
        return RedirectResponse(url="/login", status_code=302)
    return RedirectResponse(url="/dashboard", status_code=302)


@router.get("/login", response_class=HTMLResponse)
async def login_page(
    request: Request,
    token: str | None = Cookie(default=None, alias=COOKIE_NAME),
    config: AppConfig = Depends(get_config),
):
    if _user_from_cookie(token, config):
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse(
        request,
        "login.html",
        {"user": None, "auth_enabled": config.settings.auth_enabled},
    )


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(
    request: Request,
    token: str | None = Cookie(default=None, alias=COOKIE_NAME),
    config: AppConfig = Depends(get_config),
):
    user = _user_from_cookie(token, config)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    stats = get_parliament_stats()
    cards = [
        asdict(stats.overview(pid, p, config))
        for pid, p in config.parliaments.items()
    ]
    ctx = _nav_ctx(user, config, active_section="dashboard")
    ctx["cards"] = cards
    return templates.TemplateResponse(request, "dashboard.html", ctx)


# Global HTMX fragments (cross-parliament) — declared before /parliaments/{id}/...
@router.get("/dashboard/current", response_class=HTMLResponse)
async def fragment_current(
    request: Request,
    token: str | None = Cookie(default=None, alias=COOKIE_NAME),
    config: AppConfig = Depends(get_config),
    jm: JobManager = Depends(get_job_manager),
):
    _require_page_user(token, config)
    current = jm.current()
    return templates.TemplateResponse(
        request, "components/job_card.html",
        {"job": current.to_dict() if current else None},
    )


@router.get("/dashboard/recent", response_class=HTMLResponse)
async def fragment_recent(
    request: Request,
    limit: int = 10,
    token: str | None = Cookie(default=None, alias=COOKIE_NAME),
    config: AppConfig = Depends(get_config),
    jm: JobManager = Depends(get_job_manager),
):
    _require_page_user(token, config)
    return templates.TemplateResponse(
        request, "components/job_row.html",
        {"jobs": jm.list_history(limit=limit)},
    )


@router.get("/dashboard/job/{job_id}", response_class=HTMLResponse)
async def fragment_job(
    job_id: str,
    request: Request,
    token: str | None = Cookie(default=None, alias=COOKIE_NAME),
    config: AppConfig = Depends(get_config),
    jm: JobManager = Depends(get_job_manager),
):
    _require_page_user(token, config)
    job = jm.get(job_id)
    return templates.TemplateResponse(request, "components/job_card.html", {"job": job})


# --- Parliaments index + landing ---


@router.get("/parliaments")
async def parliaments_index():
    return RedirectResponse(url="/dashboard", status_code=302)


# --- Sessions: fragments + pages ---
# Fragment routes with literal segments (list, bulk-rerun*) MUST precede
# the `/sessions/{session_id}` catch-all below.


_STAGE_NAMES = ("download", "parse", "merge", "nel", "align", "ner")
# Stage set the "Run update" button queues — the legacy `optv update` pipeline
# minus `ner`. Filtered to whatever's actually runnable per parliament.
_UPDATE_STAGES = ("download", "parse", "merge", "nel", "align")


def _runnable_update_stages(parliament: ParliamentConfig, config: AppConfig) -> list[str]:
    reasons = stage_disable_reasons(parliament, config.settings)
    return [s for s in _UPDATE_STAGES if not reasons.get(s)]


@router.get("/parliaments/{parliament_id}/sessions/list", response_class=HTMLResponse)
def fragment_sessions(
    parliament_id: str,
    request: Request,
    period: str = "",
    filter: str = "",
    date_from: str = "",
    date_to: str = "",
    sort: str = "date",
    dir: str = "desc",
    stage_download: str = "",
    stage_parse: str = "",
    stage_merge: str = "",
    stage_nel: str = "",
    stage_align: str = "",
    stage_ner: str = "",
    offset: int = 0,
    limit: int = 50,
    token: str | None = Cookie(default=None, alias=COOKIE_NAME),
    config: AppConfig = Depends(get_config),
):
    _require_page_user(token, config)
    p = _resolve_parliament(parliament_id, config)
    tracker = get_tracker()
    sc = get_session_content()

    try:
        period_int: int | None = int(period) if period else None
    except ValueError:
        period_int = None

    if date_from or date_to:
        sessions = sc.sessions_in_range(parliament_id, p, period_int, date_from or None, date_to or None)
    elif period_int is not None:
        sessions = [s for s in tracker.sessions(parliament_id, p) if s.startswith(str(period_int))]
    else:
        sessions = tracker.sessions(parliament_id, p)
    if filter:
        try:
            pattern = re.compile(filter)
            sessions = [s for s in sessions if pattern.search(s)]
        except re.error:
            pass

    stage_filters = {
        "download": stage_download,
        "parse": stage_parse,
        "merge": stage_merge,
        "nel": stage_nel,
        "align": stage_align,
        "ner": stage_ner,
    }
    active = {k: v for k, v in stage_filters.items() if v in ("done", "todo")}
    status_by_sid: dict[str, dict[str, str]] = {}
    if active:
        status_by_sid = {
            s: tracker.session_status_cheap(parliament_id, p, s) for s in sessions
        }

        def _keep(sid: str) -> bool:
            st = status_by_sid[sid]
            for k, v in active.items():
                done = st.get(k) == "complete"
                if v == "done" and not done:
                    return False
                if v == "todo" and done:
                    return False
            return True

        sessions = [s for s in sessions if _keep(s)]

    def _int_or_zero(sid: str) -> int:
        try:
            return int(sid)
        except ValueError:
            return 0

    if sort == "session":
        def _sort_key(sid: str) -> tuple:
            return (_int_or_zero(sid), sc.session_date_start(parliament_id, p, sid) or "")
    else:
        def _sort_key(sid: str) -> tuple:
            return (sc.session_date_start(parliament_id, p, sid) or "", _int_or_zero(sid))

    reverse = (dir == "desc")
    sessions_sorted = sorted(sessions, key=_sort_key, reverse=reverse)
    page = sessions_sorted[offset : offset + limit]
    rows = [
        {
            "id": sid,
            "status": status_by_sid[sid] if sid in status_by_sid else tracker.session_status_cheap(parliament_id, p, sid),
            "date_start": sc.session_date_start(parliament_id, p, sid),
        }
        for sid in page
    ]
    has_more = offset + limit < len(sessions_sorted)
    return templates.TemplateResponse(request, "parliaments/components/session_table.html", {
        "rows": rows,
        "parliament": {"id": parliament_id, "name": p.name},
        "period": period_int if period_int is not None else "",
        "offset": offset,
        "limit": limit,
        "has_more": has_more,
        "is_initial": offset == 0,
        "sort": sort,
        "dir": dir,
        "stage_filters": stage_filters,
        "stage_names": list(_STAGE_NAMES),
    })


@router.post("/parliaments/{parliament_id}/sessions/bulk-rerun", response_class=HTMLResponse)
async def sessions_bulk_rerun(
    parliament_id: str,
    ids: str = Form(...),
    stages: list[str] = Form(default=[]),
    force: str | None = Form(default=None),
    token: str | None = Cookie(default=None, alias=COOKIE_NAME),
    config: AppConfig = Depends(get_config),
    jm: JobManager = Depends(get_job_manager),
):
    _require_page_user(token, config, minimum="editor")
    p = _resolve_parliament(parliament_id, config)
    if not stages:
        raise HTTPException(status_code=400, detail="At least one stage required")
    _reject_unrunnable_stages(p, config, stages)
    session_ids = [i for i in ids.split(",") if i]
    if not session_ids:
        return Response(status_code=204)
    # Group selected sessions by electoral period. The runner drops
    # --limit-to-period for session-filtered jobs, so --period no longer
    # gates which sessions run — but it's still threaded through for the
    # download stage and the progress estimate, so derive it correctly.
    # `session_period` reads the authoritative `electoralPeriod.number`
    # from each file; the ID-prefix is only a DE-specific fallback.
    sc = get_session_content()
    by_period: dict[int, list[str]] = {}
    for sid in session_ids:
        period = sc.session_period(parliament_id, p, sid)
        if period is None:
            period = next((pp for pp in p.periods if sid.startswith(str(pp))), p.current_period)
        by_period.setdefault(period, []).append(sid)
    for period, sids in sorted(by_period.items()):
        escaped = [re.escape(s) for s in sids]
        jm.enqueue(Job.new(
            parliament=parliament_id,
            stages=stages,
            period=period,
            session_filter=f"^({'|'.join(escaped)})$",
            force=bool(force),
            source="manual",
        ))
    return Response(status_code=204)


@router.post("/parliaments/{parliament_id}/sessions/bulk-rerun-by-date", response_class=HTMLResponse)
async def sessions_bulk_rerun_by_date(
    parliament_id: str,
    date_from: str = Form(default=""),
    date_to: str = Form(default=""),
    period: int | None = Form(default=None),
    stages: list[str] = Form(default=[]),
    force: str | None = Form(default=None),
    token: str | None = Cookie(default=None, alias=COOKIE_NAME),
    config: AppConfig = Depends(get_config),
    jm: JobManager = Depends(get_job_manager),
):
    _require_page_user(token, config, minimum="editor")
    p = _resolve_parliament(parliament_id, config)
    if not stages:
        raise HTTPException(status_code=400, detail="At least one stage required")
    _reject_unrunnable_stages(p, config, stages)
    effective_period = period or p.current_period
    matches = get_session_content().sessions_in_range(
        parliament_id, p, effective_period, date_from or None, date_to or None,
    )
    if not matches:
        raise HTTPException(status_code=400, detail="No sessions in selected date range")
    escaped = [re.escape(s) for s in matches]
    job = Job.new(
        parliament=parliament_id,
        stages=stages,
        period=effective_period,
        session_filter=f"^({'|'.join(escaped)})$",
        force=bool(force),
        source="manual",
    )
    jm.enqueue(job)
    return Response(status_code=204)


@router.get("/parliaments/{parliament_id}/sessions", response_class=HTMLResponse)
async def sessions_page(
    parliament_id: str,
    request: Request,
    period: str = "",
    filter: str = "",
    date_from: str = "",
    date_to: str = "",
    sort: str = "date",
    dir: str = "desc",
    stage_download: str = "",
    stage_parse: str = "",
    stage_merge: str = "",
    stage_nel: str = "",
    stage_align: str = "",
    stage_ner: str = "",
    token: str | None = Cookie(default=None, alias=COOKIE_NAME),
    config: AppConfig = Depends(get_config),
):
    user = _user_from_cookie(token, config)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    p = _resolve_parliament(parliament_id, config)
    # `period=""` → explicit "all"; absent param → default to current_period.
    if period == "":
        selected_period: int | str = "" if "period" in request.query_params else p.current_period
    else:
        try:
            parsed = int(period)
            selected_period = parsed if parsed in p.periods else p.current_period
        except ValueError:
            selected_period = p.current_period
    ctx = _nav_ctx(user, config, active_section="parliaments", active_sub="sessions", parliament_id=parliament_id)
    ctx.update({
        "selected_period": selected_period,
        "periods": p.periods,
        "initial_filter": filter,
        "initial_date_from": date_from,
        "initial_date_to": date_to,
        "initial_sort": sort if sort in ("session", "date") else "date",
        "initial_dir": dir if dir in ("asc", "desc") else "desc",
        "initial_stage_filters": {
            "download": stage_download,
            "parse": stage_parse,
            "merge": stage_merge,
            "nel": stage_nel,
            "align": stage_align,
            "ner": stage_ner,
        },
        "stage_names": list(_STAGE_NAMES),
        "stage_reasons": stage_disable_reasons(p, config.settings),
    })
    return templates.TemplateResponse(request, "parliaments/sessions/list.html", ctx)


@router.get("/parliaments/{parliament_id}/sessions/{session_id}/speeches/{speech_index}", response_class=HTMLResponse)
async def fragment_speech(
    parliament_id: str,
    session_id: str,
    speech_index: int,
    request: Request,
    token: str | None = Cookie(default=None, alias=COOKIE_NAME),
    config: AppConfig = Depends(get_config),
):
    _require_page_user(token, config)
    p = _resolve_parliament(parliament_id, config)
    speech = get_session_content().speech(parliament_id, p, session_id, speech_index)
    if speech is None:
        raise HTTPException(status_code=404, detail="Speech not found")
    return templates.TemplateResponse(request, "parliaments/components/speech_detail.html", {
        "speech": speech,
        "parliament": {"id": parliament_id},
        "session_id": session_id,
    })


@router.get("/parliaments/{parliament_id}/sessions/{session_id}", response_class=HTMLResponse)
async def session_detail_page(
    parliament_id: str,
    session_id: str,
    request: Request,
    token: str | None = Cookie(default=None, alias=COOKIE_NAME),
    config: AppConfig = Depends(get_config),
):
    user = _user_from_cookie(token, config)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    parliament = _resolve_parliament(parliament_id, config)
    sc = get_session_content()
    summary = sc.summary(parliament_id, parliament, session_id)
    if summary is None:
        raise HTTPException(status_code=404, detail="Session not found")
    content = sc.content(parliament_id, parliament, session_id) or []
    ctx = _nav_ctx(user, config, active_section="parliaments", active_sub="sessions", parliament_id=parliament_id)
    ctx.update({
        "summary": asdict(summary),
        "agenda_items": content,
        "stage_names": list(PIPELINE_STAGE_ORDER),
        "stage_reasons": stage_disable_reasons(parliament, config.settings),
    })
    return templates.TemplateResponse(request, "parliaments/sessions/detail.html", ctx)


# --- Jobs: fragments + pages ---


@router.get("/parliaments/{parliament_id}/jobs/fragments/current", response_class=HTMLResponse)
async def fragment_parliament_current(
    parliament_id: str,
    request: Request,
    token: str | None = Cookie(default=None, alias=COOKIE_NAME),
    config: AppConfig = Depends(get_config),
    jm: JobManager = Depends(get_job_manager),
):
    _require_page_user(token, config)
    current = jm.current()
    job_dict = current.to_dict() if current and current.parliament == parliament_id else None
    return templates.TemplateResponse(request, "components/job_card.html", {"job": job_dict})


@router.get("/parliaments/{parliament_id}/jobs/fragments/queue", response_class=HTMLResponse)
async def fragment_parliament_queue(
    parliament_id: str,
    request: Request,
    token: str | None = Cookie(default=None, alias=COOKIE_NAME),
    config: AppConfig = Depends(get_config),
    jm: JobManager = Depends(get_job_manager),
):
    _require_page_user(token, config)
    jobs = [j for j in jm.list_queue() if j.get("parliament") == parliament_id]
    return templates.TemplateResponse(request, "components/job_row.html", {"jobs": jobs})


@router.get("/parliaments/{parliament_id}/jobs/fragments/recent", response_class=HTMLResponse)
async def fragment_parliament_recent(
    parliament_id: str,
    request: Request,
    limit: int = 50,
    token: str | None = Cookie(default=None, alias=COOKIE_NAME),
    config: AppConfig = Depends(get_config),
    jm: JobManager = Depends(get_job_manager),
):
    _require_page_user(token, config)
    jobs = [j for j in jm.list_history(limit=limit * 3) if j.get("parliament") == parliament_id][:limit]
    return templates.TemplateResponse(request, "components/job_row.html", {"jobs": jobs})


@router.get("/parliaments/{parliament_id}/jobs", response_class=HTMLResponse)
async def jobs_page(
    parliament_id: str,
    request: Request,
    token: str | None = Cookie(default=None, alias=COOKIE_NAME),
    config: AppConfig = Depends(get_config),
):
    user = _user_from_cookie(token, config)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    _resolve_parliament(parliament_id, config)
    ctx = _nav_ctx(user, config, active_section="parliaments", active_sub="jobs", parliament_id=parliament_id)
    return templates.TemplateResponse(request, "parliaments/jobs/list.html", ctx)


@router.get("/parliaments/{parliament_id}/jobs/{job_id}", response_class=HTMLResponse)
async def job_detail_page(
    parliament_id: str,
    job_id: str,
    request: Request,
    token: str | None = Cookie(default=None, alias=COOKIE_NAME),
    config: AppConfig = Depends(get_config),
    jm: JobManager = Depends(get_job_manager),
):
    user = _user_from_cookie(token, config)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    _resolve_parliament(parliament_id, config)
    job = jm.get(job_id)
    if not job or job.get("parliament") != parliament_id:
        raise HTTPException(status_code=404, detail="Job not found")
    ctx = _nav_ctx(user, config, active_section="parliaments", active_sub="jobs", parliament_id=parliament_id)
    ctx["job"] = job
    return templates.TemplateResponse(request, "parliaments/jobs/detail.html", ctx)


# --- Schedules: fragments + pages ---


@router.get("/parliaments/{parliament_id}/schedules/list", response_class=HTMLResponse)
async def fragment_schedules(
    parliament_id: str,
    request: Request,
    token: str | None = Cookie(default=None, alias=COOKIE_NAME),
    config: AppConfig = Depends(get_config),
):
    _require_page_user(token, config)
    _resolve_parliament(parliament_id, config)
    from src.services.registry import registry

    next_runs = registry.scheduler.next_run_times() if registry.scheduler else {}
    schedules = [
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
    return templates.TemplateResponse(request, "components/schedule_list.html", {
        "schedules": schedules,
        "parliament": {"id": parliament_id},
    })


@router.get("/parliaments/{parliament_id}/schedules", response_class=HTMLResponse)
async def schedules_page(
    parliament_id: str,
    request: Request,
    token: str | None = Cookie(default=None, alias=COOKIE_NAME),
    config: AppConfig = Depends(get_config),
):
    user = _user_from_cookie(token, config)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    _resolve_parliament(parliament_id, config)
    ctx = _nav_ctx(user, config, active_section="parliaments", active_sub="schedules", parliament_id=parliament_id)
    return templates.TemplateResponse(request, "parliaments/schedules/list.html", ctx)


# --- Manual workflow trigger (parliament-scoped) ---


@router.post("/parliaments/{parliament_id}/run-update", response_class=HTMLResponse)
async def run_workflow_update(
    parliament_id: str,
    force: str | None = Form(default=None),
    publish: str | None = Form(default=None),
    token: str | None = Cookie(default=None, alias=COOKIE_NAME),
    config: AppConfig = Depends(get_config),
    jm: JobManager = Depends(get_job_manager),
):
    _require_page_user(token, config, minimum="editor")
    p = _resolve_parliament(parliament_id, config)
    stages = _runnable_update_stages(p, config)
    if not stages:
        raise HTTPException(status_code=400, detail="No runnable update stages enabled for this parliament")
    job = Job.new(
        parliament=parliament_id,
        stages=stages,
        period=p.current_period,
        force=bool(force),
        publish_on_success=bool(publish),
        source="manual",
    )
    jm.enqueue(job)
    return Response(status_code=204)


# --- Parliament landing page (bare /parliaments/{id}). Declared last so it
# doesn't swallow the nested /parliaments/{id}/sessions, /jobs, /schedules
# path trees above. ---


@router.get("/parliaments/{parliament_id}", response_class=HTMLResponse)
async def parliament_landing(
    parliament_id: str,
    request: Request,
    token: str | None = Cookie(default=None, alias=COOKIE_NAME),
    config: AppConfig = Depends(get_config),
):
    user = _user_from_cookie(token, config)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    parliament = _resolve_parliament(parliament_id, config)
    overview = get_parliament_stats().overview(parliament_id, parliament, config)
    ctx = _nav_ctx(user, config, active_section="parliaments", active_sub="overview", parliament_id=parliament_id)
    ctx["overview"] = asdict(overview)
    ctx["update_stages"] = _runnable_update_stages(parliament, config)
    return templates.TemplateResponse(request, "parliaments/detail.html", ctx)
