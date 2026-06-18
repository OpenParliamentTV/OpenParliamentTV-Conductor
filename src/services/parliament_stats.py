"""Aggregate stats per parliament for index + landing pages.

All fields are cheap by construction: session counts come from directory
listings, date ranges from header-reads of only the first/last session of
each numbering-scheme bucket per period (see `period_date_span`). A 120 s
TTL cache fronts `overview()` so repeat hits on the index/landing pages
don't re-open the same files.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any

from src.config import AppConfig, ParliamentConfig
from src.services.session_content import get_session_content

logger = logging.getLogger(__name__)

_OVERVIEW_TTL = 120.0

_GIT_SSH_RE = re.compile(r"^git@([^:]+):(.+?)(?:\.git)?$")
_GIT_HTTPS_RE = re.compile(r"^(https?://[^/]+/.+?)(?:\.git)?$")


def _git_remote_to_web_url(remote: str) -> str | None:
    """Convert a git remote (SSH or HTTPS) to a browsable HTTPS URL."""
    if not remote:
        return None
    m = _GIT_SSH_RE.match(remote)
    if m:
        return f"https://{m.group(1)}/{m.group(2)}"
    m = _GIT_HTTPS_RE.match(remote)
    if m:
        return m.group(1)
    return None


@dataclass
class PeriodStats:
    period: int
    session_count: int
    date_start: str | None
    date_end: str | None


@dataclass
class ParliamentOverview:
    id: str
    name: str
    language: str
    current_period: int
    periods: list[int]
    period_stats: list[PeriodStats]
    session_count: int
    date_start: str | None
    date_end: str | None
    active_schedules: int
    last_job: dict[str, Any] | None
    git_remote: str
    git_repo_url: str | None


class ParliamentStatsService:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._overview_cache: dict[str, tuple[float, ParliamentOverview]] = {}

    def overview(
        self,
        parliament_id: str,
        parliament: ParliamentConfig,
        config: AppConfig,
    ) -> ParliamentOverview:
        now = time.time()
        with self._lock:
            cached = self._overview_cache.get(parliament_id)
            if cached and now - cached[0] < _OVERVIEW_TTL:
                return cached[1]

        sc = get_session_content()
        period_stats: list[PeriodStats] = []
        total_sessions = 0
        earliest: str | None = None
        latest: str | None = None

        for period in parliament.periods:
            ds, de, count = sc.period_date_span(parliament_id, parliament, period)
            total_sessions += count
            period_stats.append(PeriodStats(
                period=period,
                session_count=count,
                date_start=ds,
                date_end=de,
            ))
            if ds and (earliest is None or ds < earliest):
                earliest = ds
            if de and (latest is None or de > latest):
                latest = de

        active_schedules = sum(
            1 for s in config.schedules.values()
            if s.parliament == parliament_id and s.enabled
        )
        last_job = self._last_job_for_parliament(parliament_id)

        overview = ParliamentOverview(
            id=parliament_id,
            name=parliament.name,
            language=parliament.language,
            current_period=parliament.current_period,
            periods=list(parliament.periods),
            period_stats=sorted(period_stats, key=lambda p: p.period, reverse=True),
            session_count=total_sessions,
            date_start=earliest,
            date_end=latest,
            active_schedules=active_schedules,
            last_job=last_job,
            git_remote=parliament.git_remote,
            git_repo_url=_git_remote_to_web_url(parliament.git_remote),
        )
        with self._lock:
            self._overview_cache[parliament_id] = (time.time(), overview)
        return overview

    @staticmethod
    def _last_job_for_parliament(parliament_id: str) -> dict[str, Any] | None:
        from src.services.registry import registry

        jm = registry.job_manager
        if jm is None:
            return None
        for entry in jm.list_history(limit=50):
            if entry.get("parliament") == parliament_id:
                return {
                    "id": entry.get("id"),
                    "status": entry.get("status"),
                    "finished_at": entry.get("finished_at"),
                    "stages": entry.get("stages"),
                }
        return None

    def invalidate(self, parliament_id: str | None = None) -> None:
        with self._lock:
            if parliament_id is None:
                self._overview_cache.clear()
                return
            self._overview_cache.pop(parliament_id, None)


_service: ParliamentStatsService | None = None


def get_parliament_stats() -> ParliamentStatsService:
    global _service
    if _service is None:
        _service = ParliamentStatsService()
    return _service
