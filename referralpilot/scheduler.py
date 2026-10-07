"""Background jobs (APScheduler): crawling, the email drip-feed, replies and follow-ups."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import func
from sqlmodel import select

from .activity import get_logger, prune_activity
from .config import get_settings
from .db import session_scope
from .models import Company, utcnow
from .outreach.followups import scan_followups, scan_replies
from .pipeline import PipelineError, run_harvest
from .runtime import get_queue, new_reply_checker

if TYPE_CHECKING:  # APScheduler is not installed in the browser build
    from apscheduler.schedulers.background import BackgroundScheduler

log = get_logger("scheduler")

SEND_TICK_SECONDS = 30


def job_harvest() -> None:
    log.info("Scheduled harvest starting")
    try:
        run_harvest()
    except PipelineError as exc:
        log.info("Skipped scheduled harvest: %s", exc)
    except Exception as exc:
        log.exception("Harvest crashed: %s", exc)


def job_send_tick() -> None:
    try:
        get_queue().tick()
    except Exception as exc:
        log.exception("Send tick failed: %s", exc)


def job_followups() -> None:
    checker = new_reply_checker()
    try:
        scan_followups(checker)
    except Exception as exc:
        log.exception("Follow-up scan failed: %s", exc)
    finally:
        checker.close()


def job_replies() -> None:
    checker = new_reply_checker()
    try:
        scan_replies(checker)
    except Exception as exc:
        log.exception("Reply scan failed: %s", exc)
    finally:
        checker.close()


def job_prune() -> None:
    with session_scope() as session:
        removed = prune_activity(session, keep_days=30)
    if removed:
        log.info("Pruned %d old activity log rows", removed)


def _first_harvest_at(interval: timedelta) -> datetime:
    """Don't re-crawl on every restart: wait for the interval since the last harvest."""
    with session_scope() as session:
        last = session.exec(select(func.max(Company.last_harvested_at))).one()
    now = utcnow()
    soon = now + timedelta(seconds=45)
    if last is None:
        return soon
    from .models import as_utc

    return max(soon, as_utc(last) + interval)


def build_scheduler() -> BackgroundScheduler:
    from apscheduler.schedulers.background import BackgroundScheduler

    settings = get_settings()
    scheduler = BackgroundScheduler(
        timezone=settings.tz,
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 600},
    )
    now = utcnow()
    interval = timedelta(hours=settings.harvest_interval_hours)
    scheduler.add_job(job_harvest, "interval", seconds=interval.total_seconds(), id="harvest",
                      name="Crawl job boards", next_run_time=_first_harvest_at(interval))
    scheduler.add_job(job_send_tick, "interval", seconds=SEND_TICK_SECONDS, id="send_tick",
                      name="Send next queued email", next_run_time=now + timedelta(seconds=5))
    scheduler.add_job(job_replies, "interval", minutes=settings.reply_check_interval_minutes, id="replies",
                      name="Check replies", next_run_time=now + timedelta(minutes=2))
    scheduler.add_job(job_followups, "interval", hours=1, id="followups",
                      name="Queue follow-ups", next_run_time=now + timedelta(minutes=3))
    scheduler.add_job(job_prune, "interval", hours=24, id="prune", name="Prune activity log")
    return scheduler


def describe_jobs(scheduler: BackgroundScheduler | None) -> list[dict]:
    if scheduler is None:
        return []
    # Jobs of a scheduler that has not started yet have no next_run_time attribute.
    return [{"id": job.id, "name": job.name, "next_run": getattr(job, "next_run_time", None)}
            for job in scheduler.get_jobs()]
