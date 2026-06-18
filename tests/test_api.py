"""Integration tests hitting the FastAPI app via `TestClient`.

Uses a temporary config dir + status dir so the app boots standalone.
Bypasses the OAuth flow by minting a JWT directly and placing it in a cookie.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    status_dir = tmp_path / "status"
    data_dir = tmp_path / "data"
    fake_data = data_dir / "Fake-Data"
    fake_data.mkdir(parents=True)
    config_dir.mkdir()
    status_dir.mkdir()

    # Write a minimal processed session file so SessionContentService.summary
    # and sessions_in_range have something to read.
    session_file = fake_data / "21001-processed.json"
    session_file.write_text(
        json.dumps({
            "meta": {"dateStart": "2026-01-15T09:00:00+01:00", "dateEnd": "2026-01-15T17:00:00+01:00"},
            "data": [
                {
                    "speechIndex": 1,
                    "people": [{"label": "Alice MP", "wid": "Q1"}],
                    "agendaItem": {"officialTitle": "Topic A"},
                    "textContents": [{"textBody": [{"speaker": "Alice MP", "text": "Hello world.", "type": "speech"}]}],
                    "media": {"videoFileURI": "https://example.com/1.mp4", "duration": 120},
                    "debug": {"proceedingIndex": 5, "align-duration": 3.2, "ner-duration": 0.5},
                },
                {
                    "speechIndex": 2,
                    "people": [{"label": "Bob MP"}],
                    "agendaItem": {"officialTitle": "Topic B"},
                    "textContents": [{"textBody": [{"speaker": "Bob MP", "text": "Second speech.", "type": "speech"}]}],
                    "media": {},
                    "debug": {},
                },
            ],
        }),
        encoding="utf-8",
    )

    repo_root = Path(__file__).resolve().parent.parent
    for sample in ("users.yaml.sample", "schedules.yaml.sample", "notifications.yaml.sample"):
        shutil.copy(repo_root / "config" / sample, config_dir / sample.replace(".sample", ""))

    (config_dir / "parliaments.yaml").write_text(
        f"""
parliaments:
  DE:
    name: "Deutscher Bundestag"
    language: "deu"
    tools_dir: "{data_dir}"
    data_dir: "{fake_data}"
    current_period: 21
    periods: [21]
    git_remote: ""
    retry_count: 1
    stages:
      download: true
      parse: true
      merge: true
      nel: true
      align: true
      ner: false
""",
        encoding="utf-8",
    )
    (config_dir / "users.yaml").write_text(
        """
users:
  - username: "testuser"
    role: admin
