"""Whole-database backup, restore and reset (the browser build keeps everything in one SQLite file)."""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from pathlib import Path

from .config import get_settings
from .db import get_engine, init_db, session_scope, set_engine
from .seed import seed_all

SQLITE_MAGIC = b"SQLite format 3\x00"
REQUIRED_TABLES = {"job", "company", "candidateprofile", "outreachlog", "referralcontact"}


class BackupError(ValueError):
    pass


def database_path() -> Path:
    url = get_settings().sqlalchemy_url
    if not url.startswith("sqlite:///") or ":memory:" in url:
        raise BackupError("Backups need a file-based SQLite database")
    return Path(url.split("sqlite:///", 1)[1])


def backup_bytes() -> bytes:
    """A consistent copy of the database (SQLite's online backup API)."""
    get_engine().dispose()  # release pooled connections so nothing holds a stale view
    with tempfile.TemporaryDirectory(prefix="rp-backup-") as tmp:
        target = Path(tmp) / "backup.db"
        source = sqlite3.connect(database_path())
        try:
            copy = sqlite3.connect(target)
            try:
                source.backup(copy)
            finally:
                copy.close()
        finally:
            source.close()
        return target.read_bytes()


def _check(data: bytes) -> None:
    if not data.startswith(SQLITE_MAGIC):
        raise BackupError("That file is not a ReferralPilot backup (not an SQLite database)")
    with tempfile.TemporaryDirectory(prefix="rp-restore-") as tmp:
        path = Path(tmp) / "check.db"
        path.write_bytes(data)
        conn = sqlite3.connect(path)
        try:
            tables = {row[0].lower() for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            ok = conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        except sqlite3.DatabaseError as exc:
            raise BackupError(f"The backup is damaged: {exc}") from exc
        finally:
            conn.close()
    if not ok:
        raise BackupError("The backup failed SQLite's integrity check")
    missing = REQUIRED_TABLES - tables
    if missing:
        raise BackupError(f"Not a ReferralPilot backup (missing tables: {', '.join(sorted(missing))})")


def _reopen() -> None:
    from .runtime import set_queue

    set_engine(None)
    init_db()  # adds tables that a backup from an older version lacks
    with session_scope() as session:
        from .profile import get_active_profile

        if get_active_profile(session) is None:
            seed_all(session)
    set_queue(None)


def restore_bytes(data: bytes) -> None:
    _check(data)
    path = database_path()
    set_engine(None)
    for suffix in ("-journal", "-wal", "-shm"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)
    path.write_bytes(data)
    _reopen()


def reset_all() -> None:
    """Delete every job, contact, email and setting, then start again with the sample data."""
    path = database_path()
    set_engine(None)
    for suffix in ("", "-journal", "-wal", "-shm"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)
    exports = get_settings().exports_dir
    if exports.exists():
        for child in exports.iterdir():
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            elif child.name != ".gitkeep":
                child.unlink(missing_ok=True)
    _reopen()
