"""Draft lifecycle and manual status changes: draft -> (edit) -> approve/queue -> sent."""

from __future__ import annotations

from sqlmodel import Session, col, select

from ..activity import get_logger
from ..config import get_settings
from ..models import (
    CONTACT_DO_NOT_EMAIL,
    ContactStatus,
    Job,
    JobStatus,
    OutreachLog,
    OutreachStatus,
    OutreachType,
    ReferralContact,
    Suppression,
    advance_job_status,
    utcnow,
)
from ..profile import get_active_profile
from ..prospector.patterns import is_valid_email
from .composer import compose_initial

log = get_logger("outreach")

PENDING = (OutreachStatus.DRAFT, OutreachStatus.QUEUED)


class OutreachError(RuntimeError):
    pass


def create_draft(session: Session, contact: ReferralContact, *, regenerate: bool = False) -> OutreachLog:
    """Compose (or return the existing) initial draft for a contact."""
    if contact.status in CONTACT_DO_NOT_EMAIL:
        raise OutreachError(f"{contact.name} is {contact.status}; they will not be emailed")
    if not is_valid_email(contact.email):
        raise OutreachError(f"{contact.name} has no valid email address yet")
    job = session.get(Job, contact.job_id)
    profile = get_active_profile(session)
    if job is None or profile is None:
        raise OutreachError("Job or candidate profile missing")

    existing = session.exec(
        select(OutreachLog).where(
            OutreachLog.contact_id == contact.id,
            OutreachLog.type == OutreachType.INITIAL,
            col(OutreachLog.status).in_([OutreachStatus.DRAFT, OutreachStatus.QUEUED, OutreachStatus.SENT]),
        )
    ).first()
    if existing and not (regenerate and existing.status == OutreachStatus.DRAFT):
        return existing

    draft = compose_initial(profile, job, contact, attach_resume=get_settings().attach_resume)
    item = existing or OutreachLog(contact_id=contact.id, job_id=job.id, to_email=contact.email,
                                   subject=draft.subject, body=draft.body)
    item.subject, item.body = draft.subject, draft.body
    item.to_email = contact.email
    item.attachment_path = draft.attachment_path
    item.updated_at = utcnow()
    session.add(item)
    if contact.status == ContactStatus.NEW:
        contact.status = ContactStatus.DRAFTED
        session.add(contact)
    session.flush()
    log.info("Drafted referral email to %s (%s) for %s - %s", contact.name, contact.email, job.company, job.title,
             extra={"job_id": job.id})
    return item


def update_draft(session: Session, item: OutreachLog, *, subject: str, body: str, to_email: str | None = None) -> OutreachLog:
    if item.status not in PENDING and item.status != OutreachStatus.FAILED:
        raise OutreachError(f"Cannot edit an email that is {item.status}")
    if not subject.strip() or not body.strip():
        raise OutreachError("Subject and body are required")
    if to_email is not None:
        if not is_valid_email(to_email):
            raise OutreachError(f"Invalid recipient address: {to_email!r}")
        item.to_email = to_email.strip().lower()
    item.subject, item.body = subject.strip(), body.strip() + "\n"
    item.updated_at = utcnow()
    session.add(item)
    return item


def approve(session: Session, item: OutreachLog) -> OutreachLog:
    """Human approval: move a draft into the rate-limited send queue."""
    if item.status not in (OutreachStatus.DRAFT, OutreachStatus.FAILED, OutreachStatus.CANCELLED):
        raise OutreachError(f"Email is already {item.status}")
    if not is_valid_email(item.to_email):
        raise OutreachError(f"Invalid recipient address: {item.to_email!r}")
    if session.get(Suppression, item.to_email.lower()) is not None:
        raise OutreachError(f"{item.to_email} opted out or bounced earlier; it is on the suppression list")
    contact = session.get(ReferralContact, item.contact_id)
    if contact is None or contact.status in CONTACT_DO_NOT_EMAIL:
        raise OutreachError("Contact is opted out / bounced")
    now = utcnow()
    item.status = OutreachStatus.QUEUED
    item.queued_at = now
    item.error = None
    item.updated_at = now
    session.add(item)
    if item.type == OutreachType.INITIAL and contact.status in (ContactStatus.NEW, ContactStatus.DRAFTED):
        contact.status = ContactStatus.QUEUED
        session.add(contact)
    job = session.get(Job, item.job_id)
    if job is not None:
        advance_job_status(job, JobStatus.QUEUED)
        session.add(job)
    log.info("Queued %s email to %s", item.type, item.to_email, extra={"job_id": item.job_id})
    return item


