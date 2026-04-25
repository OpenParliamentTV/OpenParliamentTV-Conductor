from __future__ import annotations

import sys
import types

import pytest
from fastapi import HTTPException


@pytest.fixture
def loaded_app(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    status = tmp_path / "status"
    cfg.mkdir()
    status.mkdir()
    (cfg / "parliaments.yaml").write_text(
        """
parliaments:
  DE:
    name: Bundestag
    language: deu
    tools_dir: /tmp/tools
    data_dir: /tmp/data
    current_period: 21
    periods: [21]
""",
        encoding="utf-8",
    )
    (cfg / "users.yaml").write_text(
        """
users:
  - username: viewer_u
    role: viewer
  - username: editor_u
    role: editor
  - username: admin_u
    role: admin
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("CONFIG_DIR", str(cfg))
    monkeypatch.setenv("STATUS_DIR", str(status))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("GITHUB_CLIENT_ID", "a")
    monkeypatch.setenv("GITHUB_CLIENT_SECRET", "b")
    monkeypatch.setenv("JWT_SECRET", "x" * 48)
    monkeypatch.setenv("BASE_URL", "http://localhost:8000")

    for m in [m for m in list(sys.modules) if m.startswith("src.")]:
        sys.modules.pop(m, None)

    yield


def _make_token(role_user: str) -> str:
    from src.auth import jwt as app_jwt

    return app_jwt.encode("x" * 48, {"sub": role_user, "role": "ignored"})


def test_current_user_rejects_missing_cookie(loaded_app):
    from src.auth.dependencies import current_user
    from src.config import get_config

    with pytest.raises(HTTPException) as exc:
        current_user(token=None, config=get_config())
    assert exc.value.status_code == 401


def test_current_user_rejects_invalid_token(loaded_app):
    from src.auth.dependencies import current_user
    from src.config import get_config

    with pytest.raises(HTTPException) as exc:
        current_user(token="not-a-jwt", config=get_config())
    assert exc.value.status_code == 401


def test_current_user_rejects_user_not_in_allowlist(loaded_app):
    from src.auth.dependencies import current_user
    from src.config import get_config

    with pytest.raises(HTTPException) as exc:
        current_user(token=_make_token("stranger"), config=get_config())
    assert exc.value.status_code == 403


def test_current_user_returns_role_from_yaml(loaded_app):
    from src.auth.dependencies import current_user
    from src.config import get_config

    user = current_user(token=_make_token("viewer_u"), config=get_config())
    assert user["username"] == "viewer_u"
    assert user["role"] == "viewer"


def test_require_role_enforces_minimum(loaded_app):
    from src.auth.dependencies import require_role

    admin_only = require_role("admin")
    with pytest.raises(HTTPException) as exc:
        admin_only(user={"username": "v", "role": "viewer"})
    assert exc.value.status_code == 403

    editor_plus = require_role("editor")
    # editor passes editor-or-higher
    assert editor_plus(user={"username": "e", "role": "editor"})["role"] == "editor"
    # admin passes editor-or-higher
    assert editor_plus(user={"username": "a", "role": "admin"})["role"] == "admin"
    # viewer rejected
    with pytest.raises(HTTPException):
        editor_plus(user={"username": "v", "role": "viewer"})
