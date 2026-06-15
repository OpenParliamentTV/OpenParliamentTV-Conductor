"""Per-session content reader.

Reads processed session JSON via Tools' `Config.file(session, 'processed')`.
Extracts a summary (metadata header, counts, per-stage status), an
agenda-item-grouped view, a single-speech view, and a date-range filter
over all sessions.

No Tools-side refactor; we do our own light reads on top of Tools paths.
"""

from __future__ import annotations

import importlib
import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.config import ParliamentConfig
from src.services.status_tracker import get_tracker

logger = logging.getLogger(__name__)

_META_DATE_RE = re.compile(r'"dateStart"\s*:\s*"([0-9T:+\-]+)"')
_META_DATE_END_RE = re.compile(r'"dateEnd"\s*:\s*"([0-9T:+\-]+)"')
_PROCESSING_BLOCK_RE = re.compile(r'"processing"\s*:\s*\{(?P<body>[^{}]*)\}', re.DOTALL)
_PROCESSING_KEY_RE = re.compile(r'"([A-Za-z_][A-Za-z0-9_]*)"\s*:')
_DATA_NONEMPTY_RE = re.compile(r'"data"\s*:\s*\[\s*\{')
# Authoritative electoral period — `data[0].electoralPeriod.number`. This is
# the source of truth regardless of session-ID format (DE IDs encode the
# period as a prefix, SE IDs are year-prefixed, etc.). Appears ~byte 350.
_ELECTORAL_PERIOD_RE = re.compile(r'"electoralPeriod"\s*:\s*\{\s*"number"\s*:\s*(\d+)')
# 16 KB header read covers evidence signals (debug.alignDuration, debug.nerDuration,
# people[].wid) for typical sessions; the very first speech item usually fits.
_HEADER_BYTES = 16384


def _parliament_config(parliament_id: str, parliament: ParliamentConfig):
    module = importlib.import_module(f"optv.parliaments.{parliament_id}.common")
    return module.Config(Path(parliament.data_dir))


def _read_header(path: Path, nbytes: int = _HEADER_BYTES) -> str:
    try:
        with path.open("rb") as fh:
            return fh.read(nbytes).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _extract_meta_dates(path: Path) -> tuple[str | None, str | None]:
    head = _read_header(path)
    if not head:
        return None, None
    # Only keep matches that occur before `data[`, so we don't pick up
    # the per-speech `session.dateStart` that shadows `meta.dateStart`.
    data_cut = head.find('"data"')
    if data_cut != -1:
        head = head[:data_cut]
    ds = _META_DATE_RE.search(head)
    de = _META_DATE_END_RE.search(head)
    return (ds.group(1) if ds else None, de.group(1) if de else None)


def _extract_processing_stages(path: Path) -> set[str]:
    """Return the set of pipeline stages that have run for this session.

    Combines two signals from a single 16 KB header read:

    1. Explicit keys in `meta.processing` (e.g. `parse_media`, `merge`,
       `nel`, `align`, `ner`) — the canonical record when modern Tools
       writes it.
    2. Evidence-based fallbacks for sessions processed by older Tools
       versions that wrote sparse `meta.processing` blocks. Tools' own
       `Config.status()` uses these same signals; we mirror them so the
       Conductor UI agrees with what actually ran:
         - `debug.alignDuration` per item -> align ran (aeneas wrote durations)
         - `debug.nerDuration` per item   -> ner ran   (entity-fishing wrote durations)
         - any data item present           -> merge + parse_media + parse_proceedings
                                              ran (you can't have items otherwise)
    """
    head = _read_header(path)
    if not head:
        return set()
    data_cut = head.find('"data"')
    head_meta = head[:data_cut] if data_cut != -1 else head
    m = _PROCESSING_BLOCK_RE.search(head_meta)
    stages = set(_PROCESSING_KEY_RE.findall(m.group("body"))) if m else set()

    if '"alignDuration"' in head:
        stages.add("align")
    if '"nerDuration"' in head:
        stages.add("ner")
    if _DATA_NONEMPTY_RE.search(head):
        stages.update({"merge", "parse_media", "parse_proceedings"})
    return stages


def _extract_period(path: Path) -> int | None:
    """Return the authoritative electoral period for a session.

    Reads `data[0].electoralPeriod.number` from the file header — the source
    of truth, independent of session-ID format. Returns None if absent (e.g.
    an empty session with no data items).
    """
    head = _read_header(path)
    if not head:
        return None
    m = _ELECTORAL_PERIOD_RE.search(head)
    return int(m.group(1)) if m else None


