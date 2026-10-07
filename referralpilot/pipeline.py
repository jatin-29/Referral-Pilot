"""High-level pipeline steps shared by the CLI, the dashboard and the scheduler.

Each function opens its own transaction(s), so callers never juggle sessions.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import nullcontext

import httpx

from .activity import get_logger
from .config import get_settings
from .db import session_scope
from .fetch import build_client
from .harvester import HarvestStats, harvest
from .harvester.service import Fallback, harvest_iter
from .models import Job, ReferralContact
from .outreach.service import create_draft
from .prospector import ProspectResult, prospect_job
from .runtime import run_in_background
from .tailor import TailorOutcome, analyze_job, tailor_job

log = get_logger("app")

_harvest_lock = threading.Lock()


class PipelineError(RuntimeError):
    pass


def _snapshot_fallback(client: httpx.Client) -> Fallback | None:
    """Browser build: boards the browser may not call (CORS) fall back to the scheduled crawl."""
    settings = get_settings()
    if not (settings.web_mode and settings.site_url):
        return None
    from .harvester.snapshot import LazySnapshot

    return LazySnapshot(client, settings.site_url.rstrip("/") + "/jobs.json")


def run_harvest(company_ids: list[int] | None = None, *, client: httpx.Client | None = None) -> list[HarvestStats]:
    """Harvest, then score (and optionally tailor) every newly discovered job."""
    if not _harvest_lock.acquire(blocking=False):
        raise PipelineError("A harvest is already running")
    try:
        with build_client() if client is None else nullcontext(client) as http:
            stats = harvest(company_ids, client=http, fallback=_snapshot_fallback(http))
    finally:
        _harvest_lock.release()
    process_new_jobs([job_id for result in stats for job_id in result.new_job_ids])
    return stats


def start_harvest(company_ids: list[int] | None = None) -> None:
    """Run a harvest in the background (the dashboard's "Harvest now")."""
    if not _harvest_lock.acquire(blocking=False):
        raise PipelineError("A harvest is already running")
    try:
        run_in_background(_harvest_job, company_ids)
    except BaseException:
        _harvest_lock.release()
        raise


def _harvest_job(company_ids: list[int] | None) -> Iterator[HarvestStats]:
    """Background harvest. Yields after each company so the browser build can serve page
    requests in between; the lock taken by start_harvest() is released at the end."""
    new_ids: list[int] = []
    try:
        with build_client() as client:
            for result in harvest_iter(company_ids, client=client, fallback=_snapshot_fallback(client)):
                new_ids += result.new_job_ids
                yield result
    except Exception as exc:
        log.exception("Harvest failed: %s", exc)
    finally:
        _harvest_lock.release()
    process_new_jobs(new_ids)


def harvest_running() -> bool:
    return _harvest_lock.locked()


def process_new_jobs(job_ids: list[int]) -> None:
    settings = get_settings()
    for job_id in job_ids:
        try:
            with session_scope() as session:
                job = session.get(Job, job_id)
                if job is None:
                    continue
                if settings.auto_tailor_new_jobs:
                    tailor_job(session, job)
                else:
                    analyze_job(session, job)
        except Exception as exc:  # one bad posting must not stop the batch
            log.error("Post-processing job %s failed: %s", job_id, exc, extra={"job_id": job_id})


def tailor(job_id: int, *, engine: str | None = None) -> TailorOutcome:
    with session_scope() as session:
        job = session.get(Job, job_id)
        if job is None:
            raise PipelineError(f"Job {job_id} not found")
        return tailor_job(session, job, engine=engine)


def prospect(job_id: int, *, client: httpx.Client | None = None, providers=None) -> ProspectResult:
    with session_scope() as session:
        job = session.get(Job, job_id)
        if job is None:
            raise PipelineError(f"Job {job_id} not found")
        if job.match_score is None:
            analyze_job(session, job)
        return prospect_job(session, job, client=client, providers=providers)


def draft_for_contact(contact_id: int, *, regenerate: bool = False) -> int:
    with session_scope() as session:
        contact = session.get(ReferralContact, contact_id)
        if contact is None:
            raise PipelineError(f"Contact {contact_id} not found")
        job = session.get(Job, contact.job_id)
        if job is not None and job.match_score is None:
            analyze_job(session, job)
        return create_draft(session, contact, regenerate=regenerate).id
