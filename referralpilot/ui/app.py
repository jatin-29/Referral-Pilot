"""FastAPI application factory for the local dashboard."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.responses import PlainTextResponse

from ..activity import get_logger, setup_logging
from ..config import get_settings
from ..db import init_db, session_scope
from ..models import as_utc, utcnow
from ..profile import get_active_profile
from ..seed import seed_all

UI_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(UI_DIR / "templates"))
log = get_logger("ui")

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class SameOriginGuard:
    """Blocks cross-site state changes: unsafe requests must come from our own htmx code.

    Browsers cannot attach the custom `HX-Request` header to cross-origin
    requests without a CORS preflight (which this app never grants), so a
    malicious page cannot make the dashboard queue or send email.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["method"] in UNSAFE_METHODS:
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
            origin = headers.get("origin")
            same_origin = not origin or urlparse(origin).netloc == headers.get("host", "")
            if headers.get("hx-request") != "true" or not same_origin:
                await PlainTextResponse("Cross-site request blocked", status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)


# --- template filters ------------------------------------------------------------

def _local(value: datetime | None) -> datetime | None:
    return as_utc(value).astimezone(get_settings().tz) if value else None


def fmt_dt(value: datetime | None, fmt: str = "%d %b %H:%M") -> str:
    local = _local(value)
    return local.strftime(fmt) if local else "-"


def fmt_ago(value: datetime | None) -> str:
    if not value:
        return "-"
    seconds = (utcnow() - as_utc(value)).total_seconds()
    future = seconds < 0
    seconds = abs(seconds)
    if seconds < 45:
        text = "now" if not future else "in <1m"
        return text
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            amount = int(seconds // size)
            return f"in {amount}{unit}" if future else f"{amount}{unit} ago"
    return "now"


def score_class(score: float | None) -> str:
    if score is None:
        return "bg-slate-100 text-slate-500 ring-slate-200"
    if score >= 80:
        return "bg-emerald-50 text-emerald-700 ring-emerald-200"
    if score >= 65:
        return "bg-amber-50 text-amber-700 ring-amber-200"
    return "bg-rose-50 text-rose-700 ring-rose-200"


STATUS_CLASSES = {
    "new": "bg-slate-100 text-slate-600",
    "drafted": "bg-sky-50 text-sky-700",
    "draft": "bg-sky-50 text-sky-700",
    "queued": "bg-indigo-50 text-indigo-700",
    "sending": "bg-indigo-100 text-indigo-800",
    "contacted": "bg-violet-50 text-violet-700",
    "sent": "bg-violet-50 text-violet-700",
    "followup_sent": "bg-fuchsia-50 text-fuchsia-700",
    "replied": "bg-emerald-50 text-emerald-700",
    "referred": "bg-emerald-600 text-white",
    "opted_out": "bg-rose-50 text-rose-700",
    "bounced": "bg-rose-50 text-rose-700",
    "failed": "bg-rose-50 text-rose-700",
    "skipped": "bg-amber-50 text-amber-700",
    "cancelled": "bg-slate-100 text-slate-500",
}


def status_class(status: str | None) -> str:
    return STATUS_CLASSES.get(status or "", "bg-slate-100 text-slate-600")


def label(value: str | None) -> str:
    return (value or "").replace("_", " ").capitalize()


templates.env.filters.update(dt=fmt_dt, ago=fmt_ago, score_class=score_class, status_class=status_class,
                             label=label, basename=lambda p: Path(p).name if p else "")
templates.env.globals.update(settings=get_settings, tojson=json.dumps)


def create_app(*, start_scheduler: bool | None = None, init_logging: bool = True) -> FastAPI:
    settings = get_settings()
    run_scheduler = settings.scheduler_enabled if start_scheduler is None else start_scheduler

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if init_logging:
            setup_logging(settings.log_level)
        init_db()
        with session_scope() as session:
            if get_active_profile(session) is None:
                seed_all(session)
        scheduler = None
        if run_scheduler:
            from ..scheduler import build_scheduler

            scheduler = build_scheduler()
            scheduler.start()
            log.info("Dashboard up - scheduler running (%s mode)", settings.email_backend)
        app.state.scheduler = scheduler
        try:
            yield
        finally:
            if scheduler is not None:
                scheduler.shutdown(wait=False)

    app = FastAPI(title="ReferralPilot", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.scheduler = None
    app.add_middleware(SameOriginGuard)
    app.mount("/static", StaticFiles(directory=str(UI_DIR / "static")), name="static")

    from .routes import router

    app.include_router(router)
    return app
