"""Job snapshots (jobs.json): public postings from a scheduled crawl.

The GitHub Pages build cannot reach every job board from the browser (boards
that send no CORS headers are blocked), so a scheduled GitHub Actions run
crawls them with `referralpilot export-jobs` and publishes the result next to
the site. When a live fetch fails, the browser falls back to these postings.
A snapshot only ever holds public job-board data: no profile, contacts or email.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx
from sqlmodel import Session, col, select

from ..activity import get_logger
from ..fetch import FetchError, get_json
from ..models import Company, Job, JobStatus, as_utc, utcnow
from .base import RawJob, parse_timestamp
from .service import HarvestStats

log = get_logger("harvester")

SNAPSHOT_VERSION = 1
_RAW_FIELDS = ("company", "external_id", "title", "url", "ats_type", "location", "department",
               "employment_type", "description", "company_domain")


def job_to_dict(job: Job) -> dict[str, Any]:
    data: dict[str, Any] = {name: getattr(job, name, None) for name in _RAW_FIELDS if name != "company_domain"}
    data["company_domain"] = job.domain
    data["posted_at"] = as_utc(job.posted_at).isoformat() if job.posted_at else None
    return data


def raw_from_dict(data: dict[str, Any]) -> RawJob | None:
    try:
        raw = RawJob(**{name: data.get(name) for name in _RAW_FIELDS if data.get(name) is not None})
    except TypeError:  # a required field is missing
        return None
    raw.description = raw.description or ""
    raw.posted_at = parse_timestamp(data.get("posted_at"))
    return raw if raw.external_id and raw.title and raw.company else None


def export_snapshot(session: Session, results: list[HarvestStats] | None = None) -> dict[str, Any]:
    """Every non-archived posting in the database, plus which boards were crawled successfully."""
    jobs = session.exec(select(Job).where(Job.status != JobStatus.ARCHIVED)
                        .order_by(col(Job.company), col(Job.id))).all()
    failed = {r.company for r in results or [] if r.error}
    companies = []
    for company in session.exec(select(Company).order_by(col(Company.name))).all():
        crawled = company.last_harvested_at is not None and company.name not in failed
        companies.append({
            "name": company.name, "ats_type": company.ats_type, "board_token": company.board_token,
            "domain": company.domain, "ok": crawled, "status": company.last_harvest_status,
        })
    return {
        "version": SNAPSHOT_VERSION,
        "generated_at": utcnow().isoformat(),
        "companies": companies,
        "jobs": [job_to_dict(job) for job in jobs],
    }


class Snapshot:
    def __init__(self, data: dict[str, Any]):
        self.generated_at: datetime | None = parse_timestamp(data.get("generated_at"))
        self._ok = {str(c.get("name", "")).lower() for c in data.get("companies", []) if c.get("ok")}
        self._jobs: dict[str, list[RawJob]] = {}
        for item in data.get("jobs", []):
            raw = raw_from_dict(item) if isinstance(item, dict) else None
            if raw is not None:
                self._jobs.setdefault(raw.company.lower(), []).append(raw)

    @property
    def label(self) -> str:
        if not self.generated_at:
            return "the scheduled crawl"
        hours = max(0, int((utcnow() - self.generated_at).total_seconds() // 3600))
        return f"the scheduled crawl from {hours}h ago" if hours else "the scheduled crawl from <1h ago"

    def jobs_for(self, company: Company) -> tuple[list[RawJob], str] | None:
        key = company.name.lower()
        if key not in self._ok:
            return None
        return [RawJob(**vars(raw)) for raw in self._jobs.get(key, [])], self.label


class LazySnapshot:
    """Downloads jobs.json the first time a board has to fall back to it (once per harvest)."""

    def __init__(self, client: httpx.Client, url: str):
        self.client = client
        self.url = url
        self._snapshot: Snapshot | None = None
        self._loaded = False

    def load(self) -> Snapshot | None:
        if not self._loaded:
            self._loaded = True
            try:
                data = get_json(self.client, self.url, retries=0)
            except (FetchError, httpx.HTTPError) as exc:
                log.warning("Scheduled-crawl snapshot unavailable (%s): %s", self.url, exc)
                return None
            if isinstance(data, dict) and data.get("version") == SNAPSHOT_VERSION:
                self._snapshot = Snapshot(data)
            else:
                log.warning("Ignoring %s: unknown snapshot format", self.url)
        return self._snapshot

    def __call__(self, company: Company) -> tuple[list[RawJob], str] | None:
        snapshot = self.load()
        return snapshot.jobs_for(company) if snapshot else None
