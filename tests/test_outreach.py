from __future__ import annotations

import random
import socket
from dataclasses import replace
from datetime import datetime, time, timedelta, timezone
from email import message_from_bytes
from email.policy import default as default_policy
from zoneinfo import ZoneInfo

import pytest
from sqlmodel import select

from referralpilot.config import Settings, get_settings
from referralpilot.db import session_scope
from referralpilot.models import (
    ContactStatus,
    Job,
    JobStatus,
    OutreachLog,
    OutreachStatus,
    OutreachType,
    ReferralContact,
    Suppression,
)
from referralpilot.outreach import (
    DryRunSender,
    NullReplyChecker,
    OutreachQueue,
    ReplyChecker,
    ReplyStatus,
    SendError,
    SendPolicy,
    approve,
    compose_followup,
    compose_initial,
    create_draft,
    opt_out,
    scan_followups,
    scan_replies,
    set_paused,
)
from referralpilot.outreach.replies import IMAPReplyChecker
from referralpilot.outreach.senders import (
    Attachment,
    GmailAPISender,
    SendResult,
    SMTPSender,
    build_message,
    new_message_id,
)
from referralpilot.textutil import split_sentences

START = datetime(2026, 10, 5, 4, 30, tzinfo=timezone.utc)  # Monday 10:00 in Asia/Kolkata


class Clock:
    def __init__(self, now: datetime = START):
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


def policy(**overrides) -> SendPolicy:
    base = SendPolicy(
        daily_limit=20, min_delay=180, max_delay=420, window_start=time(0), window_end=time(0),
        weekdays_only=False, tz=ZoneInfo("Asia/Kolkata"), per_company_daily_cap=3, per_recipient_cooldown_days=30,
    )
    return replace(base, **overrides)


@pytest.fixture()
def make_item(profile):
    """Create Job + ReferralContact + OutreachLog rows; returns the outreach id."""
    counter = {"n": 0}

    def factory(company: str = "Acme", email: str | None = None, status: str = OutreachStatus.QUEUED,
                queued_at: datetime = START) -> int:
        counter["n"] += 1
        n = counter["n"]
        with session_scope() as session:
            job = Job(company=company, external_id=f"j{n}", title="Software Engineer", url=f"https://x/{n}",
                      ats_type="greenhouse")
            session.add(job)
            session.flush()
            contact = ReferralContact(job_id=job.id, name=f"Person {n}", email=email or f"person{n}@{company.lower()}.com",
                                      email_confidence=0.9, status=ContactStatus.QUEUED)
            session.add(contact)
            session.flush()
            item = OutreachLog(contact_id=contact.id, job_id=job.id, to_email=contact.email, subject=f"Hello {n}",
                               body="Hi there.\n", status=status, queued_at=queued_at)
            session.add(item)
            session.flush()
            return item.id

    return factory


def make_queue(clock: Clock, settings: Settings, sender=None, **policy_overrides) -> OutreachQueue:
    return OutreachQueue(sender or DryRunSender(settings.outbox_dir), policy(**policy_overrides),
                         clock=clock, rng=random.Random(42), settings=settings)


def status_of(outreach_id: int) -> str:
    with session_scope() as session:
        return session.get(OutreachLog, outreach_id).status


# --- composer -------------------------------------------------------------------------

def _scored_job(session, demo_jobs, key="Stripe|Software Engineer, New Grad") -> Job:
    from referralpilot.tailor import analyze_job

    job = session.get(Job, demo_jobs[key])
    analyze_job(session, job)
    return job


def test_initial_email_is_personalised(demo_jobs, profile):
    with session_scope() as session:
        job = _scored_job(session, demo_jobs)
        contact = ReferralContact(job_id=job.id, name="Arjun Nair", email="arjun@stripe.com", role="SDE 1")
        draft = compose_initial(profile, job, contact, attach_resume=False)
    paragraphs = draft.body.split("\n\n")
    pitch = split_sentences(paragraphs[1]) + split_sentences(paragraphs[2])
    assert 3 <= len(pitch) <= 4
    assert paragraphs[0] == "Hi Arjun,"
    assert job.title in draft.body and job.company in draft.body and job.url in draft.body
    assert "DistKV" in draft.body or "QuickCart" in draft.body  # a JD-relevant project is highlighted
    assert "unsubscribe" in draft.body.lower()
    assert job.title in draft.subject and draft.attachment_path is None


