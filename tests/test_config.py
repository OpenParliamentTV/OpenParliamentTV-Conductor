from __future__ import annotations

import sys

import pytest


@pytest.fixture(autouse=True)
def _reset_src_modules():
    for m in [m for m in list(sys.modules) if m.startswith("src.")]:
        sys.modules.pop(m, None)
    yield


def _set_env(monkeypatch, tmp_path, **overrides):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("STATUS_DIR", str(tmp_path / "status"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("GITHUB_CLIENT_ID", overrides.get("gh_id", "abc"))
    monkeypatch.setenv("GITHUB_CLIENT_SECRET", overrides.get("gh_secret", "xyz"))
    monkeypatch.setenv("JWT_SECRET", overrides.get("jwt_secret", "x" * 48))
    monkeypatch.setenv("BASE_URL", "http://localhost:8000")


def _write_valid_yaml(cfg_dir):
    (cfg_dir / "parliaments.yaml").write_text(
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
    (cfg_dir / "users.yaml").write_text(
        """
users:
  - username: alice
    role: admin
""",
        encoding="utf-8",
    )


def test_valid_config_loads(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    cfg.mkdir()
    _write_valid_yaml(cfg)
    _set_env(monkeypatch, tmp_path)

    from src.config import AppConfig, Settings

    app_config = AppConfig(Settings())
    app_config.validate_startup()
    assert "DE" in app_config.parliaments
    assert app_config.parliaments["DE"].current_period == 21
    assert app_config.users["alice"].role == "admin"


def test_missing_users_rejected(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    cfg.mkdir()
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
    _set_env(monkeypatch, tmp_path)

    from src.config import AppConfig, Settings

    app_config = AppConfig(Settings())
    with pytest.raises(RuntimeError, match="authorized users"):
        app_config.validate_startup()


def test_missing_jwt_secret_rejected(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    cfg.mkdir()
    _write_valid_yaml(cfg)
    _set_env(monkeypatch, tmp_path, jwt_secret="")

    from src.config import AppConfig, Settings

    with pytest.raises(RuntimeError, match="jwt_secret"):
        AppConfig(Settings()).validate_startup()


def test_schedule_referencing_unknown_parliament_rejected(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    cfg.mkdir()
    _write_valid_yaml(cfg)
    (cfg / "schedules.yaml").write_text(
        """
schedules:
  broken:
    parliament: UNKNOWN
    cron: "0 2 * * *"
    stages: [merge]
""",
        encoding="utf-8",
    )
    _set_env(monkeypatch, tmp_path)

    from src.config import AppConfig, Settings

    with pytest.raises(RuntimeError, match="UNKNOWN"):
        AppConfig(Settings()).validate_startup()


def test_manifest_provides_defaults_yaml_overrides(tmp_path, monkeypatch):
    """Parliaments.yaml without name/language/etc. picks them up from the
    Tools manifest; explicit values in parliaments.yaml override the manifest."""
    # Other tests inject stub `optv.*` modules into sys.modules — clear them
    # so the real `from optv.parliaments import load_manifest` resolves.
    for m in [m for m in list(sys.modules) if m.startswith("optv")]:
        sys.modules.pop(m, None)
    cfg = tmp_path / "config"
    cfg.mkdir()
    # Bare per-parliament block — relies entirely on manifest defaults except
    # for the explicit `entity_dump_url` override below.
    (cfg / "parliaments.yaml").write_text(
        """
defaults:
  tools_dir: data/OpenParliamentTV-Tools
  data_root: data
parliaments:
  DE:
    git_remote: "git@github.com:fork/Data-DE.git"
    entity_dump_url: "https://my-fork/entities.json"
""",
        encoding="utf-8",
    )
    (cfg / "users.yaml").write_text(
        """
users:
  - username: alice
    role: admin
""",
        encoding="utf-8",
    )
    _set_env(monkeypatch, tmp_path)

    from src.config import AppConfig, Settings

    app_config = AppConfig(Settings())
    de = app_config.parliaments["DE"]
    # From manifest:
    assert de.name == "Deutscher Bundestag"
    assert de.language == "deu"
    assert 21 in de.periods
    # Computed from manifest's periods:
    assert de.current_period == max(de.periods)
    # From parliaments.yaml override:
    assert de.git_remote == "git@github.com:fork/Data-DE.git"
    assert de.entity_dump_url == "https://my-fork/entities.json"
    # From defaults block + convention:
    assert de.tools_dir == "data/OpenParliamentTV-Tools"
    assert de.data_dir == "data/OpenParliamentTV-Data-DE"


def test_reload_picks_up_changes(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    cfg.mkdir()
    _write_valid_yaml(cfg)
    _set_env(monkeypatch, tmp_path)

    from src.config import AppConfig, Settings

    app_config = AppConfig(Settings())
    assert app_config.schedules == {}

    (cfg / "schedules.yaml").write_text(
        """
schedules:
  nightly:
    parliament: DE
    cron: "0 2 * * *"
    stages: [merge]
""",
        encoding="utf-8",
    )
    app_config.reload()
    assert "nightly" in app_config.schedules
    assert app_config.schedules["nightly"].stages == ["merge"]


def test_corrupt_schedules_yaml_falls_back_to_none(tmp_path):
    from src.config import load_schedules

    cfg = tmp_path / "config"
    cfg.mkdir()
    # Half-written YAML, as a process killed mid-rewrite might leave behind.
    (cfg / "schedules.yaml").write_text("schedules:\n  nightly:\n    cron: \"0 2 * *", encoding="utf-8")
    assert load_schedules(cfg) == {}
