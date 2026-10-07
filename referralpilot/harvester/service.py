"""Harvest orchestration: fetch -> filter -> dedupe/upsert into SQLite."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

import httpx
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select

from ..activity import get_logger
from ..config import get_settings
from ..db import session_scope
from ..fetch import FetchError, build_client
from ..models import Company, Job, JobStatus, utcnow
from .ashby import AshbyHarvester
from .base import Harvester, HarvestTarget, RawJob
from .filters import JobFilter
from .greenhouse import GreenhouseHarvester
from .lever import LeverHarvester
from .yc import YCJobsHarvester

log = get_logger("harvester")

HARVESTERS: dict[str, type[Harvester]] = {
    "greenhouse": GreenhouseHarvester,
    "lever": LeverHarvester,
    "ashby": AshbyHarvester,
    "yc": YCJobsHarvester,
}

_UPDATABLE_FIELDS = ("title", "url", "location", "department", "employment_type", "description", "posted_at")


@dataclass
class HarvestStats:
    company: str
    ats_type: str
    fetched: int = 0
    accepted: int = 0
    new: int = 0
    updated: int = 0
    rejected: Counter = field(default_factory=Counter)
    new_job_ids: list[int] = field(default_factory=list)
    error: str | None = None

    def summary(self) -> str:
        if self.error:
            return f"error: {self.error}"
        return f"{self.fetched} fetched, {self.accepted} match, {self.new} new, {self.updated} updated"


def target_for(company: Company) -> HarvestTarget:
    return HarvestTarget(
        name=company.name,
        ats_type=company.ats_type,
        board_token=company.board_token,
        domain=company.domain,
        region=company.region,
        company_id=company.id,
    )


def upsert_job(session: Session, raw: RawJob, company_id: int | None, min_years: int | None) -> tuple[Job, bool, bool]:
    """Insert or refresh a posting. Returns (job, created, changed)."""
    now = utcnow()
    existing = session.exec(
        select(Job).where(Job.company == raw.company, Job.external_id == raw.external_id)
    ).first()
    if existing is not None:
        changed = False
        for name in _UPDATABLE_FIELDS:
            value = getattr(raw, name)
            if value and getattr(existing, name) != value:
                setattr(existing, name, value)
                changed = True
        existing.last_seen_at = now
        existing.min_years_required = min_years
        if changed:
            existing.updated_at = now
        session.add(existing)
        return existing, False, changed

    job = Job(
        company=raw.company,
        company_id=company_id,
        external_id=raw.external_id,
        title=raw.title,
        url=raw.url,
        ats_type=raw.ats_type,
        location=raw.location,
        department=raw.department,
        employment_type=raw.employment_type,
        description=raw.description,
        posted_at=raw.posted_at,
        domain=raw.company_domain,
        min_years_required=min_years,
        status=JobStatus.DISCOVERED,
        last_seen_at=now,
    )
    try:
        with session.begin_nested():  # a concurrent harvest may have inserted it first
            session.add(job)
    except IntegrityError:
        existing = session.exec(
            select(Job).where(Job.company == raw.company, Job.external_id == raw.external_id)
        ).one()
        return existing, False, False
    return job, True, True


def harvest_company(
    session: Session,
    company: Company,
    *,
    client: httpx.Client,
    job_filter: JobFilter,
) -> HarvestStats:
    stats = HarvestStats(company=company.name, ats_type=company.ats_type)
    harvester_cls = HARVESTERS.get(company.ats_type)
    if harvester_cls is None:
        stats.error = f"unsupported ATS {company.ats_type!r}"
        return stats

    try:
        raw_jobs = harvester_cls(client).fetch(target_for(company), title_prefilter=job_filter.title_ok)
    except (FetchError, httpx.HTTPError, ValueError, KeyError) as exc:
        stats.error = str(exc)
        company.last_harvested_at = utcnow()
        company.last_harvest_status = f"error: {exc}"[:250]
        session.add(company)
        log.warning("%s (%s/%s): harvest failed - %s", company.name, company.ats_type, company.board_token, exc)
        return stats

    stats.fetched = len(raw_jobs)
    for raw in raw_jobs:
        decision = job_filter.evaluate(raw)
        if not decision.accepted:
            stats.rejected[decision.reason] += 1
            continue
        stats.accepted += 1
        job, created, changed = upsert_job(session, raw, company.id, decision.min_years)
        if created:
            session.flush()
            stats.new += 1
            stats.new_job_ids.append(job.id)
            log.info("New role: %s - %s (%s)", job.company, job.title, job.location or "location n/a",
                     extra={"job_id": job.id})
        elif changed:
            stats.updated += 1

    company.last_harvested_at = utcnow()
    company.last_harvest_status = stats.summary()
    session.add(company)
    log.info("%s (%s): %s", company.name, company.ats_type, stats.summary())
    return stats


def harvest(
    company_ids: list[int] | None = None,
    *,
    client: httpx.Client | None = None,
    job_filter: JobFilter | None = None,
) -> list[HarvestStats]:
    """Crawl every enabled company (or the given ids), one transaction per company."""
    job_filter = job_filter or JobFilter.from_settings()
    own_client = client is None
    client = client or build_client(get_settings())
    results: list[HarvestStats] = []
    try:
        with session_scope() as session:
            stmt = select(Company).order_by(col(Company.id))
            if company_ids:
                stmt = stmt.where(col(Company.id).in_(company_ids))
            else:
                stmt = stmt.where(Company.enabled == True)  # noqa: E712
            company_list = list(session.exec(stmt))
        if not company_list:
            log.info("No enabled companies to harvest - add some on the Companies page")
        for company in company_list:
            with session_scope() as session:
                company = session.merge(company)
                results.append(harvest_company(session, company, client=client, job_filter=job_filter))
    finally:
        if own_client:
            client.close()
    total_new = sum(r.new for r in results)
    log.info("Harvest finished: %d companies, %d new matching roles", len(results), total_new)
    return results
