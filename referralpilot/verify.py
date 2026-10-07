"""End-to-end verification: fetch -> filter/dedupe -> match -> tailor -> prospect -> compose
-> queue -> rate-limited dry-run send -> threaded follow-up -> opt-out.

Runs in a throwaway workspace (its own SQLite file and exports folder) with the
dry-run sender and a simulated clock, so it never emails anyone and finishes in
seconds. `--live` tries the real Greenhouse/Lever APIs first.
"""

from __future__ import annotations

import os
import random
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email import message_from_bytes
from email.policy import default as default_policy
from pathlib import Path


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "") -> bool:
        self.checks.append(Check(name, bool(ok), detail))
        mark = "\033[32m✔\033[0m" if ok else "\033[31m✘\033[0m"
        print(f"  {mark} {name}" + (f" - {detail}" if detail else ""))
        return bool(ok)


class Clock:
    """Simulated clock so the 3-7 minute gaps and the 4-day follow-up are instant."""

    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta) -> None:
        self.now += timedelta(**delta)


def _morning(tz) -> datetime:
    from .models import utcnow

    local = utcnow().astimezone(tz)
    start = local.replace(hour=10, minute=0, second=0, microsecond=0)
    if start < local:
        start += timedelta(days=1)
    return start.astimezone(utcnow().tzinfo)


def _configure_workspace(workdir: Path) -> None:
    os.environ.update({
        "DATA_DIR": str(workdir / "data"),
        "DATABASE_URL": f"sqlite:///{(workdir / 'data' / 'verify.db').as_posix()}",
        "EXPORTS_DIR": str(workdir / "exports"),
        "EMAIL_BACKEND": "dry_run",
        "VERIFY_MX": "false",
        "LOCATION_KEYWORDS": "",
        "SEND_WINDOW_START": "09:00",
        "SEND_WINDOW_END": "19:00",
        "SEND_WEEKDAYS_ONLY": "false",
        "HUNTER_API_KEY": "demo-key",
        "SCHEDULER_ENABLED": "false",
    })
    from . import db
    from .config import reset_settings

    reset_settings()
    db.set_engine(db.make_engine(os.environ["DATABASE_URL"]))
    db.init_db()


def run_verification(*, live: bool = False, keep: bool = False) -> int:
    from . import fetch
    from .config import Settings

    workdir = Path(tempfile.mkdtemp(prefix="referralpilot-verify-"))
    saved_env = dict(os.environ)
    # Deterministic run: ignore the user's .env (their limits, SMTP credentials...).
    saved_env_file = Settings.model_config.get("env_file")
    Settings.model_config["env_file"] = None
    saved_sleep = fetch.sleep
    fetch.sleep = lambda _seconds: None  # no politeness pauses against mock servers
    report = Report()
    started = time.monotonic()
    try:
        _configure_workspace(workdir)
        from .activity import setup_logging

        setup_logging("WARNING", persist=True, console=False)
        _run(report, live=live, workdir=workdir)
    except Exception as exc:  # report the crash as a failed check rather than a traceback
        import traceback

        traceback.print_exc()
        report.add("pipeline ran without errors", False, f"{type(exc).__name__}: {exc}")
    finally:
        from .activity import shutdown_logging
        from . import db
        from .config import reset_settings

        shutdown_logging()
        Settings.model_config["env_file"] = saved_env_file
        fetch.sleep = saved_sleep
        db.set_engine(None)
        os.environ.clear()
        os.environ.update(saved_env)
        reset_settings()

    passed = sum(c.ok for c in report.checks)
    total = len(report.checks)
    print(f"\n{passed}/{total} checks passed in {time.monotonic() - started:.1f}s")
    if keep:
        print(f"Workspace kept at {workdir}")
    else:
        shutil.rmtree(workdir, ignore_errors=True)
    return 0 if passed == total else 1


