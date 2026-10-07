"""Drip-feed send queue with hard anti-spam limits.

Each `tick()` sends at most one email, and only when all of these hold:
  * sending is not paused and the local time is inside the send window;
  * the randomised 3-7 minute gap since the previous send has elapsed;
  * fewer than 20 emails (initial + follow-up) went out in the last 24 hours;
  * the recipient has not opted out / bounced / replied, was not contacted in
    the cooldown period, and the company's daily cap is not exhausted.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, update
from sqlmodel import Session, col, select

from ..activity import get_logger
from ..config import HARD_DAILY_SEND_CAP, MIN_SEND_DELAY_SECONDS, Settings, get_settings
from ..db import session_scope, state_get, state_get_datetime, state_set, state_set_datetime
from ..models import (
    CONTACT_DO_NOT_EMAIL,
    CandidateProfile,
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
from ..prospector.patterns import is_valid_email as is_sendable_address
from .senders import Sender, SendError, build_message, new_message_id, resume_attachment

log = get_logger("outreach")

STATE_NEXT_SEND = "outreach:next_send_at"
STATE_PAUSED = "outreach:paused"
STATE_PAUSE_REASON = "outreach:pause_reason"
RETRY_BACKOFF = timedelta(minutes=15)
STALE_SENDING_AFTER = timedelta(hours=1)


@dataclass(frozen=True)
class SendPolicy:
    daily_limit: int
    min_delay: int
    max_delay: int
    window_start: time
    window_end: time
    weekdays_only: bool
    tz: ZoneInfo
    per_company_daily_cap: int
    per_recipient_cooldown_days: int
    max_attempts: int = 3

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> SendPolicy:
        settings = settings or get_settings()
        min_delay = max(MIN_SEND_DELAY_SECONDS, settings.send_delay_min_seconds)
        return cls(
            daily_limit=max(1, min(settings.daily_send_limit, HARD_DAILY_SEND_CAP)),
            min_delay=min_delay,
            max_delay=max(min_delay, settings.send_delay_max_seconds),
            window_start=settings.window_start,
            window_end=settings.window_end,
            weekdays_only=settings.send_weekdays_only,
            tz=settings.tz,
            per_company_daily_cap=max(1, settings.per_company_daily_cap),
            per_recipient_cooldown_days=max(0, settings.per_recipient_cooldown_days),
        )

    def in_window(self, now: datetime) -> bool:
        local = now.astimezone(self.tz)
        if self.weekdays_only and local.weekday() >= 5:
            return False
        current = local.time()
        if self.window_start == self.window_end:
            return True
        if self.window_start < self.window_end:
            return self.window_start <= current < self.window_end
        return current >= self.window_start or current < self.window_end  # overnight window

    def describe_window(self) -> str:
        days = "weekdays" if self.weekdays_only else "any day"
        if self.window_start == self.window_end:
            return f"any time, {days}"
        return f"{self.window_start:%H:%M}-{self.window_end:%H:%M} {self.tz.key}, {days}"

    def next_window_start(self, now: datetime) -> datetime:
        if self.in_window(now):
            return now
        local = now.astimezone(self.tz)
        for day in range(0, 8):
            candidate = datetime.combine(local.date() + timedelta(days=day), self.window_start, tzinfo=self.tz)
            if candidate > local and (not self.weekdays_only or candidate.weekday() < 5):
                return candidate.astimezone(ZoneInfo("UTC"))
        return now


@dataclass
class TickResult:
    status: str  # sent | idle | paused | outside_window | waiting | daily_limit | error
    outreach_id: int | None = None
    detail: str = ""
    next_send_at: datetime | None = None


@dataclass
class QueueStatus:
    backend: str
    dry_run: bool
    sent_24h: int
    limit: int
    queued: int
    drafts: int
    paused: bool
    pause_reason: str | None
    in_window: bool
    window: str
    next_send_at: datetime | None
    next_window_at: datetime | None
    capacity_frees_at: datetime | None


def is_paused(session: Session) -> tuple[bool, str | None]:
    return state_get(session, STATE_PAUSED) == "1", state_get(session, STATE_PAUSE_REASON)


def set_paused(session: Session, paused: bool, reason: str | None = None) -> None:
    state_set(session, STATE_PAUSED, "1" if paused else "0")
    state_set(session, STATE_PAUSE_REASON, reason if paused else None)
    log.info("Outreach %s%s", "paused" if paused else "resumed", f": {reason}" if paused and reason else "")


def sent_times(session: Session, now: datetime, dry_run: bool) -> list[datetime]:
    """Send timestamps inside the rolling 24-hour window (initials and follow-ups)."""
    rows = session.exec(
        select(OutreachLog.sent_at).where(
            OutreachLog.status == OutreachStatus.SENT,
            OutreachLog.sent_at > now - timedelta(hours=24),
            OutreachLog.dry_run == dry_run,
        )
    ).all()
    return sorted(row for row in rows if row is not None)


def _company_sent_today(session: Session, company: str, now: datetime, dry_run: bool) -> int:
    return session.exec(
        select(func.count()).select_from(OutreachLog).join(Job, Job.id == OutreachLog.job_id).where(
            Job.company == company,
            OutreachLog.status == OutreachStatus.SENT,
            OutreachLog.sent_at > now - timedelta(hours=24),
            OutreachLog.dry_run == dry_run,
        )
    ).one()


def skip_reason(session: Session, item: OutreachLog, policy: SendPolicy, now: datetime) -> str | None:
    """Why `item` must never be sent (it is then marked SKIPPED), or None if it may go out."""
    contact = session.get(ReferralContact, item.contact_id)
    job = session.get(Job, item.job_id)
    if contact is None or job is None:
        return "contact or job no longer exists"
    if not is_sendable_address(item.to_email):
        return f"invalid recipient address {item.to_email!r}"
    if session.get(Suppression, item.to_email.lower()) is not None:
        return "recipient opted out / bounced earlier (suppression list)"
    if contact.status in CONTACT_DO_NOT_EMAIL:
        return f"contact is {contact.status}"
    if item.type == OutreachType.FOLLOWUP:
        parent = session.get(OutreachLog, item.parent_id) if item.parent_id else None
        if contact.status in {ContactStatus.REPLIED, ContactStatus.REFERRED} or (parent and parent.replied):
            return "contact already replied"
        if parent is None or parent.status != OutreachStatus.SENT:
            return "original email was never sent"
        return None
    already = session.exec(
        select(OutreachLog.id).where(
            OutreachLog.contact_id == item.contact_id,
            OutreachLog.type == OutreachType.INITIAL,
            OutreachLog.status == OutreachStatus.SENT,
        )
    ).first()
    if already:
        return "this contact was already emailed about this job"
    if policy.per_recipient_cooldown_days:
        recent = session.exec(
            select(OutreachLog.id).where(
                func.lower(OutreachLog.to_email) == item.to_email.lower(),
                OutreachLog.type == OutreachType.INITIAL,
                OutreachLog.status == OutreachStatus.SENT,
                OutreachLog.sent_at > now - timedelta(days=policy.per_recipient_cooldown_days),
            )
        ).first()
        if recent:
            return f"recipient was cold-emailed in the last {policy.per_recipient_cooldown_days} days"
    return None


class OutreachQueue:
    def __init__(
        self,
        sender: Sender,
        policy: SendPolicy | None = None,
        *,
        clock: Callable[[], datetime] = utcnow,
        rng: random.Random | None = None,
        settings: Settings | None = None,
    ):
        self.sender = sender
        self.settings = settings or get_settings()
        self.policy = policy or SendPolicy.from_settings(self.settings)
        self.clock = clock
        self.rng = rng or random.SystemRandom()

    # --- status ---------------------------------------------------------------
    def status(self, session: Session) -> QueueStatus:
        now = self.clock()
        times = sent_times(session, now, self.sender.dry_run)
        paused, reason = is_paused(session)
        count = lambda status: session.exec(  # noqa: E731
            select(func.count()).select_from(OutreachLog).where(OutreachLog.status == status)
        ).one()
        full = len(times) >= self.policy.daily_limit
        return QueueStatus(
            backend=self.sender.name,
            dry_run=self.sender.dry_run,
            sent_24h=len(times),
            limit=self.policy.daily_limit,
            queued=count(OutreachStatus.QUEUED),
            drafts=count(OutreachStatus.DRAFT),
            paused=paused,
            pause_reason=reason,
            in_window=self.policy.in_window(now),
            window=self.policy.describe_window(),
            next_send_at=state_get_datetime(session, STATE_NEXT_SEND),
            next_window_at=None if self.policy.in_window(now) else self.policy.next_window_start(now),
            capacity_frees_at=times[0] + timedelta(hours=24) if full else None,
        )

    # --- one scheduler tick ---------------------------------------------------
    def tick(self) -> TickResult:
        now = self.clock()
        with session_scope() as session:
            paused, reason = is_paused(session)
            if paused:
                return TickResult("paused", detail=reason or "")
            if not self.policy.in_window(now):
                return TickResult("outside_window", next_send_at=self.policy.next_window_start(now))
            next_at = state_get_datetime(session, STATE_NEXT_SEND)
            if next_at and now < next_at:
                return TickResult("waiting", next_send_at=next_at)
            times = sent_times(session, now, self.sender.dry_run)
            if len(times) >= self.policy.daily_limit:
                frees_at = times[0] + timedelta(hours=24)
                state_set_datetime(session, STATE_NEXT_SEND, frees_at)
                log.info("Daily limit reached (%d/24h); next send after %s UTC",
                         self.policy.daily_limit, frees_at.strftime("%Y-%m-%d %H:%M"))
                return TickResult("daily_limit", next_send_at=frees_at)
            self._sweep_stale(session, now)
            item = self._next_sendable(session, now)
            if item is None:
                return TickResult("idle")
            claimed = session.exec(
                update(OutreachLog)
                .where(col(OutreachLog.id) == item.id, col(OutreachLog.status) == OutreachStatus.QUEUED)
                .values(status=OutreachStatus.SENDING, attempts=OutreachLog.attempts + 1, updated_at=now)
            )
            if claimed.rowcount != 1:
                return TickResult("idle", detail="item was claimed by another worker")
            session.refresh(item)
            try:
                message, thread_id = self._build(session, item)
            except Exception as exc:  # e.g. unreadable attachment: fail it instead of retrying every tick
                item.status = OutreachStatus.FAILED
                item.error = f"Could not build the message: {exc}"[:1000]
                session.add(item)
                log.error("Could not build email to %s: %s", item.to_email, exc, extra={"job_id": item.job_id})
                return TickResult("error", item.id, detail=str(exc))
            item_id, to_email = item.id, item.to_email

        try:
            result = self.sender.send(message, thread_id=thread_id)
        except SendError as exc:
            return self._record_failure(item_id, exc, now)
        except Exception as exc:  # unknown outcome: never risk a duplicate by retrying automatically
            return self._record_failure(item_id, SendError(f"Unexpected send error: {exc}", permanent=True), now)

        delay = self.rng.uniform(self.policy.min_delay, self.policy.max_delay)
        next_send = now + timedelta(seconds=delay)
        with session_scope() as session:
            item = session.get(OutreachLog, item_id)
            item.status = OutreachStatus.SENT
            item.sent_at = now
            item.message_id = result.message_id
            item.thread_id = result.thread_id
            item.dry_run = self.sender.dry_run
            item.error = None
            item.updated_at = now
            session.add(item)
            contact = session.get(ReferralContact, item.contact_id)
            job = session.get(Job, item.job_id)
            followup = item.type == OutreachType.FOLLOWUP
            if contact and contact.status not in {ContactStatus.REPLIED, ContactStatus.REFERRED}:
                contact.status = ContactStatus.FOLLOWUP_SENT if followup else ContactStatus.CONTACTED
                contact.updated_at = now
                session.add(contact)
            if job:
                advance_job_status(job, JobStatus.FOLLOWUP_SENT if followup else JobStatus.CONTACTED)
                session.add(job)
            state_set_datetime(session, STATE_NEXT_SEND, next_send)
            sent_count = len(sent_times(session, now + timedelta(seconds=1), self.sender.dry_run))
        mode = "[dry-run] " if self.sender.dry_run else ""
        log.info("%sSent %s email to %s (%d/%d in 24h); next send in %.1f min",
                 mode, "follow-up" if followup else "referral", to_email, sent_count, self.policy.daily_limit,
                 delay / 60, extra={"job_id": job.id if job else None})
        return TickResult("sent", item_id, next_send_at=next_send)

    def _sweep_stale(self, session: Session, now: datetime) -> None:
        """Items stuck in SENDING (process died mid-send) are failed, never re-sent automatically."""
        stale = session.exec(
            select(OutreachLog).where(
                OutreachLog.status == OutreachStatus.SENDING,
                OutreachLog.updated_at < now - STALE_SENDING_AFTER,
            )
        ).all()
        for item in stale:
            item.status = OutreachStatus.FAILED
            item.error = "Send was interrupted - check your Sent folder before retrying"
            item.updated_at = now
            session.add(item)
            log.warning("Email to %s was interrupted mid-send; marked failed", item.to_email,
                        extra={"job_id": item.job_id})

    def _next_sendable(self, session: Session, now: datetime) -> OutreachLog | None:
        due = session.exec(
            select(OutreachLog)
            .where(OutreachLog.status == OutreachStatus.QUEUED)
            .where((OutreachLog.scheduled_for == None) | (OutreachLog.scheduled_for <= now))  # noqa: E711
            .order_by(col(OutreachLog.queued_at).asc(), col(OutreachLog.id).asc())
        ).all()
        capped: set[str] = set()
        for item in due:
            reason = skip_reason(session, item, self.policy, now)
            if reason:
                item.status = OutreachStatus.SKIPPED
                item.error = reason
                item.updated_at = now
                session.add(item)
                log.warning("Skipped email to %s: %s", item.to_email, reason, extra={"job_id": item.job_id})
                continue
            job = session.get(Job, item.job_id)
            if job.company in capped:
                continue
            if _company_sent_today(session, job.company, now, self.sender.dry_run) >= self.policy.per_company_daily_cap:
                capped.add(job.company)
                continue
            return item
        return None

    def _build(self, session: Session, item: OutreachLog):
        profile: CandidateProfile | None = get_active_profile(session)
        contact = session.get(ReferralContact, item.contact_id)
        sender_email = self.settings.sender_email or (profile.email if profile else "")
        sender_name = self.settings.sender_name or (profile.full_name if profile else "")
        parent = session.get(OutreachLog, item.parent_id) if item.parent_id else None
        attachments = []
        if item.type == OutreachType.INITIAL and self.settings.attach_resume:
            attachment = resume_attachment(item.attachment_path, profile.full_name if profile else "Resume")
            if attachment:
                attachments.append(attachment)
        in_reply_to = item.in_reply_to or (parent.message_id if parent else None)
        message = build_message(
            sender_name=sender_name,
            sender_email=sender_email,
            to_email=item.to_email,
            to_name=contact.name if contact else None,
            subject=item.subject,
            body=item.body,
            message_id=new_message_id(sender_email),
            in_reply_to=in_reply_to,
            references=[in_reply_to] if in_reply_to else None,
            attachments=attachments,
        )
        thread_id = item.thread_id or (parent.thread_id if parent else None)
        return message, thread_id

    def _record_failure(self, item_id: int, exc: SendError, now: datetime) -> TickResult:
        with session_scope() as session:
            item = session.get(OutreachLog, item_id)
            item.error = str(exc)[:1000]
            item.updated_at = now
            if exc.permanent or item.attempts >= self.policy.max_attempts:
                item.status = OutreachStatus.FAILED
            else:
                item.status = OutreachStatus.QUEUED
                item.scheduled_for = now + RETRY_BACKOFF * item.attempts
            session.add(item)
            if exc.bounced:
                contact = session.get(ReferralContact, item.contact_id)
                if contact:
                    contact.status = ContactStatus.BOUNCED
                    session.add(contact)
                if session.get(Suppression, item.to_email.lower()) is None:
                    session.add(Suppression(email=item.to_email.lower(), reason="bounce"))
            if exc.fatal:
                set_paused(session, True, f"send error: {exc}"[:200])
            # Back off before trying anything else so failures never turn into a burst.
            state_set_datetime(session, STATE_NEXT_SEND, now + timedelta(seconds=self.policy.min_delay))
        log.error("Send to %s failed: %s", item.to_email, exc, extra={"job_id": item.job_id})
        return TickResult("error", item_id, detail=str(exc))