@dataclass
class SessionSummary:
    id: str
    parliament: str
    date_start: str | None
    date_end: str | None
    duration_seconds: int | None
    speech_count: int
    agenda_item_count: int
    status: dict[str, dict[str, Any]]


class SessionContentService:
    """Reads per-session content. Caches each read for a short TTL."""

    def __init__(self, ttl_seconds: float = 60.0) -> None:
        self._ttl = ttl_seconds
        self._period_span_ttl = 120.0
        self._lock = threading.Lock()
        self._session_cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
        self._range_cache: dict[tuple[str, int | None, str, str], tuple[float, list[str]]] = {}
        self._date_cache: dict[tuple[str, str], tuple[float, str | None]] = {}
        self._period_span_cache: dict[tuple[str, int], tuple[float, tuple[str | None, str | None, int]]] = {}
        self._stages_cache: dict[tuple[str, str], tuple[float, set[str]]] = {}
        self._period_cache: dict[tuple[str, str], tuple[float, int | None]] = {}

    def _load_session(self, parliament_id: str, parliament: ParliamentConfig, session: str) -> dict[str, Any] | None:
        key = (parliament_id, session)
        now = time.time()
        with self._lock:
            cached = self._session_cache.get(key)
            if cached and now - cached[0] < self._ttl:
                return cached[1]
        config = _parliament_config(parliament_id, parliament)
        path = config.file(session, "processed")
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            logger.exception("Failed to load session %s for parliament %s", session, parliament_id)
            return None
        with self._lock:
            self._session_cache[key] = (time.time(), data)
        return data

    def summary(
        self,
        parliament_id: str,
        parliament: ParliamentConfig,
        session: str,
    ) -> SessionSummary | None:
        data = self._load_session(parliament_id, parliament, session)
        tracker = get_tracker()
        status = tracker.session_status_detailed(parliament_id, parliament, session)
        if data is None:
            return SessionSummary(
                id=session,
                parliament=parliament_id,
                date_start=None,
                date_end=None,
                duration_seconds=None,
                speech_count=0,
                agenda_item_count=0,
                status=status,
            )
        meta = data.get("meta") or {}
        speeches = data.get("data") or []
        agenda_titles: set[str] = set()
        for speech in speeches:
            title = ((speech.get("agendaItem") or {}).get("officialTitle") or "").strip()
            if title:
                agenda_titles.add(title)
        date_start = meta.get("dateStart")
        date_end = meta.get("dateEnd")
        duration = None
        if date_start and date_end:
            try:
                from datetime import datetime

                ds = datetime.fromisoformat(date_start)
                de = datetime.fromisoformat(date_end)
                duration = int((de - ds).total_seconds())
            except ValueError:
                duration = None
        return SessionSummary(
            id=session,
            parliament=parliament_id,
            date_start=date_start,
            date_end=date_end,
            duration_seconds=duration,
            speech_count=len(speeches),
            agenda_item_count=len(agenda_titles),
            status=status,
        )

    def content(
        self,
        parliament_id: str,
        parliament: ParliamentConfig,
        session: str,
    ) -> list[dict[str, Any]] | None:
        """Return [{title, speeches: [...]}] grouped by agenda item, in order."""
        data = self._load_session(parliament_id, parliament, session)
        if data is None:
            return None
        speeches = data.get("data") or []
        groups: list[dict[str, Any]] = []
        groups_by_title: dict[str, dict[str, Any]] = {}
        for speech in speeches:
            agenda = speech.get("agendaItem") or {}
            official_title = (agenda.get("officialTitle") or "").strip() or "—"
            plain_title = (agenda.get("title") or "").strip()
            group = groups_by_title.get(official_title)
            if group is None:
                group = {"title": official_title, "plain_title": plain_title, "speeches": []}
                groups_by_title[official_title] = group
                groups.append(group)
            text_body = []
            for tc in speech.get("textContents") or []:
                for b in tc.get("textBody") or []:
                    chunk = b.get("text") or " ".join(
                        s.get("text", "") for s in b.get("sentences") or [] if s.get("text")
                    )
                    if chunk:
                        text_body.append(chunk)
            full_text = " ".join(text_body).strip()
            preview = full_text[:200] + ("…" if len(full_text) > 200 else "")
            speaker = ""
            people = speech.get("people") or []
            if people:
                speaker = (people[0] or {}).get("label") or ""
            media = speech.get("media") or {}
            speech_status = self._speech_status(speech)
            groups[-1] if False else None  # no-op to keep lint quiet
            group["speeches"].append({
                "speech_index": speech.get("speechIndex"),
                "speaker": speaker,
                "preview": preview,
                "has_video": bool(media.get("videoFileURI")),
                "status": speech_status,
            })
        for g in groups:
            g["speech_count"] = len(g["speeches"])
        return groups

    def speech(
        self,
        parliament_id: str,
        parliament: ParliamentConfig,
        session: str,
        speech_index: int,
    ) -> dict[str, Any] | None:
        data = self._load_session(parliament_id, parliament, session)
        if data is None:
            return None
        for speech in data.get("data") or []:
            if speech.get("speechIndex") == speech_index:
                text_parts: list[dict[str, str]] = []
                for tc in speech.get("textContents") or []:
                    for b in tc.get("textBody") or []:
                        text_parts.append({
                            "speaker": (b.get("speaker") or ""),
                            "text": b.get("text") or " ".join(
                                s.get("text", "") for s in b.get("sentences") or [] if s.get("text")
                            ),
                            "type": (b.get("type") or ""),
                        })
                media = speech.get("media") or {}
                people = speech.get("people") or []
                return {
                    "speech_index": speech.get("speechIndex"),
                    "speaker": (people[0] or {}).get("label", "") if people else "",
                    "agenda_item": ((speech.get("agendaItem") or {}).get("officialTitle") or "").strip(),
                    "text_body": text_parts,
                    "video_uri": media.get("videoFileURI"),
                    "duration": media.get("duration"),
                    "status": self._speech_status(speech),
                }
        return None

    def sessions_in_range(
        self,
        parliament_id: str,
        parliament: ParliamentConfig,
        period: int | None,
        date_from: str | None,
        date_to: str | None,
    ) -> list[str]:
        """Return session IDs whose meta.dateStart falls in [date_from, date_to].

        `date_from` and `date_to` are ISO date strings (YYYY-MM-DD) or None.
        Uses cheap header-reads of each session file; cached 60s per
        (parliament_id, period, date_from, date_to) tuple.
        """
        key = (parliament_id, period, date_from or "", date_to or "")
        now = time.time()
        with self._lock:
            cached = self._range_cache.get(key)
            if cached and now - cached[0] < self._ttl:
                return list(cached[1])

        config = _parliament_config(parliament_id, parliament)
        prefix = str(period) if period is not None else ""
        sessions = config.sessions(prefix=prefix)
        matching: list[str] = []
        for sid in sessions:
            path = config.file(sid, "processed")
            if not path.exists():
                continue
            ds, _de = _extract_meta_dates(path)
            if ds is None:
                continue
            day = ds[:10]  # YYYY-MM-DD from ISO 8601
            if date_from and day < date_from:
                continue
            if date_to and day > date_to:
                continue
            matching.append(sid)
        with self._lock:
            self._range_cache[key] = (time.time(), list(matching))
        return matching

    def session_processing_stages(
        self,
        parliament_id: str,
        parliament: ParliamentConfig,
        session: str,
    ) -> set[str]:
        """Return the set of pipeline stages that have run for this session.

        Combines explicit `meta.processing` keys with evidence-based
        fallbacks (see `_extract_processing_stages`) so old sessions whose
        Tools version wrote sparse processing blocks still report what
        actually ran. One 16 KB header read per session, cached 60 s.
        Returns an empty set if the processed session file does not exist.
        """
        key = (parliament_id, session)
        now = time.time()
        with self._lock:
            cached = self._stages_cache.get(key)
            if cached and now - cached[0] < self._ttl:
                return set(cached[1])
        config = _parliament_config(parliament_id, parliament)
        path = config.file(session, "processed")
        stages = _extract_processing_stages(path) if path.exists() else set()
        with self._lock:
            self._stages_cache[key] = (time.time(), set(stages))
        return stages

    def session_date_start(
        self,
        parliament_id: str,
        parliament: ParliamentConfig,
        session: str,
    ) -> str | None:
        """Return meta.dateStart for a session using a cheap header read. Cached."""
        key = (parliament_id, session)
        now = time.time()
        with self._lock:
            cached = self._date_cache.get(key)
            if cached and now - cached[0] < self._ttl:
                return cached[1]
        config = _parliament_config(parliament_id, parliament)
        path = config.file(session, "processed")
        ds: str | None = None
        if path.exists():
            ds, _ = _extract_meta_dates(path)
        with self._lock:
            self._date_cache[key] = (time.time(), ds)
        return ds

    def session_period(
        self,
        parliament_id: str,
        parliament: ParliamentConfig,
        session: str,
    ) -> int | None:
        """Return the authoritative electoral period for a session.

        Reads `data[0].electoralPeriod.number` from the file via a cheap
        header read — correct regardless of session-ID format. Returns None
        if the file is missing or has no data items. Cached 60 s.
        """
        key = (parliament_id, session)
        now = time.time()
        with self._lock:
            cached = self._period_cache.get(key)
            if cached and now - cached[0] < self._ttl:
                return cached[1]
        config = _parliament_config(parliament_id, parliament)
        path = config.file(session, "processed")
        period = _extract_period(path) if path.exists() else None
        with self._lock:
            self._period_cache[key] = (time.time(), period)
        return period

    def period_date_span(
        self,
        parliament_id: str,
        parliament: ParliamentConfig,
        period: int,
    ) -> tuple[str | None, str | None, int]:
        """Return (earliest dateStart, latest dateEnd, session count) for a period.

        Header-reads only the first and last session by ID sort (session IDs
        encode period + index, so lexicographic sort matches chronological).
        2 header reads per period total. Cached 120 s.
        """
        key = (parliament_id, period)
        now = time.time()
        with self._lock:
            cached = self._period_span_cache.get(key)
            if cached and now - cached[0] < self._period_span_ttl:
                return cached[1]

        config = _parliament_config(parliament_id, parliament)
        sessions = sorted(config.sessions(prefix=str(period)))
        if not sessions:
            result: tuple[str | None, str | None, int] = (None, None, 0)
        else:
            first_path = config.file(sessions[0], "processed")
            last_path = config.file(sessions[-1], "processed")
            ds, _ = _extract_meta_dates(first_path) if first_path.exists() else (None, None)
            _, de = _extract_meta_dates(last_path) if last_path.exists() else (None, None)
            result = (ds, de, len(sessions))
        with self._lock:
            self._period_span_cache[key] = (time.time(), result)
        return result

    def invalidate(self, parliament_id: str | None = None, session: str | None = None) -> None:
        with self._lock:
            if parliament_id is None:
                self._session_cache.clear()
                self._range_cache.clear()
                self._date_cache.clear()
                self._period_span_cache.clear()
                self._stages_cache.clear()
                self._period_cache.clear()
                return
            self._session_cache = {
                k: v for k, v in self._session_cache.items()
                if not (k[0] == parliament_id and (session is None or k[1] == session))
            }
            self._range_cache = {
                k: v for k, v in self._range_cache.items() if k[0] != parliament_id
            }
            self._date_cache = {
                k: v for k, v in self._date_cache.items()
                if not (k[0] == parliament_id and (session is None or k[1] == session))
            }
            self._period_span_cache = {
                k: v for k, v in self._period_span_cache.items() if k[0] != parliament_id
            }
            self._stages_cache = {
                k: v for k, v in self._stages_cache.items()
                if not (k[0] == parliament_id and (session is None or k[1] == session))
            }
            self._period_cache = {
                k: v for k, v in self._period_cache.items()
                if not (k[0] == parliament_id and (session is None or k[1] == session))
            }

    @staticmethod
    def _speech_status(speech: dict[str, Any]) -> dict[str, Any]:
        """Per-speech signals for UI pills.

        `aligned` reflects whether time-alignment produced a duration.
        `importable` mirrors how the platform's media.php treats this row:
        the media record is always written, but the `textContents` import is
        gated on confidence == 1 && exactly one linkedMediaIndex. A missing
        confidence means merge has not run yet, so there is no text to gate
        and the speech still lands on the platform as a media-only record.
        """
        debug = speech.get("debug") or {}
        confidence = debug.get("confidence")
        linked_count = len(debug.get("linkedMediaIndexes") or [])
        has_text = any(
            bool(tc.get("textBody"))
            for tc in (speech.get("textContents") or [])
        )
        return {
            "aligned": bool(debug.get("alignDuration")),
            "confidence": confidence,
            "linked_media_count": linked_count,
            "has_text": has_text,
            "importable": confidence is None or (confidence == 1 and linked_count == 1),
        }


_service: SessionContentService | None = None


def get_session_content() -> SessionContentService:
    global _service
    if _service is None:
        _service = SessionContentService()
    return _service
