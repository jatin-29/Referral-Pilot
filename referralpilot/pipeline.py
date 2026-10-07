"""High-level pipeline steps shared by the CLI, the dashboard and the scheduler.

Each function opens its own transaction(s), so callers never juggle sessions.
"""

from __future__ import annotations

import threading

import httpx

from .activity import get_logger
from .config import get_settings
from .db import session_scope
from .harvester import HarvestStats, harvest
from .models import Job, ReferralContact
from .outreach.service import create_draft
from .prospector import ProspectResult, prospect_job
from .tailor import TailorOutcome, analyze_job, tailor_job

log = get_logger("app")

_harvest_lock = threading.Lock()


class PipelineError(RuntimeError):
    pass


def run_harvest(company_ids: list[int] | None = None, *, client: httpx.Client | None = None) -> list[HarvestStats]:
    """Harvest, then score (and optionally tailor) every newly discovered job."""
    if not _harvest_lock.acquire(blocking=False):
        raise PipelineError("A harvest is already running")
    try:
        stats = harvest(company_ids, client=client)
    finally:
        _harvest_lock.release()
    process_new_jobs([job_id for result in stats for job_id in result.new_job_ids])
    return stats


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
