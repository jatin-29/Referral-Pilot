"""Reply scanning and the automatic follow-up scheduler.

A one-sentence follow-up is queued (in the same thread) when an initial email
has had no reply for FOLLOWUP_AFTER_DAYS (default 4) - but only after a final
reply check, and never for contacts who replied, opted out or bounced.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from sqlmodel import col, select

from ..activity import get_logger
from ..config import Settings, get_settings
from ..db import session_scope
from ..models import ContactStatus, Job, OutreachLog, OutreachStatus, OutreachType, ReferralContact, utcnow
from ..profile import get_active_profile
from .composer import compose_followup
from .replies import ReplyChecker, ReplyStatus
from .service import mark_bounced, mark_replied, opt_out

log = get_logger("outreach")

_FINAL_CONTACT_STATES = {ContactStatus.REPLIED, ContactStatus.REFERRED, ContactStatus.OPTED_OUT, ContactStatus.BOUNCED}


def apply_reply_status(log_id: int, status: ReplyStatus) -> None:
    with session_scope() as session:
        item = session.get(OutreachLog, log_id)
        contact = session.get(ReferralContact, item.contact_id) if item else None
        if contact is None:
            return
        if status.bounced:
            mark_bounced(session, contact)
        elif status.opted_out:
            mark_replied(session, contact, snippet=status.snippet)
            opt_out(session, contact)
        elif status.replied:
            mark_replied(session, contact, snippet=status.snippet)


def scan_replies(checker: ReplyChecker, *, clock: Callable[[], datetime] = utcnow, lookback_days: int = 30) -> int:
    """Check the latest sent email per active contact; returns how many changed state."""
    now = clock()
    with session_scope() as session:
        rows = session.exec(
            select(OutreachLog)
            .join(ReferralContact, ReferralContact.id == OutreachLog.contact_id)
            .where(
                OutreachLog.status == OutreachStatus.SENT,
                OutreachLog.replied == False,  # noqa: E712
                OutreachLog.sent_at > now - timedelta(days=lookback_days),
                col(ReferralContact.status).not_in(list(_FINAL_CONTACT_STATES)),
            )
            .order_by(col(OutreachLog.sent_at).desc())
        ).all()
        latest: dict[int, OutreachLog] = {}
        for item in rows:
            latest.setdefault(item.contact_id, item)
    changed = 0
    for item in latest.values():
        try:
            status = checker.check(item)
        except Exception as exc:  # network / auth problems should not stop the scan
            log.warning("Reply check for %s failed: %s", item.to_email, exc)
            continue
        if status.any:
            apply_reply_status(item.id, status)
            changed += 1
    if changed:
        log.info("Reply scan: %d contact(s) updated", changed)
    return changed


def scan_followups(
    checker: ReplyChecker,
    *,
    clock: Callable[[], datetime] = utcnow,
    settings: Settings | None = None,
) -> list[int]:
    """Queue follow-ups for initial emails with no reply after N days. Returns new outreach ids."""
    settings = settings or get_settings()
    if settings.max_followups <= 0:
        return []
    now = clock()
    cutoff = now - timedelta(days=settings.followup_after_days)
    with session_scope() as session:
        due = session.exec(
            select(OutreachLog)
            .join(ReferralContact, ReferralContact.id == OutreachLog.contact_id)
            .where(
                OutreachLog.type == OutreachType.INITIAL,
                OutreachLog.status == OutreachStatus.SENT,
                OutreachLog.replied == False,  # noqa: E712
                OutreachLog.sent_at <= cutoff,
                col(ReferralContact.status).not_in(list(_FINAL_CONTACT_STATES)),
            )
        ).all()
        candidates = []
        for item in due:
            # Every follow-up counts, including ones the user cancelled - those must not come back.
            existing = session.exec(select(OutreachLog.id).where(OutreachLog.parent_id == item.id)).all()
            if len(existing) < settings.max_followups:
                candidates.append(item)

    created: list[int] = []
    for item in candidates:
        try:
            status = checker.check(item)  # last look before nudging anyone
        except Exception as exc:
            log.warning("Reply check for %s failed, follow-up postponed: %s", item.to_email, exc)
            continue
        if status.any:
            apply_reply_status(item.id, status)
            continue
        with session_scope() as session:
            original = session.get(OutreachLog, item.id)
            contact = session.get(ReferralContact, original.contact_id)
            job = session.get(Job, original.job_id)
            profile = get_active_profile(session)
            if contact is None or job is None or profile is None:
                continue
            draft = compose_followup(profile, job, contact, original)
            auto = settings.followup_auto_queue
            followup = OutreachLog(
                contact_id=contact.id,
                job_id=job.id,
                type=OutreachType.FOLLOWUP,
                status=OutreachStatus.QUEUED if auto else OutreachStatus.DRAFT,
                to_email=original.to_email,
                subject=draft.subject,
                body=draft.body,
                parent_id=original.id,
                in_reply_to=original.message_id,
                thread_id=original.thread_id,
                queued_at=now if auto else None,
                scheduled_for=now,
            )
            session.add(followup)
            session.flush()
            created.append(followup.id)
            log.info("%s follow-up to %s (no reply after %g days)", "Queued" if auto else "Drafted",
                     contact.name, settings.followup_after_days, extra={"job_id": job.id})
    return created
