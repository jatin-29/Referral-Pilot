"""Shared fixtures: every test runs against its own SQLite file and exports folder."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from referralpilot import db, fetch
from referralpilot.activity import shutdown_logging
from referralpilot.config import Settings, reset_settings

ISOLATED_ENV = {
    "EMAIL_BACKEND": "dry_run",
    "VERIFY_MX": "false",
    "LOCATION_KEYWORDS": "",
    "CONTACT_PROVIDERS": "",
    "HUNTER_API_KEY": "",
    "SEND_WINDOW_START": "00:00",
    "SEND_WINDOW_END": "00:00",
    "SEND_WEEKDAYS_ONLY": "false",
    "SCHEDULER_ENABLED": "false",
    "AUTO_TAILOR_NEW_JOBS": "false",
    "LATEX_ENGINE": "auto",
    "DEMO_MODE": "false",
    "SENDER_EMAIL": "",
    "SENDER_NAME": "",
}


@pytest.fixture()
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Isolated settings + database. Returns the Settings object."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setitem(Settings.model_config, "env_file", None)  # ignore a developer's real .env
    for key, value in ISOLATED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'data' / 'test.db').as_posix()}")
    monkeypatch.setenv("EXPORTS_DIR", str(tmp_path / "exports"))
    monkeypatch.setattr(fetch, "sleep", lambda _seconds: None)
    shutdown_logging()  # never let a previous test's log writer touch this database
    reset_settings()
    db.set_engine(db.make_engine(os.environ["DATABASE_URL"]))
    db.init_db()

    from referralpilot import runtime
    from referralpilot.config import get_settings

    runtime.set_queue(None)
    yield get_settings()
    shutdown_logging()  # drain queued log records into *this* test database first
    runtime.set_queue(None)
    db.set_engine(None)
    reset_settings()


@pytest.fixture()
def profile(workspace):
    from referralpilot.db import session_scope
    from referralpilot.seed import seed_profile

    with session_scope() as session:
        created, _ = seed_profile(session)
        return created


@pytest.fixture()
def demo_jobs(profile):
    """Harvest the bundled mock boards; returns {title: job_id}."""
    from sqlmodel import select

    from referralpilot.db import session_scope
    from referralpilot.demo import DEMO_COMPANIES, demo_client
    from referralpilot.harvester import harvest
    from referralpilot.models import Job
    from referralpilot.seed import upsert_company

    with session_scope() as session:
        for entry in DEMO_COMPANIES:
            upsert_company(session, entry)
    with demo_client() as client:
        harvest(client=client)
    with session_scope() as session:
        return {f"{job.company}|{job.title}": job.id for job in session.exec(select(Job)).all()}
