"""Command-line interface: `referralpilot <command>`."""

from __future__ import annotations

import argparse
import sys

from sqlmodel import col, select

from .activity import flush_logging, setup_logging
from .config import get_settings


def _init() -> None:
    from .db import init_db

    setup_logging(get_settings().log_level)
    init_db()


def cmd_init(args) -> int:
    from .db import session_scope
    from .seed import seed_all

    _init()
    with session_scope() as session:
        result = seed_all(session, force_profile=args.force_profile)
    settings = get_settings()
    print(f"Database ready at {settings.sqlalchemy_url}")
    print(f"Profile {'created' if result.profile_created else 'kept'}; companies: "
          f"{result.companies_added} added, {result.companies_updated} updated")
    return 0


def cmd_demo(args) -> int:
    """Load the offline demo data so the dashboard can be explored without network access."""
    from .db import session_scope
    from .demo import DEMO_COMPANIES, demo_client
    from .pipeline import run_harvest
    from .seed import seed_profile, upsert_company

    _init()
    with session_scope() as session:
        seed_profile(session)
        for entry in DEMO_COMPANIES:
            upsert_company(session, {**entry, "enabled": True})
    with session_scope() as session:
        from .models import Company

        ids = [c.id for c in session.exec(select(Company)).all()
               if (c.ats_type, c.board_token) in {(d["ats_type"], d["board_token"]) for d in DEMO_COMPANIES}]
    with demo_client() as client:
        stats = run_harvest(ids, client=client)
    print(f"Demo data loaded: {sum(s.new for s in stats)} new jobs from {len(stats)} mock boards.")
    print("Run `referralpilot serve` and open http://127.0.0.1:8000")
    return 0


def cmd_harvest(args) -> int:
    from .db import session_scope
    from .models import Company
    from .pipeline import run_harvest

    _init()
    ids = None
    if args.company:
        with session_scope() as session:
            wanted = {name.lower() for name in args.company}
            ids = [c.id for c in session.exec(select(Company)).all() if c.name.lower() in wanted]
        if not ids:
            print("No matching companies", file=sys.stderr)
            return 1
    client = None
    if args.offline:
        from .demo import demo_client

        client = demo_client()
    try:
        stats = run_harvest(ids, client=client)
    finally:
        if client is not None:
            client.close()
    for result in stats:
        rejected = ", ".join(f"{k}={v}" for k, v in result.rejected.most_common())
        print(f"{result.company:32} {result.summary()}" + (f"  [rejected: {rejected}]" if rejected else ""))
    return 0


def cmd_jobs(args) -> int:
    from .db import session_scope
    from .models import Job

    _init()
    with session_scope() as session:
        stmt = select(Job).order_by(col(Job.match_score).desc(), col(Job.discovered_at).desc()).limit(args.limit)
        if args.status:
            stmt = stmt.where(Job.status == args.status)
        jobs = session.exec(stmt).all()
        for job in jobs:
            score = f"{job.match_score:5.1f}" if job.match_score is not None else "  -  "
            print(f"#{job.id:<4} {score}  {job.status:13} {job.company[:22]:22} {job.title[:50]:50} {job.location or ''}")
    return 0


def cmd_tailor(args) -> int:
    from .pipeline import tailor

    _init()
    outcome = tailor(args.job_id, engine=args.engine)
    result = outcome.compile
    print(f"ATS match: {outcome.match.score:.1f}%  matched={', '.join(outcome.match.matched[:10])}")
    if outcome.match.missing:
        print(f"Gaps: {', '.join(outcome.match.missing[:10])}")
    if result and result.ok:
        print(f"PDF: {result.pdf_path} (engine: {result.engine})\nTeX: {result.tex_path}")
        return 0
    print(f"Compile failed: {result.describe_failures() if result else 'n/a'}", file=sys.stderr)
    return 1


def cmd_prospect(args) -> int:
    from .pipeline import prospect

    _init()
    client = None
    if args.offline:
        from .demo import demo_client

        client = demo_client()
    try:
        result = prospect(args.job_id, client=client)
    finally:
        if client is not None:
            client.close()
    print(f"Domain: {result.domain} ({result.domain_source}); pattern: {result.pattern or 'unknown'}")
    print(f"Providers: {', '.join(result.providers_used) or 'none'}; added {len(result.added)} contacts")
    for error in result.errors:
        print(f"  ! {error}")
    if not result.providers_used:
        print("Search links:")
        for link in result.links:
            print(f"  {link['label']} [{link['engine']}]: {link['url']}")
    return 0


def cmd_draft(args) -> int:
    from .db import session_scope
    from .models import OutreachLog
    from .pipeline import draft_for_contact

    _init()
    outreach_id = draft_for_contact(args.contact_id, regenerate=args.regenerate)
    with session_scope() as session:
        item = session.get(OutreachLog, outreach_id)
        print(f"Draft #{item.id} to {item.to_email}\nSubject: {item.subject}\n\n{item.body}")
    return 0


def cmd_approve(args) -> int:
    from .db import session_scope
    from .models import OutreachLog
    from .outreach.service import approve

    _init()
    with session_scope() as session:
        item = session.get(OutreachLog, args.outreach_id)
        if item is None:
            print("No such email", file=sys.stderr)
            return 1
        approve(session, item)
    print(f"Email #{args.outreach_id} queued")
    return 0


