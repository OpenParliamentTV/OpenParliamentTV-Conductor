"""Session status tracker.

Derives per-stage status from the `meta.processing` block at the top of
each session file — the authoritative record of what actually ran —
rather than guessing from intermediate file existence. Stage vocabulary:
download/parse/merge/nel/align/ner.
"""

from __future__ import annotations

import importlib
import time
from pathlib import Path
from typing import Any

from src.config import ParliamentConfig


# Maps UI stage → set of keys in `meta.processing` that indicate the
# stage has run. A stage is "complete" if ANY of the listed keys is
# present. `download` is the odd one out — it has no `meta.processing`
# entry, so it's inferred from the media file existing OR from
# downstream evidence (see `_DOWNLOAD_EVIDENCE`).
_CHEAP_PROCESSING_STAGES: dict[str, tuple[str, ...]] = {
    "parse": ("parse_media", "parse_proceedings"),
    "merge": ("merge",),
    "nel": ("nel",),
    "align": ("align",),
    "ner": ("ner",),
}

# Downstream stages whose presence proves a download happened: you can't
# parse media or merge data you never downloaded. Needed because the
# media file (`original/media/<session>-media.json`) is a transient,
# gitignored artifact — absent after cleanup or in a fresh checkout —
# so checking only for it falsely reports completed downloads as
# "never_run" while parse/merge stay green.
_DOWNLOAD_EVIDENCE: tuple[str, ...] = ("parse_media", "merge", "parse_proceedings")


class StatusTracker:
    def __init__(self) -> None:
        self._detailed_cache: dict[tuple[str, str], tuple[float, dict[str, dict[str, Any]]]] = {}
        self._cheap_cache: dict[tuple[str, str], tuple[float, dict[str, str]]] = {}
        self._ttl_seconds = 5.0

    def sessions(self, parliament_id: str, parliament: ParliamentConfig, prefix: str = "") -> list[str]:
        module = importlib.import_module(f"optv.parliaments.{parliament_id}.common")
        config = module.Config(Path(parliament.data_dir))
        return config.sessions(prefix=prefix)

    def session_status_cheap(
        self,
        parliament_id: str,
        parliament: ParliamentConfig,
        session: str,
    ) -> dict[str, str]:
        """Cheap per-row status for the sessions list.

        Reads stages from `session_processing_stages`, which combines the
        explicit `meta.processing` keys with evidence-based fallbacks
        (debug.align-duration, debug.ner-duration, non-empty data array)
        so sessions processed by older Tools versions — which wrote sparse
        meta.processing blocks — still report what actually ran. One
        16 KB header read per session, cached 60 s. The `download` stage
        has no `meta.processing` entry, so it's inferred from the media
        file existing OR downstream evidence (`_DOWNLOAD_EVIDENCE`) —
        media can't be parsed/merged unless it was downloaded, and the
        media file itself is transient/gitignored. Returns
        `{stage: "complete"|"never_run"}` for download + parse + merge +
        nel + align + ner.
        """
        key = (parliament_id, session)
        now = time.time()
        cached = self._cheap_cache.get(key)
        if cached and now - cached[0] < self._ttl_seconds:
            return cached[1]

        from src.services.session_content import get_session_content

        stages_present = get_session_content().session_processing_stages(
            parliament_id, parliament, session,
        )
        module = importlib.import_module(f"optv.parliaments.{parliament_id}.common")
        config = module.Config(Path(parliament.data_dir))

        download_done = config.file(session, "media").exists() or any(
            k in stages_present for k in _DOWNLOAD_EVIDENCE
        )
        result: dict[str, str] = {
            "download": "complete" if download_done else "never_run",
        }
        for stage, keys in _CHEAP_PROCESSING_STAGES.items():
            result[stage] = "complete" if any(k in stages_present for k in keys) else "never_run"

        self._cheap_cache[key] = (now, result)
        return result

    def session_status(
        self,
        parliament_id: str,
        parliament: ParliamentConfig,
        session: str,
    ) -> dict[str, str]:
        """Return `{stage: "complete"|"never_run"}` for every pipeline stage.

        Thin alias for `session_status_cheap` so list and detail views agree
        on what "ran" means. Both read `meta.processing` from the session
        file header, which is the authoritative record of stage execution.
        """
        return self.session_status_cheap(parliament_id, parliament, session)

    def session_status_detailed(
        self,
        parliament_id: str,
        parliament: ParliamentConfig,
        session: str,
    ) -> dict[str, dict[str, Any]]:
        """Return `{stage: {status, last_job_id, last_failure_message}}`.

        `status` is one of `complete`, `failed`, `never_run`. (`partial`
        is reserved for agenda-item rollups, not single sessions.)
        Failure info is the most recent *failed* job touching this
        (session, stage); if the stage has produced output, `failed` is
        only returned when the most recent job for this stage failed.
        """
        key = (parliament_id, session)
        now = time.time()
        cached = self._detailed_cache.get(key)
        if cached and now - cached[0] < self._ttl_seconds:
            return cached[1]

        binary = self.session_status(parliament_id, parliament, session)
        last_by_stage = self._last_job_per_stage(parliament_id, session)
        result: dict[str, dict[str, Any]] = {}
        for stage, state in binary.items():
            entry = last_by_stage.get(stage)
            if state == "complete":
                status = "complete"
            elif entry and entry.get("status") == "failed":
                status = "failed"
            else:
                status = "never_run"
            result[stage] = {
                "status": status,
                "last_job_id": entry.get("id") if entry else None,
                "last_failure_message": entry.get("error") if entry and entry.get("status") == "failed" else None,
            }
        self._detailed_cache[key] = (now, result)
        return result

    def _last_job_per_stage(self, parliament_id: str, session: str) -> dict[str, dict[str, Any]]:
        """Find the most recent completed job per stage that touched `session`."""
        try:
            from src.services.registry import registry

            jm = registry.job_manager
        except Exception:
            return {}
        if jm is None:
            return {}
        history = jm.list_history(limit=200)
        out: dict[str, dict[str, Any]] = {}
        for entry in history:
            if entry.get("parliament") != parliament_id:
                continue
            if not self._job_touches_session(entry, session):
                continue
            for stage in entry.get("stages") or []:
                if stage not in out:
                    out[stage] = entry
        return out

    @staticmethod
    def _job_touches_session(entry: dict[str, Any], session: str) -> bool:
        sf = entry.get("session_filter")
        if not sf:
            return True  # whole-period job touches every session
        try:
            import re as _re

            return bool(_re.search(sf, session))
        except _re.error:
            return False

    def invalidate(self, parliament_id: str | None = None, session: str | None = None) -> None:
        if parliament_id is None:
            self._detailed_cache.clear()
            self._cheap_cache.clear()
            return
        for cache in (self._detailed_cache, self._cheap_cache):
            keys = [k for k in cache if k[0] == parliament_id and (session is None or k[1] == session)]
            for k in keys:
                cache.pop(k, None)


_tracker: StatusTracker | None = None


def get_tracker() -> StatusTracker:
    global _tracker
    if _tracker is None:
        _tracker = StatusTracker()
    return _tracker