def cancel(session: Session, item: OutreachLog, reason: str = "cancelled by user") -> OutreachLog:
    if item.status not in (*PENDING, OutreachStatus.FAILED):
        raise OutreachError(f"Cannot cancel an email that is {item.status}")
    item.status = OutreachStatus.CANCELLED
    item.error = reason
    item.updated_at = utcnow()
    session.add(item)
    return item


def cancel_pending_for_contact(session: Session, contact_id: int, reason: str) -> int:
    pending = session.exec(
        select(OutreachLog).where(OutreachLog.contact_id == contact_id, col(OutreachLog.status).in_(PENDING))
    ).all()
    for item in pending:
        cancel(session, item, reason)
    return len(pending)


def suppress(session: Session, email: str, reason: str) -> None:
    key = email.strip().lower()
    if key and session.get(Suppression, key) is None:
        session.add(Suppression(email=key, reason=reason))


def mark_replied(session: Session, contact: ReferralContact, *, snippet: str | None = None) -> None:
    now = utcnow()
    if contact.status != ContactStatus.REFERRED:
        contact.status = ContactStatus.REPLIED
    contact.updated_at = now
    if snippet:
        contact.notes = f"Reply: {snippet}"[:2000]
    session.add(contact)
    last = session.exec(
        select(OutreachLog)
        .where(OutreachLog.contact_id == contact.id, OutreachLog.status == OutreachStatus.SENT)
        .order_by(col(OutreachLog.sent_at).desc())
    ).first()
    if last is not None and not last.replied:
        last.replied, last.replied_at = True, now
        session.add(last)
    cancel_pending_for_contact(session, contact.id, "contact replied")
    job = session.get(Job, contact.job_id)
    if job is not None:
        advance_job_status(job, JobStatus.REPLIED)
        session.add(job)
    log.info("%s replied about %s", contact.name, job.company if job else "a job", extra={"job_id": contact.job_id})


def mark_referred(session: Session, contact: ReferralContact) -> None:
    mark_replied(session, contact)
    contact.status = ContactStatus.REFERRED
    session.add(contact)
    job = session.get(Job, contact.job_id)
    if job is not None:
        advance_job_status(job, JobStatus.REFERRED)
        session.add(job)
    log.info("Referral secured via %s", contact.name, extra={"job_id": contact.job_id})


def opt_out(session: Session, contact: ReferralContact, reason: str = "opt_out") -> None:
    contact.status = ContactStatus.OPTED_OUT
    contact.updated_at = utcnow()
    session.add(contact)
    if contact.email:
        suppress(session, contact.email, reason)
    cancelled = cancel_pending_for_contact(session, contact.id, "recipient opted out")
    log.warning("%s opted out; %d pending email(s) cancelled and address suppressed", contact.name, cancelled,
                extra={"job_id": contact.job_id})


def mark_bounced(session: Session, contact: ReferralContact) -> None:
    contact.status = ContactStatus.BOUNCED
    contact.updated_at = utcnow()
    session.add(contact)
    if contact.email:
        suppress(session, contact.email, "bounce")
    cancel_pending_for_contact(session, contact.id, "address bounced")
    log.warning("%s <%s> bounced; address suppressed", contact.name, contact.email, extra={"job_id": contact.job_id})
