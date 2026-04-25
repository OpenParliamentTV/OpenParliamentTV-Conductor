import pytest

from src.config import SlackConfig
from src.services.notifier import JobResult, SlackNotifier


def _cfg(**overrides) -> SlackConfig:
    return SlackConfig(**overrides)


def test_skipped_when_webhook_empty():
    n = SlackNotifier("", "http://x", _cfg())
    result = JobResult("id", "test", "DE", success=True, partial=False, stage=None,
                      duration_seconds=10, sessions_total=1, sessions_completed=1)
    import asyncio
    assert asyncio.run(n.notify(result)) is False


def test_builds_success_message():
    n = SlackNotifier("http://hook", "http://x", _cfg(on_success=True))
    result = JobResult("id-1", "de-daily", "DE", success=True, partial=False, stage=None,
                      duration_seconds=125, sessions_total=3, sessions_completed=3)
    msg = n._build_message(result)
    att = msg["attachments"][0]
    assert att["color"] == "good"
    assert "🟢" in att["title"]
    assert att["title_link"].endswith("/jobs/id-1")
    assert any(f["value"] == "3/3" for f in att["fields"])


def test_builds_partial_failure_message_with_failed_sessions():
    n = SlackNotifier("http://hook", "http://x", _cfg())
    result = JobResult(
        "id-2", "de-daily", "DE", success=False, partial=True, stage="align",
        duration_seconds=932, sessions_total=45, sessions_completed=43,
        failed_sessions=[{"session": "21045", "error": "Audio not found"},
                         {"session": "21046", "error": "timeout"}],
    )
    msg = n._build_message(result)
    att = msg["attachments"][0]
    assert att["color"] == "warning"
    assert "🟡" in att["title"]
    assert "21045: Audio not found" in att["text"]
    assert "21046: timeout" in att["text"]


def test_partial_failure_truncates_over_ten():
    n = SlackNotifier("http://hook", "http://x", _cfg())
    failed = [{"session": f"{i}", "error": "e"} for i in range(15)]
    result = JobResult("id", "j", "DE", success=False, partial=True, stage=None,
                      duration_seconds=1, sessions_total=15, sessions_completed=0,
                      failed_sessions=failed)
    msg = n._build_message(result)
    assert "…and 5 more" in msg["attachments"][0]["text"]


def test_filters_on_scheduled_only():
    n = SlackNotifier("http://hook", "http://x", _cfg(scheduled_only=True))
    result = JobResult("id", "j", "DE", success=False, partial=False, stage="download",
                      duration_seconds=1, sessions_total=1, sessions_completed=0)
    import asyncio
    assert asyncio.run(n.notify(result, source="manual")) is False


def test_duration_formatting():
    assert SlackNotifier._format_duration(45) == "45s"
    assert SlackNotifier._format_duration(125) == "2m 5s"
    assert SlackNotifier._format_duration(3665) == "1h 1m"