def test_alumni_variant_and_followup(demo_jobs, profile):
    with session_scope() as session:
        job = _scored_job(session, demo_jobs)
        contact = ReferralContact(job_id=job.id, name="Priya Sharma", email="priya@stripe.com", is_alumni=True)
        draft = compose_initial(profile, job, contact)
        assert draft.subject.startswith("Fellow DTU alum")
        assert "fellow DTU alum" in draft.body
        original = OutreachLog(contact_id=1, job_id=job.id, to_email="priya@stripe.com", subject=draft.subject,
                               body=draft.body, sent_at=START)
        followup = compose_followup(profile, job, contact, original)
    assert followup.subject == f"Re: {draft.subject}"
    own_words = followup.body.split("\n\n")[1]
    assert len(split_sentences(own_words)) == 1
    assert "> Hi Priya," in followup.body  # original quoted below


# --- MIME + transports ------------------------------------------------------------------

def test_mime_threading_and_opt_out_headers(tmp_path):
    pdf = tmp_path / "resume.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    message = build_message(
        sender_name="Aarav Mehta", sender_email="aarav@example.com", to_email="priya@stripe.com", to_name="Priya Sharma",
        subject="Re: Referral", body="Hi\n", message_id="<new@example.com>", in_reply_to="<orig@example.com>",
        attachments=[Attachment(pdf, "Aarav_Mehta_Resume.pdf")],
    )
    assert message["In-Reply-To"] == "<orig@example.com>"
    assert message["References"] == "<orig@example.com>"
    assert message["List-Unsubscribe"] == "<mailto:aarav@example.com?subject=unsubscribe>"
    assert [p.get_filename() for p in message.iter_attachments()] == ["Aarav_Mehta_Resume.pdf"]
    assert new_message_id("me@gmail.com").endswith("@gmail.com>")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_smtp_sender_delivers_to_local_server():
    aiosmtpd = pytest.importorskip("aiosmtpd.controller")

    received = []

    class Handler:
        async def handle_DATA(self, server, session, envelope):  # noqa: N802 (aiosmtpd API)
            received.append(envelope)
            return "250 OK"

    port = _free_port()
    controller = aiosmtpd.Controller(Handler(), hostname="127.0.0.1", port=port)
    controller.start()
    try:
        message = build_message(sender_name="A", sender_email="a@example.com", to_email="b@example.com", to_name=None,
                                subject="Hello", body="Body text\n", message_id="<m1@example.com>")
        result = SMTPSender("127.0.0.1", port).send(message)
    finally:
        controller.stop()
    assert result.message_id == "<m1@example.com>"
    assert received and received[0].rcpt_tos == ["b@example.com"]
    parsed = message_from_bytes(received[0].content, policy=default_policy)
    assert parsed["Subject"] == "Hello" and parsed["List-Unsubscribe"]


def test_smtp_refuses_credentials_without_tls():
    pytest.importorskip("aiosmtpd.controller")
    from aiosmtpd.controller import Controller

    port = _free_port()
    controller = Controller(object(), hostname="127.0.0.1", port=port)
    controller.start()
    try:
        message = build_message(sender_name="A", sender_email="a@example.com", to_email="b@example.com", to_name=None,
                                subject="x", body="y\n", message_id="<m2@example.com>")
        with pytest.raises(SendError) as info:
            SMTPSender("127.0.0.1", port, "user", "secret").send(message)
    finally:
        controller.stop()
    assert info.value.fatal


