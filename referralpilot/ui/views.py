"""Query helpers that assemble what each dashboard page/partial needs."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import func, or_
from sqlmodel import Session, col, select

from ..models import (
    BOARD_COLUMNS,
    Company,
    Job,
    JobStatus,
    OutreachLog,
    OutreachStatus,
    OutreachType,
    ReferralContact,
    utcnow,
)
from ..outreach.queue import OutreachQueue, QueueStatus
from ..profile import get_active_profile
from ..prospector.providers import ProspectContext, search_links
from ..prospector.service import institutions_for

AVERAGE_GAP = timedelta(minutes=5)


def _counts_by_job(session: Session) -> tuple[dict[int, int], dict[int, dict[str, int]]]:
    contacts = dict(session.exec(
        select(ReferralContact.job_id, func.count()).group_by(ReferralContact.job_id)
    ).all())
    outreach: dict[int, dict[str, int]] = defaultdict(dict)
    for job_id, status, count in session.exec(
        select(OutreachLog.job_id, OutreachLog.status, func.count()).group_by(OutreachLog.job_id, OutreachLog.status)
    ).all():
        outreach[job_id][status] = count
    return contacts, outreach


def board_columns(
    session: Session,
    *,
    q: str | None = None,
    company: str | None = None,
    min_score: float | None = None,
    show_archived: bool = False,
    limit_per_column: int = 80,
) -> list[dict]:
    stmt = select(Job)
    if not show_archived:
        stmt = stmt.where(Job.status != JobStatus.ARCHIVED)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(col(Job.title).ilike(like), col(Job.company).ilike(like), col(Job.location).ilike(like)))
    if company:
        stmt = stmt.where(Job.company == company)
    if min_score is not None:
        stmt = stmt.where(Job.match_score >= min_score)
    jobs = session.exec(stmt).all()
    contacts, outreach = _counts_by_job(session)

    columns = [{"key": key, "label": label, "statuses": statuses, "cards": []} for key, label, statuses in BOARD_COLUMNS]
    if show_archived:
        columns.append({"key": "archived", "label": "Archived", "statuses": (JobStatus.ARCHIVED,), "cards": []})
    by_status = {status: column for column in columns for status in column["statuses"]}
    for job in jobs:
        column = by_status.get(job.status)
        if column is None:
            continue
        counts = outreach.get(job.id, {})
        column["cards"].append({
            "job": job,
            "contacts": contacts.get(job.id, 0),
            "drafts": counts.get(OutreachStatus.DRAFT, 0),
            "queued": counts.get(OutreachStatus.QUEUED, 0) + counts.get(OutreachStatus.SENDING, 0),
            "sent": counts.get(OutreachStatus.SENT, 0),
        })
    epoch = datetime.min.replace(tzinfo=utcnow().tzinfo)
    for column in columns:
        column["cards"].sort(key=lambda c: (-(c["job"].match_score or -1), -(c["job"].discovered_at or epoch).timestamp()))
        column["total"] = len(column["cards"])
        column["cards"] = column["cards"][:limit_per_column]
    return columns


def company_names(session: Session) -> list[str]:
    return list(session.exec(select(Job.company).distinct().order_by(Job.company)).all())


def job_detail(session: Session, job: Job) -> dict:
    contacts = session.exec(
        select(ReferralContact).where(ReferralContact.job_id == job.id)
        .order_by(col(ReferralContact.priority_score).desc(), col(ReferralContact.id))
    ).all()
    emails = session.exec(
        select(OutreachLog).where(OutreachLog.job_id == job.id).order_by(col(OutreachLog.created_at).desc())
    ).all()
    latest_initial: dict[int, OutreachLog] = {}
    for item in emails:
        if item.type == OutreachType.INITIAL and item.status != OutreachStatus.CANCELLED:
            latest_initial.setdefault(item.contact_id, item)
    company = session.get(Company, job.company_id) if job.company_id else None
    profile = get_active_profile(session)
    ctx = ProspectContext(company=job.company, domain=job.domain, role_title=job.title,
                          institutions=institutions_for(profile))
    contact_names = {c.id: c.name for c in contacts}
    stale = bool(company and company.last_harvested_at and job.last_seen_at
                 and job.last_seen_at < company.last_harvested_at - timedelta(minutes=5))
    return {
        "job": job,
        "company": company,
        "contacts": contacts,
        "contact_names": contact_names,
        "emails": emails,
        "latest_initial": latest_initial,
        "links": search_links(ctx),
        "has_pdf": bool(job.resume_pdf_path and Path(job.resume_pdf_path).exists()),
        "has_tex": bool(job.resume_tex_path and Path(job.resume_tex_path).exists()),
        "tex_source": _read_text(job.resume_tex_path),
        "breakdown": (job.match_details or {}).get("breakdown", {}),
        "ranked_projects": (job.match_details or {}).get("projects", [])[:3],
        "stale": stale,
    }


def _read_text(path: str | None, limit: int = 200_000) -> str | None:
    """Small text files shown inline (the LaTeX source posted to Overleaf)."""
    if not path or not Path(path).exists():
        return None
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    return text if len(text) <= limit else None


def browser_timers(session: Session) -> list[dict]:
    """Next runs of the browser build's timers (they only tick while the tab is open)."""
    from ..config import get_settings
    from ..db import state_get_datetime

    settings = get_settings()
    timers = [
        ("harvest", "Crawl job boards", timedelta(hours=settings.harvest_interval_hours)),
        ("replies", "Check replies", timedelta(minutes=settings.reply_check_interval_minutes)),
        ("followups", "Queue follow-ups", timedelta(hours=1)),
        ("prune", "Prune activity log", timedelta(hours=24)),
    ]
    rows = [{"id": "send_tick", "name": "Send next queued email", "next_run": utcnow() + timedelta(seconds=30)}]
    for key, name, interval in timers:
        last = state_get_datetime(session, f"web:last_{key}")
        rows.append({"id": key, "name": name, "next_run": (last + interval) if last else utcnow()})
    return rows