def cmd_queue(args) -> int:
    from .db import session_scope
    from .runtime import get_queue

    _init()
    with session_scope() as session:
        status = get_queue().status(session)
    print(f"Backend: {status.backend}{' (dry run)' if status.dry_run else ''}")
    print(f"Sent in last 24h: {status.sent_24h}/{status.limit}; queued: {status.queued}; drafts: {status.drafts}")
    print(f"Paused: {status.paused} {status.pause_reason or ''}; window {status.window} (open: {status.in_window})")
    print(f"Next send not before: {status.next_send_at or 'now'}")
    return 0


def cmd_send_tick(args) -> int:
    from .runtime import get_queue

    _init()
    result = get_queue().tick()
    print(f"{result.status}: {result.detail or ''} next={result.next_send_at or '-'}")
    return 0


def cmd_followups(args) -> int:
    from .outreach.followups import scan_followups, scan_replies
    from .runtime import new_reply_checker

    _init()
    checker = new_reply_checker()
    try:
        changed = scan_replies(checker)
        created = scan_followups(checker)
    finally:
        checker.close()
    print(f"Replies detected: {changed}; follow-ups queued: {len(created)}")
    return 0


def cmd_gmail_auth(args) -> int:
    from .outreach.gmail import build_service

    service = build_service(get_settings(), interactive=True)
    profile = service.users().getProfile(userId="me").execute()
    print(f"Authorised Gmail account: {profile.get('emailAddress')} (token cached at {get_settings().gmail_token_file})")
    return 0


def cmd_engines(args) -> int:
    from .tailor import available_engines

    for name, ok in available_engines().items():
        print(f"{name:10} {'available' if ok else 'not installed'}")
    return 0


def cmd_serve(args) -> int:
    import uvicorn

    from .ui.app import create_app

    if args.demo:
        from .demo import enable_demo_env

        enable_demo_env()
        print("Demo mode: job boards and enrichment APIs are served from bundled mock data; sending is dry-run.")
    settings = get_settings()
    app = create_app(start_scheduler=not args.no_scheduler)
    uvicorn.run(app, host=args.host or settings.web_host, port=args.port or settings.web_port, log_level="warning")
    return 0


def cmd_verify(args) -> int:
    from .verify import run_verification

    return run_verification(live=args.live, keep=args.keep)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="referralpilot", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create the database and seed profile + companies")
    p.add_argument("--force-profile", action="store_true", help="overwrite the profile from config/candidate_profile.json")
    p.set_defaults(func=cmd_init)

    sub.add_parser("demo", help="load offline demo jobs (mock Greenhouse/Lever/Ashby/YC)").set_defaults(func=cmd_demo)

    p = sub.add_parser("harvest", help="crawl job boards now")
    p.add_argument("--company", action="append", help="limit to this company (repeatable)")
    p.add_argument("--offline", action="store_true", help="use bundled mock API responses")
    p.set_defaults(func=cmd_harvest)

    p = sub.add_parser("jobs", help="list discovered jobs")
    p.add_argument("--status")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_jobs)

    p = sub.add_parser("tailor", help="score a job and compile its tailored resume")
    p.add_argument("job_id", type=int)
    p.add_argument("--engine", choices=["auto", "pdflatex", "xelatex", "lualatex", "tectonic", "typst", "fpdf"])
    p.set_defaults(func=cmd_tailor)

    p = sub.add_parser("prospect", help="find referral contacts for a job")
    p.add_argument("job_id", type=int)
    p.add_argument("--offline", action="store_true")
    p.set_defaults(func=cmd_prospect)

    p = sub.add_parser("draft", help="compose the referral email for a contact")
    p.add_argument("contact_id", type=int)
    p.add_argument("--regenerate", action="store_true")
    p.set_defaults(func=cmd_draft)

    p = sub.add_parser("approve", help="approve a draft into the send queue")
    p.add_argument("outreach_id", type=int)
    p.set_defaults(func=cmd_approve)

    sub.add_parser("queue", help="show send-queue status").set_defaults(func=cmd_queue)
    sub.add_parser("send-tick", help="send the next queued email if the limits allow").set_defaults(func=cmd_send_tick)
    sub.add_parser("followups", help="check replies and queue due follow-ups").set_defaults(func=cmd_followups)
    sub.add_parser("gmail-auth", help="authorise the Gmail API (opens a browser once)").set_defaults(func=cmd_gmail_auth)
    sub.add_parser("engines", help="list available resume compile engines").set_defaults(func=cmd_engines)

    p = sub.add_parser("serve", help="run the dashboard (and the background scheduler)")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--no-scheduler", action="store_true")
    p.add_argument("--demo", action="store_true", help="offline demo: mock APIs + demo provider keys, dry-run sending")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("verify", help="end-to-end pipeline check in a throwaway workspace")
    p.add_argument("--live", action="store_true", help="hit the real Greenhouse/Lever APIs (falls back to mocks)")
    p.add_argument("--keep", action="store_true", help="keep the temporary workspace for inspection")
    p.set_defaults(func=cmd_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args) or 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # surface a clean message instead of a traceback
        if getattr(args, "command", "") in {"verify", "serve"}:
            raise
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        flush_logging()


if __name__ == "__main__":
    sys.exit(main())