class FakeGmail:
    def __init__(self):
        self.sent_body = None

    def users(self):
        return self

    def messages(self):
        return self

    def send(self, userId, body):  # noqa: N803 (Google API naming)
        self.sent_body = body
        return self

    def get(self, userId, id, format, metadataHeaders):  # noqa: A002, N803
        self._result = {"payload": {"headers": [{"name": "Message-ID", "value": "<gmail-id@mail.gmail.com>"}]}}
        return self

    def execute(self):
        if getattr(self, "_result", None):
            result, self._result = self._result, None
            return result
        return {"id": "abc", "threadId": "thread-1"}


def test_gmail_sender_threads_and_reads_message_id():
    service = FakeGmail()
    message = build_message(sender_name="A", sender_email="a@gmail.com", to_email="b@example.com", to_name=None,
                            subject="Re: x", body="y\n", message_id="<local@gmail.com>")
    result = GmailAPISender(lambda: service).send(message, thread_id="thread-1")
    assert service.sent_body["threadId"] == "thread-1" and "raw" in service.sent_body
    assert result == SendResult(message_id="<gmail-id@mail.gmail.com>", thread_id="thread-1", provider_id="abc")


# --- queue: hard limits ----------------------------------------------------------------------

def test_settings_cannot_loosen_hard_limits(monkeypatch):
    monkeypatch.setenv("DAILY_SEND_LIMIT", "100")
    monkeypatch.setenv("SEND_DELAY_MIN_SECONDS", "5")
    monkeypatch.setenv("SEND_DELAY_MAX_SECONDS", "10")
    settings = Settings()
    assert settings.daily_send_limit == 20
    assert settings.send_delay_min_seconds == 180 and settings.send_delay_max_seconds == 180
    built = SendPolicy.from_settings(settings)
    assert built.daily_limit == 20 and built.min_delay == 180


def test_daily_cap_of_20_per_rolling_24h(workspace, make_item):
    clock = Clock()
    ids = [make_item(company=f"Co{i}") for i in range(25)]
    queue = make_queue(clock, workspace)
    results = []
    for _ in range(25):
        result = queue.tick()
        results.append(result.status)
        if result.status != "sent":
            break
        clock.now = result.next_send_at
    assert results.count("sent") == 20 and results[-1] == "daily_limit"
    first_sent = START
    with session_scope() as session:
        status = queue.status(session)
    assert status.sent_24h == 20 and status.capacity_frees_at == first_sent + timedelta(hours=24)
    clock.now = first_sent + timedelta(hours=24, seconds=1)
    assert queue.tick().status == "sent"  # capacity frees as the window rolls
    assert sum(status_of(i) == OutreachStatus.SENT for i in ids) == 21


def test_random_delay_between_sends(workspace, make_item):
    clock = Clock()
    for i in range(6):
        make_item(company=f"Co{i}")
    queue = make_queue(clock, workspace)
    gaps = []
    for _ in range(5):
        sent = queue.tick()
        assert sent.status == "sent"
        gap = (sent.next_send_at - clock.now).total_seconds()
        gaps.append(gap)
        clock.advance(seconds=gap - 1)
        assert queue.tick().status == "waiting"  # one second too early
        clock.advance(seconds=1)
    assert all(180 <= gap <= 420 for gap in gaps) and len(set(gaps)) > 1


def test_send_window_and_weekdays(workspace, make_item):
    make_item()
    clock = Clock(datetime(2026, 10, 5, 16, 0, tzinfo=timezone.utc))  # 21:30 IST
    queue = make_queue(clock, workspace, window_start=time(9), window_end=time(19))
    result = queue.tick()
    assert result.status == "outside_window"
    assert result.next_send_at.astimezone(ZoneInfo("Asia/Kolkata")).hour == 9
    saturday = Clock(datetime(2026, 10, 10, 6, 0, tzinfo=timezone.utc))
    weekend = make_queue(saturday, workspace, window_start=time(9), window_end=time(19), weekdays_only=True)
    assert weekend.tick().status == "outside_window"


