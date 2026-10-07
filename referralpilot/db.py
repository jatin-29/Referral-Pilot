"""Engine/session management for the SQLite database."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from .config import get_settings

_engine: Engine | None = None
_engine_lock = threading.Lock()


def _sqlite_pragmas(dbapi_connection, _record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.close()


def make_engine(url: str) -> Engine:
    connect_args: dict = {}
    if url.startswith("sqlite"):
        connect_args = {"check_same_thread": False, "timeout": 30}
        db_path = url.split("sqlite:///", 1)[-1]
        if db_path and db_path != url and ":memory:" not in db_path:
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, connect_args=connect_args)
    if url.startswith("sqlite"):
        event.listen(engine, "connect", _sqlite_pragmas)
    return engine


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                _engine = make_engine(get_settings().sqlalchemy_url)
    return _engine


def set_engine(engine: Engine | None) -> None:
    """Swap the global engine (used by tests and the verification script)."""
    global _engine
    with _engine_lock:
        if _engine is not None and _engine is not engine:
            _engine.dispose()
        _engine = engine


def init_db(engine: Engine | None = None) -> Engine:
    from . import models  # noqa: F401  (register tables)

    engine = engine or get_engine()
    SQLModel.metadata.create_all(engine)
    return engine


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope: commits on success, rolls back on error."""
    with Session(get_engine(), expire_on_commit=False) as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    with Session(get_engine(), expire_on_commit=False) as session:
        yield session


# --- tiny key/value helpers on AppState ---------------------------------------

def state_get(session: Session, key: str, default: str | None = None) -> str | None:
    from .models import AppState

    row = session.get(AppState, key)
    return row.value if row is not None and row.value is not None else default


def state_set(session: Session, key: str, value: str | None) -> None:
    from .models import AppState, utcnow

    row = session.get(AppState, key)
    if row is None:
        row = AppState(key=key, value=value)
    else:
        row.value = value
        row.updated_at = utcnow()
    session.add(row)


def state_get_datetime(session: Session, key: str) -> datetime | None:
    from .models import as_utc

    raw = state_get(session, key)
    if not raw:
        return None
    try:
        return as_utc(datetime.fromisoformat(raw))
    except ValueError:
        return None


def state_set_datetime(session: Session, key: str, value: datetime | None) -> None:
    state_set(session, key, value.isoformat() if value else None)
