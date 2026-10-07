"""Browser runtime for the GitHub Pages build (Pyodide inside a Web Worker).

The page's worker (web/worker.js) mounts a persistent IndexedDB file system,
loads this module and then calls:

* ``bootstrap(env)`` once;
* ``await handle(method, url, headers, body)`` for every request the page makes;
* ``run_task(name)`` from timers: the send queue, periodic jobs (harvest,
  replies, follow-ups) and queued background work.

Pyodide has no threads and no sockets, so FastAPI's thread pool runs inline,
background work is queued and run one step at a time between page requests,
log records are buffered and written after each call, and HTTP goes through
synchronous XMLHttpRequest (``fetch.BrowserTransport``). The worker runs every
call to completion before starting the next one.
"""

from __future__ import annotations

import asyncio
import os
from collections import deque
from collections.abc import Callable, Iterator
from datetime import timedelta
from typing import Any
from urllib.parse import unquote, urlsplit

from . import __version__

HOST = "referralpilot.local"

_app = None
_pending: deque[tuple[Callable[..., Any], tuple[Any, ...]]] = deque()
_current: Iterator[Any] | None = None


def _patch_threads() -> None:
    """Run FastAPI/Starlette "thread pool" work inline: Pyodide has no threads."""
    import anyio.to_thread

    async def run_sync(func, *args, abandon_on_cancel=False, cancellable=None, limiter=None):
        return func(*args)

    anyio.to_thread.run_sync = run_sync


def _queue_background(target: Callable[..., Any], *args: Any) -> None:
    _pending.append((target, args))


def bootstrap(env: dict[str, str]) -> dict[str, Any]:
    """Configure settings from the worker's environment, open the database and build the app."""
    global _app
    from . import websettings

    env = {str(k): str(v) for k, v in dict(env).items()}
    os.environ.update({k: v for k, v in env.items() if k.lower() not in websettings.FIELD_NAMES})
    os.environ.update({"WEB_MODE": "true", "SQLITE_WAL": "false", "SCHEDULER_ENABLED": "false"})
    websettings.set_defaults({k.lower(): v for k, v in env.items() if k.lower() in websettings.FIELD_NAMES})

    from .config import reset_settings

    reset_settings()
    _patch_threads()

    from .activity import flush_logging, get_logger, setup_logging
    from .config import get_settings
    from .db import init_db, session_scope
    from .profile import get_active_profile
    from .runtime import set_background_runner
    from .seed import seed_all

    setup_logging(get_settings().log_level, persist="buffer", console=False)
    init_db()
    with session_scope() as session:
        first_run = get_active_profile(session) is None
        if first_run:
            seed_all(session)
        stored = websettings.load(session)
    websettings.apply(stored)
    set_background_runner(_queue_background)

    from .ui.app import create_app

    _app = create_app(start_scheduler=False, init_logging=False)
    get_logger("app").info("Browser edition %s ready%s", __version__, " - welcome!" if first_run else "")
    flush_logging()
    return {"version": __version__, "first_run": first_run, **client_config()}


def client_config() -> dict[str, Any]:
    """What the page itself needs to know (Gmail sign-in happens in the page, not the worker)."""
    from .config import get_settings
    from .outreach.gmail_web import SCOPES, token_status

    settings = get_settings()
    return {
        "email_backend": settings.email_backend,
        "gmail_client_id": settings.gmail_client_id,
        "gmail_scopes": SCOPES,
        "gmail": token_status(),
    }