def test_per_company_cap_lets_other_companies_through(workspace, make_item):
    clock = Clock()
    acme = [make_item(company="Acme") for _ in range(4)]
    other = make_item(company="Globex", queued_at=START + timedelta(minutes=1))
    queue = make_queue(clock, workspace)
    for _ in range(4):
        result = queue.tick()
        assert result.status == "sent"
        clock.now = result.next_send_at
    assert [status_of(i) for i in acme] == ["sent", "sent", "sent", "queued"]
    assert status_of(other) == "sent"
    assert queue.tick().status == "idle"


def test_cooldown_suppression_and_opt_out_are_skipped(workspace, make_item):
    clock = Clock()
    first = make_item(company="Acme", email="same@acme.com")
    queue = make_queue(clock, workspace)
    clock.now = queue.tick().next_send_at
    repeat = make_item(company="Acme", email="same@acme.com")  # different job, same person
    suppressed = make_item(company="Globex", email="gone@globex.com")
    with session_scope() as session:
        session.add(Suppression(email="gone@globex.com", reason="opt_out"))
    assert queue.tick().status == "idle"
    with session_scope() as session:
        repeat_row = session.get(OutreachLog, repeat)
        assert repeat_row.status == OutreachStatus.SKIPPED and "last 30 days" in repeat_row.error
        assert session.get(OutreachLog, suppressed).status == OutreachStatus.SKIPPED
    assert status_of(first) == "sent"


def test_pause_blocks_sending(workspace, make_item):
    item = make_item()
    queue = make_queue(Clock(), workspace)
    with session_scope() as session:
        set_paused(session, True, "testing")
    assert queue.tick().status == "paused" and status_of(item) == "queued"


class FlakySender(DryRunSender):
    def __init__(self, outbox, error: SendError):
        super().__init__(outbox)
        self.error = error

    def send(self, message, *, thread_id=None):
        raise self.error


def test_transient_failure_is_retried_later(workspace, make_item):
    clock = Clock()
    item = make_item()
    queue = make_queue(clock, workspace, sender=FlakySender(workspace.outbox_dir, SendError("timeout")))
    assert queue.tick().status == "error"
    with session_scope() as session:
        row = session.get(OutreachLog, item)
        assert row.status == OutreachStatus.QUEUED and row.scheduled_for > START and row.attempts == 1


def test_bounce_suppresses_and_fatal_error_pauses(workspace, make_item):
    clock = Clock()
    bounced = make_item(email="nobody@acme.com")
    queue = make_queue(clock, workspace, sender=FlakySender(workspace.outbox_dir, SendError("550", bounced=True)))
    queue.tick()
    with session_scope() as session:
        assert session.get(OutreachLog, bounced).status == OutreachStatus.FAILED
        assert session.get(Suppression, "nobody@acme.com") is not None
    make_item(company="Globex")
    clock.advance(minutes=10)
    fatal = make_queue(clock, workspace, sender=FlakySender(workspace.outbox_dir, SendError("auth", fatal=True)))
    fatal.tick()
    clock.advance(minutes=10)
    assert fatal.tick().status == "paused"


# --- drafts, follow-ups and replies --------------------------------------------------------------

class ScriptedChecker(ReplyChecker):
    def __init__(self, statuses: dict[str, ReplyStatus]):
        self.statuses = statuses

    def check(self, log):
        return self.statuses.get(log.to_email, ReplyStatus())


def _send_first_emails(workspace, demo_jobs, clock, count=2) -> list[int]:
    with session_scope() as session:
        job = _scored_job(session, demo_jobs)
        ids = []
        for name in ["Priya Sharma", "Rohan Das", "Arjun Nair"][:count]:
            contact = ReferralContact(job_id=job.id, name=name, email=f"{name.split()[0].lower()}@stripe.com",
                                      email_confidence=0.9)
            session.add(contact)
            session.flush()
            draft = create_draft(session, contact)
            approve(session, draft)
            ids.append(draft.id)
        assert job.status == JobStatus.QUEUED
    queue = make_queue(clock, workspace)
    for _ in ids:
        result = queue.tick()
        assert result.status == "sent"
        clock.now = result.next_send_at
    return ids