def queue_etas(status: QueueStatus, count: int, now: datetime) -> list[datetime | None]:
    """Rough send-time estimates for queued items (None = after the 24h cap frees up)."""
    start = max(now, status.next_send_at or now)
    if status.next_window_at:
        start = max(start, status.next_window_at)
    remaining = max(0, status.limit - status.sent_24h)
    return [start + AVERAGE_GAP * i if i < remaining else None for i in range(count)]


def outbox(session: Session, queue: OutreachQueue) -> dict:
    status = queue.status(session)
    now = utcnow()

    def rows(*statuses: str, order=None, limit: int = 100):
        stmt = select(OutreachLog).where(col(OutreachLog.status).in_(list(statuses)))
        stmt = stmt.order_by(order if order is not None else col(OutreachLog.updated_at).desc()).limit(limit)
        items = session.exec(stmt).all()
        result = []
        for item in items:
            result.append({
                "item": item,
                "contact": session.get(ReferralContact, item.contact_id),
                "job": session.get(Job, item.job_id),
            })
        return result

    queued = rows(OutreachStatus.QUEUED, OutreachStatus.SENDING,
                  order=col(OutreachLog.queued_at).asc(), limit=200)
    for row, eta in zip(queued, queue_etas(status, len(queued), now), strict=True):
        row["eta"] = eta
    return {
        "status": status,
        "queued": queued,
        "drafts": rows(OutreachStatus.DRAFT),
        "sent": rows(OutreachStatus.SENT, order=col(OutreachLog.sent_at).desc(), limit=50),
        "problems": rows(OutreachStatus.FAILED, OutreachStatus.SKIPPED, limit=50),
        "now": now,
    }


def pipeline_counts(session: Session) -> dict[str, int]:
    return dict(session.exec(select(Job.status, func.count()).group_by(Job.status)).all())