async def handle(method: str, url: str, headers: Any = (), body: Any = None) -> tuple[int, list[list[str]], bytes]:
    """Serve one request through the ASGI app. Returns (status, headers, body)."""
    from .activity import flush_logging

    if _app is None:
        raise RuntimeError("bootstrap() has not run")
    parts = urlsplit(str(url))
    raw_headers = [(str(k).lower().encode("latin-1"), str(v).encode("latin-1")) for k, v in headers]
    if not any(name == b"host" for name, _ in raw_headers):
        raw_headers.append((b"host", HOST.encode()))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": str(method).upper(),
        "scheme": "https",
        "path": unquote(parts.path) or "/",
        "raw_path": (parts.path or "/").encode(),
        "query_string": parts.query.encode(),
        "root_path": "",
        "headers": raw_headers,
        "client": ("127.0.0.1", 0),
        "server": (HOST, 443),
        "state": {},
    }
    request_body = bytes(body) if body is not None else b""
    response: dict[str, Any] = {"status": 500, "headers": [], "chunks": []}
    finished = asyncio.Event()
    delivered = False

    async def receive() -> dict[str, Any]:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": request_body, "more_body": False}
        await finished.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            response["status"] = message["status"]
            response["headers"] = [[k.decode("latin-1"), v.decode("latin-1")] for k, v in message.get("headers", [])]
        elif message["type"] == "http.response.body":
            response["chunks"].append(bytes(message.get("body", b"")))
            if not message.get("more_body"):
                finished.set()

    try:
        await _app(scope, receive, send)
    finally:
        finished.set()
        flush_logging()
    return response["status"], response["headers"], b"".join(response["chunks"])


# --- timers -----------------------------------------------------------------------------

def _due(key: str, interval: timedelta) -> bool:
    """True (and the run is recorded) when `interval` has passed since the last run."""
    from .db import session_scope, state_get_datetime, state_set_datetime
    from .models import utcnow

    now = utcnow()
    with session_scope() as session:
        last = state_get_datetime(session, key)
        if last is not None and now - last < interval:
            return False
        state_set_datetime(session, key, now)
    return True


def run_periodic() -> list[str]:
    """Run whichever periodic jobs are due. Their last-run times live in the database,
    so the schedule carries on across page reloads."""
    from . import scheduler
    from .config import get_settings
    from .pipeline import PipelineError, harvest_running, start_harvest

    settings = get_settings()
    ran: list[str] = []
    if not harvest_running() and _due("web:last_harvest", timedelta(hours=settings.harvest_interval_hours)):
        try:
            start_harvest()
            ran.append("harvest")
        except PipelineError:
            pass
    jobs = (
        ("replies", timedelta(minutes=settings.reply_check_interval_minutes), scheduler.job_replies),
        ("followups", timedelta(hours=1), scheduler.job_followups),
        ("prune", timedelta(hours=24), scheduler.job_prune),
    )
    for name, interval, job in jobs:
        if _due(f"web:last_{name}", interval):
            job()
            ran.append(name)
    return ran


def background_step() -> bool:
    """Run one unit of queued background work. Returns True while more is waiting."""
    global _current
    from .activity import get_logger

    try:
        if _current is None and _pending:
            target, args = _pending.popleft()
            result = target(*args)
            _current = result if isinstance(result, Iterator) else None
        elif _current is not None:
            try:
                next(_current)
            except StopIteration:
                _current = None
    except Exception as exc:  # a crashing job must not wedge the queue
        _current = None
        get_logger("app").exception("Background job failed: %s", exc)
    return _current is not None or bool(_pending)


def background_pending() -> bool:
    return _current is not None or bool(_pending)


def run_task(name: str) -> dict[str, Any]:
    """Entry point for the worker's timers: "send", "periodic" or "background"."""
    from .activity import flush_logging

    try:
        if name == "send":
            from .runtime import get_queue

            result = get_queue().tick()
            return {"status": result.status, "detail": result.detail, "more": background_pending()}
        if name == "periodic":
            return {"ran": run_periodic(), "more": background_pending()}
        if name == "background":
            return {"more": background_step()}
        raise ValueError(f"unknown task {name!r}")
    finally:
        flush_logging()


# --- Gmail (the page signs in, the worker sends) ------------------------------------------

def set_gmail_token(access_token: str | None, expires_in: float = 3600, email: str | None = None) -> dict[str, Any]:
    from .activity import flush_logging, get_logger
    from .outreach.gmail_web import set_token, token_status

    set_token(access_token, expires_in, email)
    status = token_status()
    if access_token:
        get_logger("outreach").info("Gmail connected%s - queued emails can go out while this tab is open",
                                    f" as {email}" if email else "")
    flush_logging()
    return status