def test_followup_after_four_days_without_reply(workspace, demo_jobs):
    clock = Clock()
    initial = _send_first_emails(workspace, demo_jobs, clock)
    assert scan_followups(NullReplyChecker(), clock=clock) == []  # too early
    clock.advance(days=4)
    created = scan_followups(NullReplyChecker(), clock=clock)
    assert len(created) == 2
    assert scan_followups(NullReplyChecker(), clock=clock) == []  # only one follow-up each
    with session_scope() as session:
        followup = session.get(OutreachLog, created[0])
        parent = session.get(OutreachLog, followup.parent_id)
        assert followup.type == OutreachType.FOLLOWUP and followup.status == OutreachStatus.QUEUED
        assert followup.in_reply_to == parent.message_id and followup.thread_id == parent.thread_id
        assert parent.id in initial
    queue = make_queue(clock, workspace)
    assert queue.tick().status == "sent"
    eml = sorted(workspace.outbox_dir.glob("*.eml"))[-1]
    message = message_from_bytes(eml.read_bytes(), policy=default_policy)
    assert message["In-Reply-To"] and message["Subject"].startswith("Re:")
    with session_scope() as session:
        assert session.get(Job, demo_jobs["Stripe|Software Engineer, New Grad"]).status == JobStatus.FOLLOWUP_SENT


def test_replies_stop_followups_and_opt_outs_suppress(workspace, demo_jobs):
    clock = Clock()
    _send_first_emails(workspace, demo_jobs, clock, count=3)
    clock.advance(days=4)
    checker = ScriptedChecker({
        "priya@stripe.com": ReplyStatus(replied=True, snippet="Happy to refer you!"),
        "rohan@stripe.com": ReplyStatus(replied=True, opted_out=True, snippet="Please unsubscribe me"),
    })
    created = scan_followups(checker, clock=clock)
    with session_scope() as session:
        followups = [session.get(OutreachLog, i) for i in created]
        assert [f.to_email for f in followups] == ["arjun@stripe.com"]
        contacts = {c.email: c for c in session.exec(select(ReferralContact)).all()}
        assert contacts["priya@stripe.com"].status == ContactStatus.REPLIED
        assert contacts["rohan@stripe.com"].status == ContactStatus.OPTED_OUT
        assert session.get(Suppression, "rohan@stripe.com") is not None
        assert session.get(Job, demo_jobs["Stripe|Software Engineer, New Grad"]).status == JobStatus.REPLIED


def test_scan_replies_and_manual_opt_out_cancels_pending(workspace, demo_jobs):
    clock = Clock()
    _send_first_emails(workspace, demo_jobs, clock, count=2)
    changed = scan_replies(ScriptedChecker({"rohan@stripe.com": ReplyStatus(replied=True)}), clock=clock)
    assert changed == 1
    clock.advance(days=4)
    [followup_id] = scan_followups(NullReplyChecker(), clock=clock)
    with session_scope() as session:
        followup = session.get(OutreachLog, followup_id)
        opt_out(session, session.get(ReferralContact, followup.contact_id))
    assert status_of(followup_id) == OutreachStatus.CANCELLED
    with session_scope() as session:
        with pytest.raises(Exception, match="suppression|opted out"):
            approve(session, session.get(OutreachLog, followup_id))


class FakeIMAP:
    """Minimal imaplib.IMAP4 stand-in."""

    def __init__(self, messages: dict[bytes, bytes], searches: dict[tuple, list[bytes]]):
        self.messages, self.searches = messages, searches

    def login(self, user, password):
        return "OK", [b""]

    def select(self, mailbox, readonly=False):
        return "OK", [b"1"]

    def search(self, charset, *criteria):
        key = criteria[0] if criteria[0] != "HEADER" else (criteria[0], criteria[1])
        ids = self.searches.get(key, []) if isinstance(key, tuple) else self.searches.get(criteria[0], [])
        if criteria[0] == "FROM" and "mailer-daemon" in criteria[1]:
            ids = self.searches.get("BOUNCE", [])
        elif criteria[0] == "FROM":
            ids = self.searches.get("FROM", [])
        return "OK", [b" ".join(ids)]

    def fetch(self, msg_id, query):
        return "OK", [(b"1 (BODY[] {100}", self.messages[msg_id])]

    def logout(self):
        return "BYE", [b""]