""",
        encoding="utf-8",
    )

    monkeypatch.setenv("CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("STATUS_DIR", str(status_dir))
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("GITHUB_CLIENT_ID", "test-id")
    monkeypatch.setenv("GITHUB_CLIENT_SECRET", "test-secret")
    monkeypatch.setenv("JWT_SECRET", "x" * 48)
    monkeypatch.setenv("BASE_URL", "http://localhost:8000")

    # Reset cached Settings/AppConfig + any singletons from prior test modules.
    for m in [m for m in list(sys.modules) if m.startswith("src.")]:
        sys.modules.pop(m, None)

    # Fake parliament module so the runner/status-tracker imports succeed.
    import types
    import logging

    for pkg_name in ("optv", "optv.parliaments", "optv.parliaments.DE"):
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = []
        sys.modules[pkg_name] = pkg
    common = types.ModuleType("optv.parliaments.DE.common")

    fake_sessions = ["21001"]

    class FakeConfig:
        def __init__(self, data_dir):
            self.data_dir = Path(data_dir)

        def sessions(self, prefix=""):
            return [s for s in fake_sessions if s.startswith(prefix)]

        def status(self, session):
            return set()

        def file(self, session, kind):
            return self.data_dir / f"{session}-{kind}.json"

    common.Config = FakeConfig
    common.SessionStatus = types.SimpleNamespace()
    sys.modules["optv.parliaments.DE.common"] = common

    workflow = types.ModuleType("optv.parliaments.DE.workflow")
    workflow.logger = logging.getLogger("optv.parliaments.DE.workflow")

    def execute_workflow(args):
        workflow.logger.warning("Publishing 21001 from 21001-merged.json")

    workflow.execute_workflow = execute_workflow
    sys.modules["optv.parliaments.DE.workflow"] = workflow

    from src.auth import jwt as app_jwt
    from src.main import app

    token = app_jwt.encode("x" * 48, {"sub": "testuser", "role": "admin"})

    with TestClient(app) as client:
        client.cookies.set("optv_token", token)
        yield client


# --- Basic / auth ---


def test_health(app_client):
    r = app_client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "healthy"}


def test_auth_me(app_client):
    r = app_client.get("/auth/me")
    assert r.status_code == 200
    assert r.json()["username"] == "testuser"
    assert r.json()["role"] == "admin"


def test_unauthorized_without_cookie(app_client):
    app_client.cookies.clear()
    r = app_client.get("/api/parliaments")
    assert r.status_code == 401


# --- API: parliaments ---


def test_list_parliaments(app_client):
    r = app_client.get("/api/parliaments")
    assert r.status_code == 200
    body = r.json()
    assert len(body["parliaments"]) == 1
    assert body["parliaments"][0]["id"] == "DE"


def test_parliament_stats(app_client):
    r = app_client.get("/api/parliaments/DE/stats")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == "DE"
    assert body["session_count"] == 1
    assert body["date_start"].startswith("2026-01-15")
    # Speech count was deliberately removed — parliament-level aggregates
    # must stay cheap.
    assert "speech_count" not in body
    assert "speech_count_computing" not in body


def test_parliament_stats_unknown(app_client):
    r = app_client.get("/api/parliaments/XX/stats")
    assert r.status_code == 404


def test_session_status_cheap_skips_full_parse(app_client):
    """The sessions list must use file-existence status, not Config.status()."""
    import sys

    from src.services.status_tracker import get_tracker

    get_tracker().invalidate()
    common = sys.modules["optv.parliaments.DE.common"]
    original = common.Config.status

    def boom(self, session):
        raise AssertionError("Config.status must not be called by the cheap path")

    common.Config.status = boom
    try:
        r = app_client.get("/parliaments/DE/sessions/list?period=21")
        assert r.status_code == 200
        assert "21001" in r.text
    finally:
        common.Config.status = original


def test_list_sessions_by_period(app_client):
    r = app_client.get("/api/parliaments/DE/periods/21/sessions")
    assert r.status_code == 200
    body = r.json()
    assert body["sessions"][0]["id"] == "21001"


# --- API: session content ---


def test_session_summary(app_client):
    r = app_client.get("/api/parliaments/DE/sessions/21001/summary")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == "21001"
    assert body["speech_count"] == 2
    assert body["agenda_item_count"] == 2
    assert body["date_start"].startswith("2026-01-15")


def test_session_content(app_client):
    r = app_client.get("/api/parliaments/DE/sessions/21001/content")
    assert r.status_code == 200
    body = r.json()
    assert len(body["agenda_items"]) == 2
    titles = [a["title"] for a in body["agenda_items"]]
    assert "Topic A" in titles and "Topic B" in titles


def test_session_speech(app_client):
    r = app_client.get("/api/parliaments/DE/sessions/21001/speeches/1")
    assert r.status_code == 200
    body = r.json()
    assert body["speech_index"] == 1
    assert body["speaker"] == "Alice MP"
    assert body["video_uri"] == "https://example.com/1.mp4"


def test_session_speech_missing(app_client):
    r = app_client.get("/api/parliaments/DE/sessions/21001/speeches/99")
    assert r.status_code == 404


# --- API: jobs + sessions rerun ---


def test_create_and_list_job(app_client):
    r = app_client.post(
        "/api/parliaments/DE/jobs",
        json={"period": 21, "stages": ["merge"]},
    )
    assert r.status_code == 200
    job_id = r.json()["job_id"]

    r = app_client.get("/api/parliaments/DE/jobs")
    assert r.status_code == 200
    body = r.json()
    all_ids = (
        [body["current"]["id"]] if body["current"] else []
    ) + [j["id"] for j in body["queue"]] + [j["id"] for j in body["recent"]]
    assert job_id in all_ids


def test_rerun_session(app_client):
    r = app_client.post(
        "/api/parliaments/DE/sessions/21001/rerun",
        json={"stages": ["merge"]},
    )
    assert r.status_code == 200
    assert "job_id" in r.json()


def test_create_job_rejects_disabled_stage(app_client):
    # `ner` is `false` in the test parliament's stages — must be rejected, not queued.
    r = app_client.post(
        "/api/parliaments/DE/jobs",
        json={"period": 21, "stages": ["merge", "ner"]},
    )
    assert r.status_code == 400
    assert "ner" in r.json()["detail"]


def test_create_job_allows_publish(app_client):
    # `publish` isn't gated by parliaments.yaml `stages:` — it must pass the guard.
    r = app_client.post(
        "/api/parliaments/DE/jobs",
        json={"period": 21, "stages": ["publish"]},
    )
    assert r.status_code == 200


def test_rerun_session_rejects_disabled_stage(app_client):
    r = app_client.post(
        "/api/parliaments/DE/sessions/21001/rerun",
        json={"stages": ["ner"]},
    )
    assert r.status_code == 400
    assert "ner" in r.json()["detail"]


def test_rerun_by_date(app_client):
    r = app_client.post(
        "/api/parliaments/DE/sessions/rerun-by-date",
        json={
            "date_from": "2026-01-01",
            "date_to": "2026-12-31",
            "period": 21,
            "stages": ["merge"],
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["session_count"] == 1


def test_bad_parliament_rejected(app_client):
    r = app_client.post(
        "/api/parliaments/XX/jobs",
        json={"period": 21, "stages": ["merge"]},
    )
    assert r.status_code == 404


# --- Page routes (HTML) ---


def test_login_page_anonymous(app_client):
    app_client.cookies.clear()
    r = app_client.get("/login")
    assert r.status_code == 200
    assert "Sign in with GitHub" in r.text


def test_root_redirects_anonymous(app_client):
    app_client.cookies.clear()
    r = app_client.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


def test_root_redirects_to_dashboard(app_client):
    r = app_client.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/dashboard"


def test_dashboard_page(app_client):
    r = app_client.get("/dashboard")
    assert r.status_code == 200
    assert "Deutscher Bundestag" in r.text
    assert 'hx-get="/dashboard/current"' in r.text


def test_parliaments_index_redirects_to_dashboard(app_client):
    r = app_client.get("/parliaments", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/dashboard"


def test_parliament_landing(app_client):
    r = app_client.get("/parliaments/DE")
    assert r.status_code == 200
    assert "Electoral periods" in r.text


def test_sessions_page(app_client):
    r = app_client.get("/parliaments/DE/sessions")
    assert r.status_code == 200
    assert "Filter (regex" in r.text


def test_sessions_list_fragment(app_client):
    r = app_client.get("/parliaments/DE/sessions/list?period=21")
    assert r.status_code == 200
    assert "21001" in r.text


def test_session_detail_page(app_client):
    r = app_client.get("/parliaments/DE/sessions/21001")
    assert r.status_code == 200
    assert "Topic A" in r.text
    assert "Alice MP" in r.text


def test_jobs_page(app_client):
    r = app_client.get("/parliaments/DE/jobs")
    assert r.status_code == 200


def test_schedules_page(app_client):
    r = app_client.get("/parliaments/DE/schedules")
    assert r.status_code == 200


def test_schedules_list_fragment(app_client):
    r = app_client.get("/parliaments/DE/schedules/list")
    assert r.status_code == 200


def test_old_routes_gone(app_client):
    assert app_client.get("/sessions", follow_redirects=False).status_code in (404, 405)
    assert app_client.get("/jobs", follow_redirects=False).status_code in (404, 405)
    assert app_client.get("/schedules", follow_redirects=False).status_code in (404, 405)
