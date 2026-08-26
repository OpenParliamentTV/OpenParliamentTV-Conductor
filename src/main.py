"""FastAPI app entrypoint."""

from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from src import __commit__, __version__
from src.api.jobs import router as jobs_router
from src.api.parliaments import router as parliaments_router
from src.api.schedules import router as schedules_router
from src.api.sessions import router as sessions_router
from src.api.websocket import router as websocket_router
from src.auth.github import router as auth_router
from src.web.pages import router as pages_router
from src.config import get_config
from src.services.job_manager import JobManager
from src.services.log_streamer import LogStreamer
from src.services.notifier import SlackNotifier
from src.services.registry import registry
from src.services.scheduler import SchedulerService
from src.services.worker import Worker
from src.workflow.runner import WorkflowRunner


# Process start time — reported by /health so a deploy can be confirmed from
# outside: self-update recreates the container, which resets this.
_STARTED_AT = datetime.now(tz=timezone.utc).isoformat()


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = get_config()
    _configure_logging(config.settings.log_level)
    config.validate_startup()

    status_dir = Path(config.status_dir)
    registry.job_manager = JobManager(status_dir)
    registry.log_streamer = LogStreamer(status_dir)
    registry.notifier = SlackNotifier(
        webhook_url=config.settings.slack_webhook_url,
        base_url=config.settings.base_url,
        config=config.notifications.slack,
    )
    registry.runner = WorkflowRunner(
        config=config,
        job_manager=registry.job_manager,
        log_streamer=registry.log_streamer,
        notifier=registry.notifier,
    )
    registry.worker = Worker(registry.job_manager, registry.runner)
    registry.scheduler = SchedulerService(config, registry.job_manager)

    logging.getLogger(__name__).info(
        "openparliamenttv-conductor starting with %d parliament(s), %d user(s), %d schedule(s)",
        len(config.parliaments),
        len(config.users),
        len(config.schedules),
    )
    await registry.worker.start()
    await registry.scheduler.start()
    try:
        yield
    finally:
        await registry.scheduler.stop()
        await registry.worker.stop()


app = FastAPI(title="OpenParliamentTV-Conductor", lifespan=lifespan)

_config = get_config()
app.add_middleware(
    SessionMiddleware,
    secret_key=_config.settings.jwt_secret or secrets.token_hex(32),
    same_site="lax",
    https_only=_config.settings.base_url.startswith("https://"),
)

_static_dir = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

app.include_router(auth_router)
app.include_router(parliaments_router)
app.include_router(sessions_router)
app.include_router(jobs_router)
app.include_router(schedules_router)
app.include_router(websocket_router)
app.include_router(pages_router)


@app.get("/health")
async def health() -> dict:
    """Liveness plus what you need to verify a deploy from outside.

    `commit` is the git SHA the image was built from, or null when nothing
    stamped it. `version` is the hand-set fallback that answers "did my push
    land?" even on an unstamped image. `started_at` is when this process began,
    i.e. when the container was last recreated, which is what
    `scripts/self-update.sh` does after a pull. All three are readable in a
    browser without authentication, which matters when the only access to a
    deployment is its URL.
    """
    return {
        "status": "healthy",
        "commit": __commit__,
        "version": __version__,
        "started_at": _STARTED_AT,
    }
