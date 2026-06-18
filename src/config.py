"""Configuration loader.

Reads environment variables from `config/secrets.env` (via docker env_file or
pydantic-settings) and merges with the YAML config files under `CONFIG_DIR`.

Per-parliament metadata that's a code capability (name, language, periods,
supported stages, official entity-dump URL, default retry tuning) lives in
the Tools repo at `optv/parliaments/<id>/manifest.yaml` and is loaded via
the `optv.parliaments.load_manifest()` helper. Conductor's `parliaments.yaml`
holds deployment-only fields (paths, git remotes, stage selection) and may
override any manifest field.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# Synthetic identity used for every request when AUTH_ENABLED is false. Copy it
# (dict(ANONYMOUS_ADMIN)) before handing it to a request so callers can't mutate
# the shared instance.
ANONYMOUS_ADMIN = {"username": "local-admin", "role": "admin", "avatar_url": None}


class Settings(BaseSettings):
    """Environment-backed secrets and paths."""

    model_config = SettingsConfigDict(
        env_file=os.environ.get("SECRETS_ENV_FILE", "config/secrets.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Master switch for GitHub auth. When false, login is bypassed entirely and
    # every request is treated as ANONYMOUS_ADMIN — no OAuth creds, JWT secret,
    # or users.yaml are required. Read once at startup; restart to change.
    auth_enabled: bool = Field(default=True, alias="AUTH_ENABLED")

    github_client_id: str = Field(default="", alias="GITHUB_CLIENT_ID")
    github_client_secret: str = Field(default="", alias="GITHUB_CLIENT_SECRET")
    jwt_secret: str = Field(default="", alias="JWT_SECRET")
    base_url: str = Field(default="http://localhost:8000", alias="BASE_URL")
    ner_api_endpoint: str = Field(default="", alias="NER_API_ENDPOINT")
    slack_webhook_url: str = Field(default="", alias="SLACK_WEBHOOK_URL")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    config_dir: str = Field(default="config", alias="CONFIG_DIR")
    status_dir: str = Field(default="status", alias="STATUS_DIR")
    data_dir: str = Field(default="data", alias="DATA_DIR")

    # Git identity for the publish step (commits to Data repos).
    git_user_name: str = Field(default="Conductor Bot", alias="GIT_USER_NAME")
    git_user_email: str = Field(default="conductor@example.com", alias="GIT_USER_EMAIL")

    def require(self, *names: str) -> None:
        missing = [n for n in names if not getattr(self, n, "")]
        if missing:
            raise RuntimeError(
                f"Missing required settings: {', '.join(missing)}. "
                "Fill them in config/secrets.env."
            )


# Canonical pipeline stage order (publish is handled separately by the runner).
PIPELINE_STAGE_ORDER = ("download", "parse", "merge", "nel", "align", "ner")


class ParliamentStages(BaseModel):
    download: bool = True
    parse: bool = True
    merge: bool = True
    nel: bool = True
    align: bool = True
    ner: bool = False


class ParliamentDefaults(BaseModel):
    """Top-level `defaults:` block in parliaments.yaml — apply to every parliament
    unless overridden per-parliament."""

    tools_dir: str = "data/OpenParliamentTV-Tools"
    data_root: str = "data"


class ParliamentConfig(BaseModel):
    name: str
    language: str
    tools_dir: str
    data_dir: str
    periods: list[int]
    # current_period is computed from max(periods) if not provided. Kept as a
    # writable field for backward compat with existing parliaments.yaml that
    # set it explicitly.
    current_period: int | None = None
    supported_stages: list[str] = Field(
        default_factory=lambda: ["download", "parse", "merge", "nel", "align", "ner"]
    )
    entity_dump_url: str = ""
    git_remote: str = ""  # used by setup.sh for clone + by UI for browsable URL
    retry_count: int = 20
    retry_delay_max: int = 10
    enabled: bool = True
    stages: ParliamentStages = ParliamentStages()
    cache_dir: str | None = None

    @model_validator(mode="after")
    def _default_current_period(self) -> "ParliamentConfig":
        if self.current_period is None and self.periods:
            self.current_period = max(self.periods)
        return self


def stage_disable_reasons(
    parliament: ParliamentConfig, settings: Settings
) -> dict[str, str | None]:
    """For each pipeline stage, a human-readable reason it can't run for this
    parliament on this deployment, or None if it's runnable.

    Three gates, checked most-fundamental first: (1) the Tools manifest lists
    the stage in `supported_stages` (the workflow implements it at all),
    (2) the deployment enabled it in parliaments.yaml `stages:`, and (3) machine
    capability — `ner` needs a configured NER endpoint to reach entity-fishing.
    Used to gray out checkboxes in the re-run dialogs and to reject non-runnable
    stages server-side (UI graying alone is cosmetic).
    """
    reasons: dict[str, str | None] = {}
    for s in PIPELINE_STAGE_ORDER:
        if s not in parliament.supported_stages:
            reasons[s] = "not supported by this parliament's workflow"
        elif not getattr(parliament.stages, s, False):
            reasons[s] = "disabled for this parliament in parliaments.yaml"
        elif s == "ner" and not settings.ner_api_endpoint:
            reasons[s] = "no NER endpoint configured (set NER_API_ENDPOINT)"
        else:
            reasons[s] = None
    return reasons


class UserEntry(BaseModel):
    username: str
    role: str  # "admin" | "editor" | "viewer"


class ScheduleConfig(BaseModel):
    enabled: bool = True
    parliament: str
    cron: str
    stages: list[str]
    description: str = ""
    publish_on_success: bool = False
    force: bool = False


class SlackConfig(BaseModel):
    enabled: bool = True
    on_failure: bool = True
    on_partial_failure: bool = True
    on_success: bool = False
    scheduled_only: bool = False


class NotificationsConfig(BaseModel):
    slack: SlackConfig = SlackConfig()


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


# Manifest fields → ParliamentConfig fields (manifest provides defaults that
# parliaments.yaml may override).
_MANIFEST_TO_CONFIG = {
    "name": "name",
    "language": "language",
    "periods": "periods",
    "supported_stages": "supported_stages",
    "entity_dump_url": "entity_dump_url",
    "default_retry_count": "retry_count",
    "default_retry_delay_max": "retry_delay_max",
}


def _load_manifests() -> tuple[set[str], dict[str, dict[str, Any]]]:
    """Return (set of available parliament ids, dict of id→manifest).

    Importing optv.parliaments requires Tools to be on PYTHONPATH (Dockerfile
    sets it; dev's `PYTHONPATH=data/OpenParliamentTV-Tools uvicorn ...` does too).
    If the import fails, return empty results and let parliaments.yaml provide
    everything explicitly — this preserves test setups that mock parliaments
    via sys.modules without shipping a real `optv.parliaments` package.
    """
    try:
        from optv.parliaments import list_parliaments, load_manifest
    except ImportError:
        logger.warning(
            "Could not import optv.parliaments — manifest-based defaults disabled. "
            "Parliaments.yaml must provide all fields explicitly."
        )
        return set(), {}

    available = set(list_parliaments())
    manifests: dict[str, dict[str, Any]] = {}
    for pid in available:
        try:
            manifests[pid] = load_manifest(pid)
        except Exception as exc:
            logger.warning("Failed to load manifest for %s: %s", pid, exc)
    return available, manifests


def load_parliaments(config_dir: Path) -> dict[str, ParliamentConfig]:
    raw = _load_yaml(config_dir / "parliaments.yaml")
    defaults = ParliamentDefaults(**(raw.get("defaults") or {}))
    parliaments_raw = raw.get("parliaments") or {}

    available_manifests, manifests = _load_manifests()

    result: dict[str, ParliamentConfig] = {}
    for pid, override in parliaments_raw.items():
        merged: dict[str, Any] = {}

        # Layer 1: manifest defaults (if available)
        if pid in manifests:
            for mkey, ckey in _MANIFEST_TO_CONFIG.items():
                if mkey in manifests[pid]:
                    merged[ckey] = manifests[pid][mkey]
        elif pid in parliaments_raw:
            logger.warning(
                "Parliament %r has no manifest in Tools — parliaments.yaml must "
                "provide name/language/periods/etc. explicitly.",
                pid,
            )

        # Layer 2: convention defaults for paths
        merged.setdefault("tools_dir", defaults.tools_dir)
        merged.setdefault("data_dir", f"{defaults.data_root}/OpenParliamentTV-Data-{pid}")

        # Layer 3: parliaments.yaml per-parliament overrides
        merged.update(override or {})

        if not merged.get("enabled", True):
            logger.info("Parliament %r is disabled — skipping", pid)
            continue

        try:
            result[pid] = ParliamentConfig(**merged)
        except Exception:
            logger.error("Failed to load ParliamentConfig for %r from %s", pid, merged)
            raise

    # Inform about manifests not enabled in this deployment
    for pid in sorted(available_manifests - set(parliaments_raw.keys())):
        logger.info(
            "Manifest exists for parliament %r but it's not in parliaments.yaml — "
            "add it there to enable.",
            pid,
        )

    return result


def load_users(config_dir: Path) -> dict[str, UserEntry]:
    entries = _load_yaml(config_dir / "users.yaml").get("users") or []
    return {u["username"]: UserEntry(**u) for u in entries}


def load_schedules(config_dir: Path) -> dict[str, ScheduleConfig]:
    raw = _load_yaml(config_dir / "schedules.yaml").get("schedules") or {}
    return {sid: ScheduleConfig(**data) for sid, data in raw.items()}


def load_notifications(config_dir: Path) -> NotificationsConfig:
    raw = _load_yaml(config_dir / "notifications.yaml")
    return NotificationsConfig(**raw) if raw else NotificationsConfig()


class AppConfig:
    """Aggregated runtime configuration.

    Yaml files are re-read on demand via `reload()`. Secrets are read once at
    startup — restart to change env vars.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.config_dir = Path(settings.config_dir).resolve()
        self.status_dir = Path(settings.status_dir).resolve()
        self.data_dir = Path(settings.data_dir).resolve()
        self.parliaments: dict[str, ParliamentConfig] = {}
        self.users: dict[str, UserEntry] = {}
        self.schedules: dict[str, ScheduleConfig] = {}
        self.notifications: NotificationsConfig = NotificationsConfig()
        self.reload()

    def reload(self) -> None:
        self.parliaments = load_parliaments(self.config_dir)
        self.users = load_users(self.config_dir)
        self.schedules = load_schedules(self.config_dir)
        self.notifications = load_notifications(self.config_dir)

    def validate_startup(self) -> None:
        if self.settings.auth_enabled:
            self.settings.require("github_client_id", "github_client_secret", "jwt_secret")
        else:
            logger.warning(
                "AUTH_ENABLED is false — GitHub login is bypassed and every request "
                "has admin access. Anyone who can reach this server controls it."
            )
        if not self.parliaments:
            raise RuntimeError(
                f"No parliaments configured. Copy "
                f"{self.config_dir}/parliaments.yaml.sample to parliaments.yaml."
            )
        if self.settings.auth_enabled and not self.users:
            raise RuntimeError(
                f"No authorized users configured. Copy "
                f"{self.config_dir}/users.yaml.sample to users.yaml and add your GitHub username."
            )
        for sid, sched in self.schedules.items():
            if sched.parliament not in self.parliaments:
                raise RuntimeError(
                    f"Schedule '{sid}' references unknown parliament '{sched.parliament}'."
                )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    return AppConfig(get_settings())