def test_imap_reply_and_bounce_detection():
    log = OutreachLog(contact_id=1, job_id=1, to_email="priya@stripe.com", subject="s", body="b",
                      message_id="<orig@example.com>", sent_at=START)
    reply = (b"From: Priya <priya@stripe.com>\r\nSubject: Re: s\r\n\r\nSure, please unsubscribe me from these.\r\n"
             b"On Mon, 5 Oct 2026 Aarav wrote:\r\n> referral\r\n")
    replied = IMAPReplyChecker("h", 993, "u", "p", imap_factory=lambda: FakeIMAP(
        {b"7": reply}, {("HEADER", "In-Reply-To"): [b"7"]}))
    status = replied.check(log)
    assert status.replied and status.opted_out
    bounced = IMAPReplyChecker("h", 993, "u", "p", imap_factory=lambda: FakeIMAP({}, {"BOUNCE": [b"9"]}))
    assert bounced.check(log).bounced
    quiet = IMAPReplyChecker("h", 993, "u", "p", imap_factory=lambda: FakeIMAP({}, {}))
    assert not quiet.check(log).any


def test_runtime_queue_uses_configured_backend(workspace):
    from referralpilot.runtime import get_queue

    queue = get_queue()
    assert queue.sender.dry_run and queue.policy.daily_limit == 20
    assert get_settings().is_dry_run


# --- failure-mode hardening -------------------------------------------------------------------

def test_unbuildable_message_fails_once_instead_of_looping(workspace, make_item, monkeypatch):
    item = make_item()
    queue = make_queue(Clock(), workspace)

    def broken_build(session, outreach):
        raise OSError("attachment vanished")

    monkeypatch.setattr(queue, "_build", broken_build)
    assert queue.tick().status == "error"
    assert status_of(item) == OutreachStatus.FAILED
    assert queue.tick().status == "idle"


def test_unexpected_sender_exception_is_not_retried(workspace, make_item):
    class Exploding(DryRunSender):
        def send(self, message, *, thread_id=None):
            raise RuntimeError("socket closed after DATA")

    item = make_item()
    queue = make_queue(Clock(), workspace, sender=Exploding(workspace.outbox_dir))
    assert queue.tick().status == "error"
    with session_scope() as session:
        row = session.get(OutreachLog, item)
        assert row.status == OutreachStatus.FAILED and "Unexpected" in row.error


def test_interrupted_send_is_failed_not_resent(workspace, make_item):
    item = make_item()
    with session_scope() as session:
        row = session.get(OutreachLog, item)
        row.status = OutreachStatus.SENDING
        row.updated_at = START - timedelta(hours=2)
        session.add(row)
    assert make_queue(Clock(), workspace).tick().status == "idle"
    with session_scope() as session:
        row = session.get(OutreachLog, item)
        assert row.status == OutreachStatus.FAILED and "interrupted" in row.error
    assert not list(workspace.outbox_dir.glob("*.eml"))


def test_cancelled_followup_is_not_recreated(workspace, demo_jobs):
    from referralpilot.outreach import cancel

    clock = Clock()
    _send_first_emails(workspace, demo_jobs, clock, count=1)
    clock.advance(days=4)
    [followup_id] = scan_followups(NullReplyChecker(), clock=clock)
    with session_scope() as session:
        cancel(session, session.get(OutreachLog, followup_id))
    clock.advance(hours=2)
    assert scan_followups(NullReplyChecker(), clock=clock) == []
