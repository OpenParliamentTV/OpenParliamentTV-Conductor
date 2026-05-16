"""Log + progress bridge between Tools' stdlib `logging` and our `LogStreamer`.

Tools modules (DE workflow, shared nel/align/ner) use module-level `logger`
instances. We attach a handler that:
  1. Streams every record into `log_streamer.append()`.
  2. Pattern-matches known progress-signalling messages (e.g. "Publishing 21045",
     "Time-aligning 21045") to bump `Job.sessions_completed` + `Job.current_session`.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Callable

from src.services.job_manager import Job
from src.services.log_streamer import LogStreamer

# Regexes capturing the session id from known messages in Tools' workflow.
_SESSION_PATTERNS = [
    re.compile(r"Publishing (\d+) from"),
    re.compile(r"Time-aligning (\d+)"),
    re.compile(r"Linking entities for (\d+) from"),
    re.compile(r"Extracting Named Entities for (\d+)"),
]

# Banner messages emitted by Tools' workflow.execute_workflow at each stage
# transition. We use them to advance Job.stage so the dashboard isn't stuck
# on stages[0] for the whole run. Order doesn't matter — each is checked
# against every line.
_STAGE_PATTERNS = [
    (re.compile(r"Downloading media and proceeding data"), "download"),
    (re.compile(r"Merging data from"), "merge"),
    (re.compile(r"Linking entities with wikidata"), "nel"),
    (re.compile(r"Updating time-alignment"), "align"),
    (re.compile(r"Updating NER"), "ner"),
]


class JobLogHandler(logging.Handler):
    def __init__(
        self,
        job: Job,
        streamer: LogStreamer,
        loop: asyncio.AbstractEventLoop,
        on_progress: Callable[[Job], None] | None = None,
    ) -> None:
        super().__init__()
        self.job = job
        self.streamer = streamer
        self.loop = loop
        self.on_progress = on_progress
        self.seen_sessions: set[str] = set()
        self.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-5s %(name)s: %(message)s", "%H:%M:%S")
        )

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
            asyncio.run_coroutine_threadsafe(self.streamer.append(self.job.id, line), self.loop)
            message = record.getMessage()
            stage_changed = self._maybe_advance_stage(message)
            session_changed = self._maybe_bump_progress(message)
            if (stage_changed or session_changed) and self.on_progress:
                self.on_progress(self.job)
        except Exception:
            self.handleError(record)

    def _maybe_advance_stage(self, message: str) -> bool:
        for pat, stage in _STAGE_PATTERNS:
            if pat.search(message) and self.job.stage != stage:
                self.job.stage = stage
                return True
        return False

    def _maybe_bump_progress(self, message: str) -> bool:
        for pat in _SESSION_PATTERNS:
            m = pat.search(message)
            if not m:
                continue
            session = m.group(1)
            if session in self.seen_sessions:
                return False
            self.seen_sessions.add(session)
            self.job.current_session = session
            self.job.sessions_completed = len(self.seen_sessions)
            if self.job.sessions_total:
                self.job.progress = min(
                    100,
                    int(100 * self.job.sessions_completed / self.job.sessions_total),
                )
            return True
        return False
