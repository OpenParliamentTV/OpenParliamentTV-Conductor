"""Jinja2 template filters for human-friendly dates.

All datetime values in the app are stored as ISO strings (e.g.
`2026-06-12T09:00:00+02:00`). These filters parse them and render a
compact absolute form (`12 Jun 2026, 09:00`) plus a static relative
form (`3 hours ago` / `in 2 hours`). Both are null-safe: anything that
isn't a parseable datetime renders as the em-dash placeholder, so
callers can drop their own `or "—"` fallbacks.
"""

from __future__ import annotations

from datetime import datetime, timezone

DASH = "—"


def _parse(value: object) -> datetime | None:
    """Parse an ISO string (or pass through a datetime) to a datetime.

    Returns None for None/empty/unparseable input so filters never raise.
    """
    if isinstance(value, datetime):
        return value
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def humandate(value: object, with_time: bool = True) -> str:
    """Format an ISO datetime as `12 Jun 2026, 09:00` (or date-only).

    Displays the value in its own offset — no timezone conversion. Uses
    explicit day assembly instead of `%-d` to stay platform-independent.
    """
    dt = _parse(value)
    if dt is None:
        return DASH
    date_part = f"{dt.day} {dt:%b %Y}"
    if with_time:
        return f"{date_part}, {dt:%H:%M}"
    return date_part


def humantime(value: object) -> str:
    """Format just the clock time, e.g. `15:36`, in the value's own offset."""
    dt = _parse(value)
    if dt is None:
        return DASH
    return f"{dt:%H:%M}"


def same_day(a: object, b: object) -> bool:
    """True if two ISO datetimes fall on the same calendar date."""
    da, db = _parse(a), _parse(b)
    return da is not None and db is not None and da.date() == db.date()


def timeago(value: object) -> str:
    """Static relative time, e.g. `5 minutes ago` / `in 2 hours`.

    Always relative — scales up to weeks/months/years so it never falls
    back to printing an absolute date (which would duplicate `humandate`
    at the call sites). Past and future are both handled. Naive
    datetimes are assumed to be UTC.
    """
    dt = _parse(value)
    if dt is None:
        return DASH
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = datetime.now(tz=timezone.utc)
    delta = (now - dt).total_seconds()
    future = delta < 0
    secs = abs(delta)

    if secs < 45:
        return "just now"
    if secs < 90:
        unit, n = "minute", 1
    elif secs < 3600:
        unit, n = "minute", round(secs / 60)
    elif secs < 86400:
        unit, n = "hour", round(secs / 3600)
    elif secs < 7 * 86400:
        unit, n = "day", round(secs / 86400)
    elif secs < 30 * 86400:
        unit, n = "week", round(secs / (7 * 86400))
    elif secs < 365 * 86400:
        unit, n = "month", round(secs / (30 * 86400))
    else:
        unit, n = "year", round(secs / (365 * 86400))

    label = f"{n} {unit}{'s' if n != 1 else ''}"
    return f"in {label}" if future else f"{label} ago"


def register(env) -> None:
    """Register the date filters on a Jinja2 environment."""
    env.filters["humandate"] = humandate
    env.filters["humantime"] = humantime
    env.filters["timeago"] = timeago
    env.filters["same_day"] = same_day
