"""Settings for the browser build, which has no .env file: they live in the app database.

Saved values are applied as environment variables, so they go through exactly the
same validation (and hard sending limits) as `.env` on a local install.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import ValidationError
from sqlmodel import Session

from .config import Settings, get_settings, reset_settings
from .db import state_get, state_set

STATE_KEY = "web_settings"
_defaults: dict[str, str] = {}


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    kind: str = "text"  # text | password | number | bool | select | time
    group: str = ""
    help: str = ""
    options: tuple[str, ...] = ()


FIELDS: tuple[Field, ...] = (
    Field("email_backend", "Sending mode", "select", "Email", options=("dry_run", "gmail_web"),
          help="Dry run keeps every email in the outbox without sending it. Gmail sends from your own account "
               "while this tab is open (needs a Google OAuth client ID, see the README)."),
    Field("gmail_client_id", "Google OAuth client ID", "text", "Email",
          help="A \"Web application\" OAuth client whose authorised JavaScript origin is this site's origin."),
    Field("sender_name", "Your name in the From line", "text", "Email", help="Defaults to the profile name."),
    Field("sender_email", "Your email address", "text", "Email", help="Defaults to the profile email."),
    Field("attach_resume", "Attach the tailored resume PDF", "bool", "Email"),
    Field("followup_auto_queue", "Queue one polite follow-up automatically", "bool", "Email"),
    Field("followup_after_days", "Follow up after (days)", "number", "Email"),
    Field("daily_send_limit", "Emails per 24 hours", "number", "Sending limits",
          help="Hard maximum 20 - higher values are clamped."),
    Field("send_delay_min_seconds", "Minimum gap between emails (seconds)", "number", "Sending limits",
          help="Never less than 180 (3 minutes)."),
    Field("send_delay_max_seconds", "Maximum gap between emails (seconds)", "number", "Sending limits"),
    Field("send_window_start", "Send window opens", "time", "Sending limits"),
    Field("send_window_end", "Send window closes", "time", "Sending limits"),
    Field("send_weekdays_only", "Weekdays only", "bool", "Sending limits"),
    Field("per_company_daily_cap", "Emails per company per day", "number", "Sending limits"),
    Field("app_timezone", "Time zone", "text", "Sending limits", help="IANA name, e.g. Asia/Kolkata."),
    Field("harvest_interval_hours", "Re-check job boards every (hours)", "number", "Job discovery"),
    Field("max_required_years", "Hide roles asking for more than (years)", "number", "Job discovery"),
    Field("include_internships", "Include internships", "bool", "Job discovery"),
    Field("location_keywords", "Only these locations (comma separated, empty = all)", "text", "Job discovery"),
    Field("auto_tailor_new_jobs", "Compile a tailored resume for every new job", "bool", "Job discovery"),
    Field("contact_providers", "Contact finders (comma separated)", "text", "Contacts",
          help="Any of: hunter, apollo, brave, google_cse, duckduckgo. Only Google's API answers browsers "
               "directly; the others need the CORS relay below."),
    Field("hunter_api_key", "Hunter.io API key", "password", "Contacts"),
    Field("apollo_api_key", "Apollo API key", "password", "Contacts"),
    Field("brave_search_api_key", "Brave Search API key", "password", "Contacts"),
    Field("google_cse_api_key", "Google Programmable Search API key", "password", "Contacts"),
    Field("google_cse_id", "Google Programmable Search engine ID", "text", "Contacts"),
    Field("cors_proxy", "CORS relay (optional)", "text", "Contacts",
          help="Relays requests that browsers block, e.g. https://corsproxy.io/?url={url}. "
               "The relay can read those requests, including API keys in them."),
)
FIELD_NAMES = {field.name for field in FIELDS}


def groups() -> list[tuple[str, list[Field]]]:
    ordered: dict[str, list[Field]] = {}
    for field in FIELDS:
        ordered.setdefault(field.group, []).append(field)
    return list(ordered.items())


def display_value(settings: Settings, field: Field) -> str:
    value = getattr(settings, field.name)
    if isinstance(value, list):
        return ", ".join(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return "" if value is None else str(value)


def current_values(settings: Settings | None = None) -> dict[str, str]:
    settings = settings or get_settings()
    return {field.name: display_value(settings, field) for field in FIELDS}


def from_form(form: dict[str, str]) -> dict[str, str]:
    """Normalise submitted form fields (unchecked checkboxes are simply absent)."""
    values: dict[str, str] = {}
    for field in FIELDS:
        raw = form.get(field.name)
        if field.kind == "bool":
            values[field.name] = "true" if raw in ("true", "on", "1") else "false"
        else:
            values[field.name] = (raw or "").strip()
    return values


def validate(values: dict[str, str]) -> list[str]:
    try:
        Settings(**{name: value for name, value in values.items() if name in FIELD_NAMES and value != ""})
    except ValidationError as exc:
        return [f"{'.'.join(str(p) for p in error['loc'])}: {error['msg']}" for error in exc.errors()]
    zone = values.get("app_timezone", "")
    if zone:
        try:
            ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError):
            return [f"app_timezone: unknown time zone {zone!r} (use a name like Asia/Kolkata)"]
    return []


def load(session: Session) -> dict[str, str]:
    raw = state_get(session, STATE_KEY)
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {}
    return {k: str(v) for k, v in data.items() if k in FIELD_NAMES} if isinstance(data, dict) else {}


def save(session: Session, values: dict[str, str]) -> None:
    state_set(session, STATE_KEY, json.dumps({k: v for k, v in values.items() if k in FIELD_NAMES}))


def set_defaults(values: dict[str, str]) -> None:
    """Values used when a field is left empty (the browser build passes e.g. the local time zone)."""
    _defaults.clear()
    _defaults.update({k: v for k, v in values.items() if k in FIELD_NAMES})


def apply(values: dict[str, str]) -> None:
    """Expose saved values to Settings (empty means "use the default") and rebuild the sender."""
    for name in FIELD_NAMES:
        os.environ.pop(name.upper(), None)
    for name, value in {**_defaults, **{k: v for k, v in values.items() if v != ""}}.items():
        if name in FIELD_NAMES and value != "":
            os.environ[name.upper()] = value
    reset_settings()
    from .runtime import set_queue

    set_queue(None)
