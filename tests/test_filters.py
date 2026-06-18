"""Tests for the human-friendly date Jinja filters."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.web.filters import DASH, humandate, timeago


def test_humandate_with_time():
    assert humandate("2026-06-12T09:00:00+02:00") == "12 Jun 2026, 09:00"


def test_humandate_date_only():
    assert humandate("2026-06-12T09:00:00+02:00", with_time=False) == "12 Jun 2026"


def test_humandate_none_and_malformed():
    assert humandate(None) == DASH
    assert humandate("") == DASH
    assert humandate("not-a-date") == DASH


def test_timeago_past():
    past = datetime.now(tz=timezone.utc) - timedelta(hours=3)
    assert timeago(past.isoformat()) == "3 hours ago"


def test_timeago_future():
    future = datetime.now(tz=timezone.utc) + timedelta(hours=2)
    assert timeago(future.isoformat()) == "in 2 hours"


def test_timeago_just_now():
    now = datetime.now(tz=timezone.utc)
    assert timeago(now.isoformat()) == "just now"


def test_timeago_naive_assumed_utc():
    past = datetime.now(tz=timezone.utc).replace(tzinfo=None) - timedelta(minutes=5)
    assert timeago(past.isoformat()) == "5 minutes ago"


def test_timeago_scales_to_weeks_months_years():
    now = datetime.now(tz=timezone.utc)
    assert timeago((now - timedelta(days=14)).isoformat()) == "2 weeks ago"
    assert timeago((now - timedelta(days=33)).isoformat()) == "1 month ago"
    assert timeago((now - timedelta(days=400)).isoformat()) == "1 year ago"


def test_timeago_none():
    assert timeago(None) == DASH
