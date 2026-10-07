"""Application settings loaded from environment variables and `.env`."""

from __future__ import annotations

from datetime import time
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Non-negotiable safety rails: configuration may tighten these, never loosen them.
HARD_DAILY_SEND_CAP = 20
MIN_SEND_DELAY_SECONDS = 180

CommaList = Annotated[list[str], NoDecode]


def _split_csv(value: object) -> object:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


def _parse_hhmm(value: str) -> time:
    hours, _, minutes = value.strip().partition(":")
    return time(int(hours), int(minutes or 0))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT / ".env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- core ---
    data_dir: Path = PROJECT_ROOT / "data"
    database_url: str = ""
    exports_dir: Path = PROJECT_ROOT / "exports"
    templates_dir: Path = PROJECT_ROOT / "templates"
    config_dir: Path = PROJECT_ROOT / "config"
    app_timezone: str = "UTC"
    log_level: str = "INFO"

    # --- harvester ---
    harvest_interval_hours: float = 6.0
    http_timeout_seconds: float = 20.0
    http_user_agent: str = "ReferralPilot/0.1 (personal job-search assistant)"
    http_max_retries: int = 2
    max_required_years: int = 2
    include_internships: bool = False
    location_keywords: CommaList = []
    respect_robots_txt: bool = True

    # --- tailor ---
    latex_engine: str = "auto"
    latex_timeout_seconds: int = 90
    max_projects_on_resume: int = 3
    auto_tailor_new_jobs: bool = False

    # --- prospector ---
    contact_providers: CommaList = []
    max_contacts_per_job: int = 5
    hunter_api_key: str = ""
    apollo_api_key: str = ""
    apollo_search_url: str = "https://api.apollo.io/api/v1/mixed_people/search"
    brave_search_api_key: str = ""
    google_cse_api_key: str = ""
    google_cse_id: str = ""
    verify_mx: bool = True

    # --- outreach ---
    email_backend: Literal["dry_run", "smtp", "gmail_api", "gmail_web"] = "dry_run"
    sender_name: str = ""
    sender_email: str = ""
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_use_ssl: bool = False
    imap_host: str = "imap.gmail.com"
    imap_port: int = 993
    imap_username: str = ""
    imap_password: str = ""
    gmail_credentials_file: Path = PROJECT_ROOT / "secrets" / "credentials.json"
    gmail_token_file: Path = PROJECT_ROOT / "secrets" / "token.json"
    daily_send_limit: int = HARD_DAILY_SEND_CAP
    send_delay_min_seconds: int = MIN_SEND_DELAY_SECONDS
    send_delay_max_seconds: int = 420
    send_window_start: str = "09:00"
    send_window_end: str = "19:00"
    send_weekdays_only: bool = False
    per_company_daily_cap: int = 3
    per_recipient_cooldown_days: int = 30
    require_approval: bool = True
    attach_resume: bool = True
    followup_after_days: float = 4.0
    followup_auto_queue: bool = True
    max_followups: int = 1
    reply_check_interval_minutes: int = 30

    # --- dashboard / scheduler ---
    web_host: str = "127.0.0.1"
    web_port: int = 8000
    scheduler_enabled: bool = True
    # Also load the Tailwind Play CDN (only needed after adding new utility classes to templates).
    ui_tailwind_cdn: bool = False
    # Serve every outbound HTTP call from the bundled mock API responses (offline demo).
    demo_mode: bool = False

    # --- browser build (GitHub Pages) ---
    # Set by referralpilot.web when the app runs inside the browser (Pyodide): no threads,
    # no sockets, no LaTeX; storage is the browser's IndexedDB.
    web_mode: bool = False
    # Base URL of the hosted site; the scheduled crawl publishes <site_url>/jobs.json.
    site_url: str = ""
    # OAuth client ID ("Web application") that lets the browser build send through the Gmail API.
    gmail_client_id: str = ""
    # Opt-in relay for APIs that refuse browser (CORS) requests, e.g. "https://corsproxy.io/?url={url}".
    # The relay sees those requests, including API keys in their URLs.
    cors_proxy: str = ""
    # SQLite write-ahead logging (the browser's virtual file system cannot memory-map it).
    sqlite_wal: bool = True

    @field_validator("location_keywords", "contact_providers", mode="before")
    @classmethod
    def _split_lists(cls, value: object) -> object:
        return _split_csv(value)

    @field_validator("contact_providers", mode="after")
    @classmethod
    def _lower_providers(cls, value: list[str]) -> list[str]:
        return [item.lower() for item in value]

    @field_validator("daily_send_limit")
    @classmethod
    def _clamp_daily_limit(cls, value: int) -> int:
        return max(1, min(int(value), HARD_DAILY_SEND_CAP))

    @field_validator("send_window_start", "send_window_end")
    @classmethod
    def _validate_window(cls, value: str) -> str:
        _parse_hhmm(value)
        return value.strip()

    @model_validator(mode="after")
    def _clamp_delays(self) -> Settings:
        self.send_delay_min_seconds = max(MIN_SEND_DELAY_SECONDS, int(self.send_delay_min_seconds))
        self.send_delay_max_seconds = max(self.send_delay_min_seconds, int(self.send_delay_max_seconds))
        self.max_followups = max(0, min(int(self.max_followups), 2))
        return self

    # --- derived values ---
    @property
    def sqlalchemy_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{(self.data_dir / 'referralpilot.db').as_posix()}"

    @property
    def tz(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.app_timezone)
        except (ZoneInfoNotFoundError, ValueError):
            return ZoneInfo("UTC")

    @property
    def window_start(self) -> time:
        return _parse_hhmm(self.send_window_start)

    @property
    def window_end(self) -> time:
        return _parse_hhmm(self.send_window_end)

    @property
    def outbox_dir(self) -> Path:
        return self.exports_dir / "outbox"

    @property
    def is_dry_run(self) -> bool:
        return self.email_backend == "dry_run"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings() -> None:
    """Drop the cached settings so the next call re-reads the environment (tests)."""
    get_settings.cache_clear()
