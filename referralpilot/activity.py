"""Activity log: `logging` records mirrored into the ActivityLog table.

Records are pushed through a queue and written by a background thread, so a
log call made while the caller holds an open SQLite write transaction can
never deadlock against its own lock. The dashboard tails the table over SSE.
"""

from __future__ import annotations

import atexit
import logging
import queue
import sys
from datetime import timedelta
from logging.handlers import QueueHandler, QueueListener

from sqlmodel import Session, col, delete, select

ROOT_LOGGER = "referralpilot"
_CONSOLE_FORMAT = "%(asctime)s %(levelname)-7s [%(source)s] %(message)s"

_listener: QueueListener | None = None
_queue: queue.Queue | None = None
_installed: list[logging.Handler] = []


def get_logger(source: str) -> logging.Logger:
    """Logger whose records show up in the dashboard under `source`."""
    return logging.getLogger(f"{ROOT_LOGGER}.{source}")


class _SourceFilter(logging.Filter):
    """Default `record.source` to the sub-package name (referralpilot.<source>.x)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "source", None):
            parts = record.name.split(".")
            record.source = parts[1] if len(parts) > 1 and parts[0] == ROOT_LOGGER else parts[0]
        if not hasattr(record, "job_id"):
            record.job_id = None
        return True


class _ShortFormatter(logging.Formatter):
    """Message plus a one-line exception summary instead of a full traceback."""

    def formatException(self, ei) -> str:  # noqa: N802 (logging API)
        exc = ei[1]
        return f"{type(exc).__name__}: {exc}" if exc else ""

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if record.exc_info and record.exc_info[1] is not None:
            message = f"{message} ({self.formatException(record.exc_info)})"
        return message


class _DBWriter(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        from .db import get_engine
        from .models import ActivityLog

        try:
            with Session(get_engine()) as session:
                session.add(
                    ActivityLog(
                        level=record.levelname,
                        source=getattr(record, "source", "app") or "app",
                        message=record.getMessage()[:4000],
                        job_id=getattr(record, "job_id", None),
                    )
                )
                session.commit()
        except Exception as exc:  # never recurse into logging from a log handler
            sys.stderr.write(f"[referralpilot] could not persist activity log: {exc}\n")


def setup_logging(level: str = "INFO", *, persist: bool = True, console: bool = True) -> None:
    """Configure the `referralpilot` logger tree. Safe to call more than once."""
    global _listener, _queue

    logger = logging.getLogger(ROOT_LOGGER)
    logger.setLevel(level.upper())
    logger.propagate = False
    shutdown_logging()

    if console:
        handler = logging.StreamHandler(sys.stderr)
        handler.addFilter(_SourceFilter())
        handler.setFormatter(logging.Formatter(_CONSOLE_FORMAT, datefmt="%H:%M:%S"))
        logger.addHandler(handler)
        _installed.append(handler)

    if persist:
        _queue = queue.Queue(-1)
        queue_handler = QueueHandler(_queue)
        queue_handler.addFilter(_SourceFilter())
        queue_handler.setFormatter(_ShortFormatter())
        logger.addHandler(queue_handler)
        _installed.append(queue_handler)
        _listener = QueueListener(_queue, _DBWriter())
        _listener.start()


def flush_logging() -> None:
    """Block until every queued record has been written to the database."""
    if _queue is not None and _listener is not None:
        _queue.join()


def shutdown_logging() -> None:
    global _listener, _queue
    logger = logging.getLogger(ROOT_LOGGER)
    if _listener is not None:
        try:
            _listener.stop()  # drains the queue first
        except Exception:
            pass
    _listener = None
    _queue = None
    for handler in _installed:
        logger.removeHandler(handler)
    _installed.clear()


atexit.register(shutdown_logging)


# --- queries used by the dashboard ----------------------------------------------

def recent_activity(session: Session, *, after_id: int = 0, limit: int = 200, source: str | None = None):
    from .models import ActivityLog

    stmt = select(ActivityLog).where(ActivityLog.id > after_id)
    if source:
        stmt = stmt.where(ActivityLog.source == source)
    if after_id:
        stmt = stmt.order_by(col(ActivityLog.id).asc()).limit(limit)
        return list(session.exec(stmt))
    rows = list(session.exec(stmt.order_by(col(ActivityLog.id).desc()).limit(limit)))
    return list(reversed(rows))


def prune_activity(session: Session, keep_days: int = 30) -> int:
    from .models import ActivityLog, utcnow

    cutoff = utcnow() - timedelta(days=keep_days)
    result = session.exec(delete(ActivityLog).where(ActivityLog.created_at < cutoff))
    return result.rowcount or 0