def _run(report: Report, *, live: bool, workdir: Path) -> None:
    import httpx
    from sqlmodel import select

    from .config import get_settings
    from .db import session_scope
    from .demo import DEMO_COMPANIES, demo_client
    from .fetch import build_client
    from .harvester import harvest
    from .models import (
        Company,
        ContactStatus,
        Job,
        JobStatus,
        OutreachLog,
        OutreachStatus,
        OutreachType,
        ReferralContact,
    )
    from .outreach import (
        DryRunSender,
        NullReplyChecker,
        OutreachQueue,
        SendPolicy,
        approve,
        create_draft,
        opt_out,
        scan_followups,
    )
    from .prospector import prospect_job
    from .prospector.providers import build_providers
    from .seed import seed_profile, upsert_company
    from .tailor import tailor_job
    from .textutil import split_sentences

    settings = get_settings()
    print(f"Workspace: {workdir}\n\n1. Fetch")
    with session_scope() as session:
        seed_profile(session)
        for entry in DEMO_COMPANIES:
            upsert_company(session, entry)

    client: httpx.Client = demo_client()
    source = "bundled mock API responses"
    if live:
        live_client = build_client(settings)
        try:
            stats = harvest(client=live_client)
            if sum(s.fetched for s in stats) > 0:
                client, source = live_client, "live Greenhouse/Lever/Ashby APIs"
            else:
                print("  (live APIs unreachable - falling back to mocks)")
                live_client.close()
        except Exception as exc:
            print(f"  (live harvest failed: {exc} - falling back to mocks)")
            live_client.close()

    stats = harvest(client=client)
    fetched = sum(s.fetched for s in stats)
    accepted = sum(s.accepted for s in stats)
    rejected = {}
    for s in stats:
        for reason, count in s.rejected.items():
            rejected[reason] = rejected.get(reason, 0) + count
    report.add("job boards fetched", fetched > 0, f"{fetched} postings from {len(stats)} boards via {source}")
    report.add("entry-level filter applied", accepted > 0 and bool(rejected),
               f"{accepted} kept; rejected {', '.join(f'{k}={v}' for k, v in sorted(rejected.items()))}")
    with session_scope() as session:
        titles = [j.title for j in session.exec(select(Job)).all()]
    senior = [t for t in titles if re.search(r"\b(senior|staff|lead|principal)\b", t, re.I)]
    report.add("no Senior/Staff/Lead roles stored", not senior, ", ".join(senior) or f"{len(titles)} roles stored")
    again = harvest(client=client)
    report.add("dedupe on (company, job_id)", sum(s.new for s in again) == 0,
               f"second crawl inserted {sum(s.new for s in again)} new rows")

    print("\n2. Match + tailor")
    with session_scope() as session:
        jobs = session.exec(select(Job)).all()
        from .tailor import analyze_job

        for job in jobs:
            analyze_job(session, job)
        best = max(jobs, key=lambda j: j.match_score or 0)
        report.add("ATS match scores computed", all(0 <= (j.match_score or -1) <= 100 for j in jobs),
                   f"{len(jobs)} jobs scored; best: {best.company} - {best.title} ({best.match_score:.0f}%)")
        outcome = tailor_job(session, best)
        result = outcome.compile
        pdf_ok = bool(result and result.ok and result.pdf_path.read_bytes()[:4] == b"%PDF")
        report.add("tailored resume compiled", pdf_ok,
                   f"{result.pdf_path.name} via {result.engine}" if pdf_ok else result.describe_failures())
        tex = result.tex_path.read_text(encoding="utf-8") if result else ""
        top_project = outcome.match.projects[0].name if outcome.match.projects else ""
        report.add("JD-ranked projects + bolded skills in .tex", bool(top_project and top_project in tex and "\\textbf{" in tex),
                   f"top project {top_project}; matched {', '.join(outcome.match.matched[:5])}")
        report.add("job moved to Matched / Tailored", best.status == JobStatus.TAILORED, best.status)
        best_id = best.id

    print("\n3. Prospect")
    with session_scope() as session:
        best = session.get(Job, best_id)
        providers = build_providers(settings, client, ["hunter", "duckduckgo"])
        prospect = prospect_job(session, best, client=client, providers=providers)
        contacts = session.exec(select(ReferralContact).where(ReferralContact.job_id == best_id)
                                .order_by(ReferralContact.priority_score.desc())).all()
        report.add("company domain resolved", bool(prospect.domain), f"{prospect.domain} ({prospect.domain_source})")
        report.add("email pattern discovered", bool(prospect.pattern), f"{prospect.pattern} via {prospect.pattern_source}")
        with_email = [c for c in contacts if c.email]
        report.add("referral contacts found", len(with_email) >= 2,
                   "; ".join(f"{c.name} <{c.email}> {c.email_confidence:.0%}{' alumni' if c.is_alumni else ''}" for c in contacts[:3]))
        contact_ids = [c.id for c in with_email]

    print("\n4. Compose + queue")
    with session_scope() as session:
        drafts = [create_draft(session, session.get(ReferralContact, cid)) for cid in contact_ids[:3]]
        first = drafts[0]
        pitch = first.body.split("\n\n")[1] + " " + first.body.split("\n\n")[2]
        sentences = len(split_sentences(pitch))
        best = session.get(Job, best_id)
        report.add("personalised 3-4 sentence draft", 3 <= sentences <= 4 and best.title in first.body and best.company in first.body,
                   f"{sentences} sentences; subject: {first.subject!r}")
        report.add("opt-out line + resume attachment", "unsubscribe" in first.body and bool(first.attachment_path),
                   Path(first.attachment_path).name if first.attachment_path else "no attachment")
        for draft in drafts:
            approve(session, draft)
        report.add("drafts approved into the queue", all(d.status == OutreachStatus.QUEUED for d in drafts),
                   f"{len(drafts)} queued; job status {best.status}")

    print("\n5. Rate-limited dry-run sending")
    clock = Clock(_morning(settings.tz))
    sender = DryRunSender(settings.outbox_dir)
    queue = OutreachQueue(sender, SendPolicy.from_settings(settings), clock=clock, rng=random.Random(7), settings=settings)
    first_tick = queue.tick()
    second_tick = queue.tick()
    gap = (first_tick.next_send_at - clock.now).total_seconds() if first_tick.next_send_at else 0
    report.add("first email sent", first_tick.status == "sent", f"outreach #{first_tick.outreach_id}")
    report.add("next send waits a random 3-7 minutes", second_tick.status == "waiting" and 180 <= gap <= 420,
               f"tick immediately after -> {second_tick.status}; gap {gap / 60:.1f} min")
    clock.now = first_tick.next_send_at + timedelta(seconds=1)
    third_tick = queue.tick()
    report.add("queue resumes after the gap", third_tick.status == "sent", f"outreach #{third_tick.outreach_id}")

    # Global 24h cap: relax only the per-company cap so 20+ synthetic emails can be attempted.
    relaxed = SendPolicy.from_settings(settings)
    relaxed = SendPolicy(**{**relaxed.__dict__, "per_company_daily_cap": 100})
    burst = OutreachQueue(sender, relaxed, clock=clock, rng=random.Random(3), settings=settings)
    with session_scope() as session:
        job = session.get(Job, best_id)
        for i in range(25):
            contact = ReferralContact(job_id=job.id, name=f"Load Test {i}", email=f"load.test{i}@example.org",
                                      email_confidence=1.0, email_source="manual", status=ContactStatus.DRAFTED)
            session.add(contact)
            session.flush()
            session.add(OutreachLog(contact_id=contact.id, job_id=job.id, to_email=contact.email,
                                    subject="Load test", body="Synthetic message for the rate-limit check.\n",
                                    status=OutreachStatus.QUEUED, queued_at=clock.now))
    statuses = []
    for _ in range(40):
        clock.advance(minutes=7, seconds=1)
        result = burst.tick()
        statuses.append(result.status)
        if result.status == "daily_limit":
            break
    with session_scope() as session:
        sent_window = len([r for r in session.exec(select(OutreachLog).where(OutreachLog.status == OutreachStatus.SENT)).all()
                           if r.sent_at > clock.now - timedelta(hours=24)])
    report.add("hard cap of 20 emails per 24h enforced", statuses[-1] == "daily_limit" and sent_window == 20,
               f"{statuses.count('sent')} more sent, then '{statuses[-1]}' with {sent_window} in the window")
    with session_scope() as session:
        for row in session.exec(select(OutreachLog).where(OutreachLog.subject == "Load test",
                                                          OutreachLog.status == OutreachStatus.QUEUED)).all():
            row.status = OutreachStatus.CANCELLED
            session.add(row)

    print("\n6. Follow-up + opt-out")
    clock.advance(days=4, hours=1)
    created = scan_followups(NullReplyChecker(), clock=clock, settings=settings)
    with session_scope() as session:
        followups = [session.get(OutreachLog, i) for i in created]
        parents = {f.parent_id: session.get(OutreachLog, f.parent_id) for f in followups}
        threaded = all(f.in_reply_to == parents[f.parent_id].message_id and f.subject.startswith("Re:") for f in followups)
        real = [f for f in followups if f.to_email.endswith("@example.org") is False]
        report.add("follow-up queued after 4 days without reply", len(real) >= 2 and threaded,
                   f"{len(real)} follow-ups, threaded via In-Reply-To")
        # A recipient opts out before their follow-up goes out.
        victim = session.get(ReferralContact, real[1].contact_id)
        opt_out(session, victim)
        victim_email = victim.email
    clock.advance(hours=1)
    sent_followup = None
    for _ in range(6):
        result = queue.tick()
        if result.status == "sent":
            sent_followup = result.outreach_id
            break
        if result.next_send_at:
            clock.now = max(clock.now, result.next_send_at) + timedelta(seconds=1)
    with session_scope() as session:
        sent = session.get(OutreachLog, sent_followup) if sent_followup else None
        eml = sorted(settings.outbox_dir.glob("*.eml"))[-1]
        message = message_from_bytes(eml.read_bytes(), policy=default_policy)
        report.add("follow-up sent in the same thread", bool(sent and sent.type == OutreachType.FOLLOWUP
                                                          and message["In-Reply-To"] == sent.in_reply_to),
                   f"{eml.name}: In-Reply-To {message['In-Reply-To']}")
        report.add("List-Unsubscribe header present", bool(message["List-Unsubscribe"]), message["List-Unsubscribe"] or "")
        blocked = session.exec(select(OutreachLog).where(OutreachLog.to_email == victim_email,
                                                         OutreachLog.type == OutreachType.FOLLOWUP)).first()
        report.add("opted-out recipient never emailed again", blocked is not None and blocked.status == OutreachStatus.CANCELLED,
                   f"{victim_email}: follow-up {blocked.status if blocked else 'missing'}")
        job = session.get(Job, best_id)
        report.add("Kanban status advanced", job.status == JobStatus.FOLLOWUP_SENT, job.status)
        report.add("outbox written as .eml files", len(list(settings.outbox_dir.glob("*.eml"))) >= 3,
                   f"{len(list(settings.outbox_dir.glob('*.eml')))} files in {settings.outbox_dir}")
    client.close()
    del Company  # imported for completeness of the model registry


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    return run_verification(live=args.live, keep=args.keep)


if __name__ == "__main__":
    raise SystemExit(main())
