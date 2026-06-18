"""Unit tests for SessionContentService.period_date_span.

Exercises the numbering-scheme bucketing that fixes the "latest session by
ID sort != latest by date" bug: special sessions (DE's 8xx/9xx) sort after
the regular block but are not chronologically last, so the end date must
come from the regular block, not the lexicographically-last special.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest


def _write_session(data_dir: Path, sid: str, date_start: str, date_end: str) -> None:
    (data_dir / f"{sid}-processed.json").write_text(
        json.dumps({"meta": {"dateStart": date_start, "dateEnd": date_end}, "data": []}),
        encoding="utf-8",
    )


@pytest.fixture
def fake_de_common(monkeypatch, tmp_path):
    """Inject a fake optv.parliaments.DE.common with an on-disk Config."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    for pkg_name in ("optv", "optv.parliaments", "optv.parliaments.DE"):
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = []
        monkeypatch.setitem(sys.modules, pkg_name, pkg)

    sessions: list[str] = []

    class FakeConfig:
        def __init__(self, _data_dir):
            self.data_dir = Path(_data_dir)

        def sessions(self, prefix=""):
            return [s for s in sessions if s.startswith(prefix)]

        def file(self, session, kind):
            return self.data_dir / f"{session}-{kind}.json"

    common = types.ModuleType("optv.parliaments.DE.common")
    common.Config = FakeConfig
    monkeypatch.setitem(sys.modules, "optv.parliaments.DE.common", common)

    return data_dir, sessions


def _parliament(data_dir: Path):
    return types.SimpleNamespace(data_dir=str(data_dir))


def test_latest_comes_from_regular_block_not_lexicographic_last(fake_de_common):
    """21902 sorts last but 21084 is chronologically later — end date is 21084's."""
    data_dir, sessions = fake_de_common
    _write_session(data_dir, "21001", "2025-03-25T11:00:00+01:00", "2025-03-25T18:00:00+01:00")
    _write_session(data_dir, "21084", "2026-06-12T09:00:00+02:00", "2026-06-12T15:36:00+02:00")
    _write_session(data_dir, "21800", "2025-09-04T13:00:00+02:00", "2025-09-04T13:19:10+02:00")
    _write_session(data_dir, "21900", "2025-05-08T12:29:02+02:00", "2025-05-08T13:33:59+02:00")
    _write_session(data_dir, "21902", "2026-02-23T13:54:40+01:00", "2026-02-24T13:19:27+01:00")
    sessions += ["21001", "21084", "21800", "21900", "21902"]

    from src.services.session_content import SessionContentService

    ds, de, n = SessionContentService().period_date_span("DE", _parliament(data_dir), 21)
    assert ds == "2025-03-25T11:00:00+01:00"
    assert de == "2026-06-12T15:36:00+02:00"  # 21084, not 21902's 2026-02-24
    assert n == 5


def test_malformed_dates_are_ignored(fake_de_common):
    """A malformed mid-block dateStart must not win the min comparison."""
    data_dir, sessions = fake_de_common
    _write_session(data_dir, "18001", "2013-10-22T11:00:00+02:00", "2013-10-22T18:00:00+02:00")
    _write_session(data_dir, "18014", "02014T09:00:00+01:00", "02014T13:00:00+01:00")
    _write_session(data_dir, "18245", "2017-09-05T09:00:00+02:00", "2017-09-05T14:21:00+02:00")
    sessions += ["18001", "18014", "18245"]

    from src.services.session_content import SessionContentService

    ds, de, n = SessionContentService().period_date_span("DE", _parliament(data_dir), 18)
    assert ds == "2013-10-22T11:00:00+02:00"  # not "02014T..."
    assert de == "2017-09-05T14:21:00+02:00"
    assert n == 3


def test_empty_period(fake_de_common):
    data_dir, _sessions = fake_de_common
    from src.services.session_content import SessionContentService

    assert SessionContentService().period_date_span("DE", _parliament(data_dir), 99) == (None, None, 0)
