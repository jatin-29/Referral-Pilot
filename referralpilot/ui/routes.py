"""Dashboard routes (server-rendered HTML + htmx partials + an SSE log stream)."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import ValidationError
from sqlalchemy import func
from sqlmodel import Session, col, select

from ..activity import get_logger, recent_activity
from ..config import get_settings
from ..db import get_session, session_scope
from ..harvester.service import HARVESTERS
from ..models import (
    ActivityLog,
    Company,
    Job,
    JobStatus,
    OutreachLog,
    OutreachStatus,
    ReferralContact,
    utcnow,
)
from ..outreach import service as outreach
from ..outreach.queue import set_paused
from ..pipeline import PipelineError, draft_for_contact, harvest_running, prospect, run_harvest, tailor
from ..profile import ProfileIn, apply_profile, get_active_profile, profile_to_dict
from ..prospector.service import add_manual_contact, update_contact_email
from ..runtime import get_queue
from ..seed import seed_profile, upsert_company
from ..tailor import TailorError, available_engines
from . import views
from .app import templates

router = APIRouter()
log = get_logger("ui")

SessionDep = Annotated[Session, Depends(get_session)]
VALID_JOB_STATUSES = {status.value for status in JobStatus}
ERRORS = (PipelineError, TailorError, outreach.OutreachError, ValueError)


# --- helpers -------------------------------------------------------------------------

def _triggers(toast: str | None = None, kind: str = "success", events: tuple[str, ...] = ()) -> dict[str, str]:
    payload: dict = {name: True for name in events}
    if toast:
        payload["toast"] = {"message": toast, "kind": kind}
    return {"HX-Trigger": json.dumps(payload)} if payload else {}


def render(request: Request, name: str, context: dict | None = None, *, toast: str | None = None,
           kind: str = "success", events: tuple[str, ...] = (), status_code: int = 200) -> HTMLResponse:
    response = templates.TemplateResponse(request, name, context or {}, status_code=status_code)
    response.headers.update(_triggers(toast, kind, events))
    return response


def toast_only(message: str, kind: str = "error", events: tuple[str, ...] = ()) -> Response:
    return Response(status_code=204, headers=_triggers(message, kind, events))


def _job_or_404(session: Session, job_id: int) -> Job:
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return job


def _contact_or_404(session: Session, contact_id: int) -> ReferralContact:
    contact = session.get(ReferralContact, contact_id)
    if contact is None:
        raise HTTPException(404, "Contact not found")
    return contact


def _outreach_or_404(session: Session, outreach_id: int) -> OutreachLog:
    item = session.get(OutreachLog, outreach_id)
    if item is None:
        raise HTTPException(404, "Email not found")
    return item


def drawer(request: Request, job_id: int, **kwargs) -> HTMLResponse:
    with session_scope() as session:
        job = _job_or_404(session, job_id)
        context = views.job_detail(session, job)
        return render(request, "partials/job_drawer.html", context, **kwargs)


def _background(target, *args) -> None:
    threading.Thread(target=target, args=args, daemon=True).start()


def _harvest_in_background(company_ids: list[int] | None) -> None:
    try:
        run_harvest(company_ids)
    except PipelineError as exc:
        log.info("Harvest not started: %s", exc)
    except Exception as exc:
        log.exception("Harvest failed: %s", exc)


# --- pages ---------------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
def board_page(request: Request, session: SessionDep):
    return render(request, "board.html", {"companies": views.company_names(session), "page": "board"})


@router.get("/partials/board", response_class=HTMLResponse)
def board_partial(
    request: Request,
    session: SessionDep,
    q: str | None = None,
    company: str | None = None,
    min_score: Annotated[str | None, Query()] = None,
    archived: bool = False,
):
    score = float(min_score) if min_score not in (None, "") else None
    columns = views.board_columns(session, q=q, company=company or None, min_score=score, show_archived=archived)
    return render(request, "partials/board.html", {"columns": columns})


@router.get("/partials/status", response_class=HTMLResponse)
def status_partial(request: Request, session: SessionDep):
    return render(request, "partials/status.html", {
        "status": get_queue().status(session),
        "harvesting": harvest_running(),
    })


@router.get("/outbox", response_class=HTMLResponse)
def outbox_page(request: Request, session: SessionDep):
    return render(request, "outbox.html", {**views.outbox(session, get_queue()), "page": "outbox"})


@router.get("/partials/outbox", response_class=HTMLResponse)
def outbox_partial(request: Request, session: SessionDep):
    return render(request, "partials/outbox_body.html", views.outbox(session, get_queue()))


@router.get("/companies", response_class=HTMLResponse)
def companies_page(request: Request, session: SessionDep):
    return render(request, "companies.html", _companies_context(session))


def _companies_context(session: Session) -> dict:
    companies = session.exec(select(Company).order_by(col(Company.enabled).desc(), Company.name)).all()
    counts = dict(session.exec(select(Job.company_id, func.count()).group_by(Job.company_id)).all())
    return {"companies": companies, "counts": counts, "ats_types": sorted(HARVESTERS), "page": "companies"}


@router.get("/profile", response_class=HTMLResponse)
def profile_page(request: Request, session: SessionDep):
    profile = get_active_profile(session)
    data = profile_to_dict(profile) if profile else {}
    return render(request, "profile.html", {
        "profile": profile,
        "profile_json": json.dumps(data, indent=2, ensure_ascii=False),
        "engines": available_engines(),
        "page": "profile",
        "errors": [],
    })


@router.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request, session: SessionDep, source: str | None = None):
    rows = recent_activity(session, limit=300, source=source or None)
    sources = session.exec(select(ActivityLog.source).distinct().order_by(ActivityLog.source)).all()
    scheduler = getattr(request.app.state, "scheduler", None)
    from ..scheduler import describe_jobs

    return render(request, "logs.html", {
        "rows": rows, "sources": sources, "source": source, "page": "logs",
        "jobs": describe_jobs(scheduler),
    })


# --- live log stream (Server-Sent Events) ----------------------------------------------

def _fetch_logs(after_id: int, limit: int = 100, source: str | None = None) -> list[dict]:
    with session_scope() as session:
        rows = recent_activity(session, after_id=after_id, limit=limit, source=source)
        return [{"id": r.id, "time": r.created_at.isoformat(), "level": r.level, "source": r.source,
                 "message": r.message, "job_id": r.job_id} for r in rows]


@router.get("/logs/stream")
async def logs_stream(request: Request, after: int = 0, backlog: int = 40, source: str | None = None):
    source = source or None

    async def events():
        last_id = after
        if not last_id and backlog > 0:
            initial = await run_in_threadpool(_fetch_logs, 0, backlog, source)
            for row in initial:
                last_id = row["id"]
                yield f"id: {row['id']}\nevent: log\ndata: {json.dumps(row)}\n\n"
        idle = 0
        while not await request.is_disconnected():
            rows = await run_in_threadpool(_fetch_logs, last_id, 100, source)
            for row in rows:
                last_id = row["id"]
                yield f"id: {row['id']}\nevent: log\ndata: {json.dumps(row)}\n\n"
            idle = 0 if rows else idle + 1
            if idle and idle % 15 == 0:
                yield ": keep-alive\n\n"
            await asyncio.sleep(1.0)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# --- jobs ----------------------------------------------------------------------------

@router.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_drawer(request: Request, job_id: int):
    return drawer(request, job_id)


@router.post("/jobs/{job_id}/status")
def set_job_status(request: Request, job_id: int, session: SessionDep, status: Annotated[str, Form()]):
    if status not in VALID_JOB_STATUSES:
        return toast_only(f"Unknown status {status!r}")
    job = _job_or_404(session, job_id)
    job.status = status
    job.updated_at = utcnow()
    session.add(job)
    session.commit()
    log.info("Moved %s - %s to %s", job.company, job.title, status.replace("_", " "), extra={"job_id": job.id})
    return toast_only(f"Moved to {status.replace('_', ' ')}", "success", events=("refreshBoard",))


@router.post("/jobs/{job_id}/tailor", response_class=HTMLResponse)
def tailor_resume(request: Request, job_id: int):
    try:
        outcome = tailor(job_id)
    except ERRORS as exc:
        return toast_only(str(exc))
    if outcome.compile and outcome.compile.ok:
        message = f"Resume compiled with {outcome.compile.engine} - ATS match {outcome.match.score:.0f}%"
        return drawer(request, job_id, toast=message, events=("refreshBoard",))
    return drawer(request, job_id, toast="Resume compile failed - see the activity log", kind="error")


@router.get("/jobs/{job_id}/resume.pdf")
def resume_pdf(job_id: int, session: SessionDep):
    job = _job_or_404(session, job_id)
    if not job.resume_pdf_path or not Path(job.resume_pdf_path).exists():
        raise HTTPException(404, "No compiled resume yet")
    return FileResponse(job.resume_pdf_path, media_type="application/pdf",
                        content_disposition_type="inline", filename=Path(job.resume_pdf_path).name)


@router.get("/jobs/{job_id}/resume.tex")
def resume_tex(job_id: int, session: SessionDep):
    job = _job_or_404(session, job_id)
    if not job.resume_tex_path or not Path(job.resume_tex_path).exists():
        raise HTTPException(404, "No LaTeX source yet")
    return FileResponse(job.resume_tex_path, media_type="text/plain; charset=utf-8",
                        filename=Path(job.resume_tex_path).name)


@router.post("/jobs/{job_id}/prospect", response_class=HTMLResponse)
def prospect_contacts(request: Request, job_id: int):
    try:
        result = prospect(job_id)
    except ERRORS as exc:
        return toast_only(str(exc))
    if result.added:
        message, kind = f"Found {len(result.added)} contact(s) at {result.domain}", "success"
    elif not result.providers_used:
        message, kind = "No enrichment provider configured - use the search links or add people manually", "info"
    else:
        message, kind = "No new contacts found", "info"
    return drawer(request, job_id, toast=message, kind=kind, events=("refreshBoard",))


@router.post("/jobs/{job_id}/contacts", response_class=HTMLResponse)
def add_contact(
    request: Request,
    job_id: int,
    name: Annotated[str, Form()],
    email: Annotated[str, Form()] = "",
    role: Annotated[str, Form()] = "",
    linkedin_url: Annotated[str, Form()] = "",
):
    if not name.strip():
        return toast_only("Name is required")
    with session_scope() as session:
        job = _job_or_404(session, job_id)
        contact = add_manual_contact(session, job, name=name, email=email or None, role=role or None,
                                     linkedin_url=linkedin_url or None)
        message = f"Added {contact.name}" + (f" ({contact.email})" if contact.email else "")
    return drawer(request, job_id, toast=message, events=("refreshBoard",))


@router.post("/jobs/{job_id}/notes")
def save_notes(job_id: int, session: SessionDep, notes: Annotated[str, Form()] = ""):
    job = _job_or_404(session, job_id)
    job.notes = notes.strip() or None
    session.add(job)
    session.commit()
    return toast_only("Notes saved", "success")


# --- contacts ------------------------------------------------------------------------

@router.post("/contacts/{contact_id}/email", response_class=HTMLResponse)
def change_email(request: Request, contact_id: int, email: Annotated[str, Form()]):
    with session_scope() as session:
        contact = _contact_or_404(session, contact_id)
        job_id = contact.job_id
        try:
            update_contact_email(session, contact, email)
        except ValueError as exc:
            return toast_only(str(exc))
        for item in session.exec(select(OutreachLog).where(
                OutreachLog.contact_id == contact.id, OutreachLog.status == OutreachStatus.DRAFT)).all():
            item.to_email = contact.email
            session.add(item)
    return drawer(request, job_id, toast=f"Email set to {email}")


@router.post("/contacts/{contact_id}/draft", response_class=HTMLResponse)
def draft_email(request: Request, contact_id: int, regenerate: Annotated[bool, Form()] = False):
    try:
        outreach_id = draft_for_contact(contact_id, regenerate=regenerate)
    except ERRORS as exc:
        return toast_only(str(exc))
    return editor(request, outreach_id, events=("refreshDrawer",))


@router.post("/contacts/{contact_id}/{action}", response_class=HTMLResponse)
def contact_action(request: Request, contact_id: int, action: str):
    actions = {
        "replied": (outreach.mark_replied, "Marked as replied"),
        "referred": (outreach.mark_referred, "Referral recorded - congrats!"),
        "opt-out": (outreach.opt_out, "Opted out: pending emails cancelled, address suppressed"),
        "bounced": (outreach.mark_bounced, "Marked as bounced"),
    }
    with session_scope() as session:
        contact = _contact_or_404(session, contact_id)
        job_id = contact.job_id
        if action == "delete":
            sent = session.exec(select(OutreachLog.id).where(
                OutreachLog.contact_id == contact.id, OutreachLog.status == OutreachStatus.SENT)).first()
            if sent:
                return toast_only("This contact has been emailed; mark them opted out instead of deleting")
            for item in session.exec(select(OutreachLog).where(OutreachLog.contact_id == contact.id)).all():
                session.delete(item)
            session.delete(contact)
            message = "Contact removed"
        elif action in actions:
            func, message = actions[action]
            func(session, contact)
        else:
            raise HTTPException(404, "Unknown action")
    return drawer(request, job_id, toast=message, events=("refreshBoard",))


# --- outreach drafts -------------------------------------------------------------------

def editor(request: Request, outreach_id: int, **kwargs) -> HTMLResponse:
    with session_scope() as session:
        item = _outreach_or_404(session, outreach_id)
        context = {
            "item": item,
            "contact": session.get(ReferralContact, item.contact_id),
            "job": session.get(Job, item.job_id),
            "status": get_queue().status(session),
            "attach": get_settings().attach_resume and item.attachment_path and Path(item.attachment_path).exists(),
        }
        return render(request, "partials/editor.html", context, **kwargs)


@router.get("/outreach/{outreach_id}/edit", response_class=HTMLResponse)
def edit_outreach(request: Request, outreach_id: int):
    return editor(request, outreach_id)


@router.post("/outreach/{outreach_id}", response_class=HTMLResponse)
def save_outreach(
    request: Request,
    outreach_id: int,
    subject: Annotated[str, Form()],
    body: Annotated[str, Form()],
    to_email: Annotated[str, Form()],
    approve: Annotated[bool, Form()] = False,
):
    with session_scope() as session:
        item = _outreach_or_404(session, outreach_id)
        try:
            outreach.update_draft(session, item, subject=subject, body=body, to_email=to_email)
            if approve:
                outreach.approve(session, item)
        except outreach.OutreachError as exc:
            return toast_only(str(exc))
    if approve:
        return render(request, "partials/empty.html", toast="Approved - queued for rate-limited sending",
                      events=("refreshDrawer", "refreshBoard", "refreshStatus", "refreshOutbox", "closeModal"))
    return editor(request, outreach_id, toast="Draft saved", events=("refreshDrawer",))


@router.post("/outreach/{outreach_id}/{action}")
def outreach_action(outreach_id: int, action: str):
    with session_scope() as session:
        item = _outreach_or_404(session, outreach_id)
        try:
            if action == "approve":
                outreach.approve(session, item)
                message = "Queued for sending"
            elif action == "cancel":
                outreach.cancel(session, item)
                message = "Email cancelled"
            elif action == "retry":
                item.attempts = 0
                item.scheduled_for = None
                if item.status == OutreachStatus.SKIPPED:
                    item.status = OutreachStatus.DRAFT
                outreach.approve(session, item)
                message = "Re-queued"
            else:
                raise HTTPException(404, "Unknown action")
        except outreach.OutreachError as exc:
            return toast_only(str(exc))
    return toast_only(message, "success",
                      events=("refreshDrawer", "refreshBoard", "refreshStatus", "refreshOutbox"))


# --- sending controls / harvest ---------------------------------------------------------

@router.post("/outbox/{action}")
def outbox_control(action: str, session: SessionDep):
    if action not in {"pause", "resume"}:
        raise HTTPException(404, "Unknown action")
    set_paused(session, action == "pause", "paused from dashboard" if action == "pause" else None)
    session.commit()
    message = "Sending paused" if action == "pause" else "Sending resumed"
    return toast_only(message, "success", events=("refreshStatus", "refreshOutbox"))


@router.post("/harvest")
def harvest_now():
    if harvest_running():
        return toast_only("A harvest is already running", "info")
    _background(_harvest_in_background, None)
    return toast_only("Harvest started - watch the live log", "success", events=("refreshStatus",))


# --- companies -----------------------------------------------------------------------

@router.post("/companies", response_class=HTMLResponse)
def add_company(
    request: Request,
    name: Annotated[str, Form()],
    ats_type: Annotated[str, Form()],
    board_token: Annotated[str, Form()],
    domain: Annotated[str, Form()] = "",
):
    try:
        with session_scope() as session:
            company, created = upsert_company(session, {
                "name": name.strip(), "ats_type": ats_type, "board_token": board_token.strip(),
                "domain": domain.strip() or None, "enabled": True,
            })
            message = f"{'Added' if created else 'Updated'} {company.name}"
    except (ValueError, KeyError) as exc:
        return toast_only(str(exc))
    with session_scope() as session:
        return render(request, "partials/companies_table.html", _companies_context(session), toast=message)


@router.post("/companies/{company_id}/{action}", response_class=HTMLResponse)
def company_action(request: Request, company_id: int, action: str):
    with session_scope() as session:
        company = session.get(Company, company_id)
        if company is None:
            raise HTTPException(404, "Company not found")
        if action == "toggle":
            company.enabled = not company.enabled
            session.add(company)
            message = f"{company.name} {'enabled' if company.enabled else 'disabled'}"
        elif action == "harvest":
            if harvest_running():
                return toast_only("A harvest is already running", "info")
            _background(_harvest_in_background, [company.id])
            message = f"Harvesting {company.name}..."
        elif action == "delete":
            for job in session.exec(select(Job).where(Job.company_id == company.id)).all():
                job.company_id = None
                session.add(job)
            session.delete(company)
            message = "Company removed (its jobs are kept)"
        else:
            raise HTTPException(404, "Unknown action")
    with session_scope() as session:
        return render(request, "partials/companies_table.html", _companies_context(session), toast=message)


# --- profile -------------------------------------------------------------------------

@router.post("/profile", response_class=HTMLResponse)
def save_profile(request: Request, profile_json: Annotated[str, Form()]):
    errors: list[str] = []
    try:
        data = ProfileIn.model_validate(json.loads(profile_json))
    except json.JSONDecodeError as exc:
        errors = [f"Invalid JSON: {exc}"]
    except ValidationError as exc:
        errors = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]
    if errors:
        return render(request, "partials/profile_form.html",
                      {"profile_json": profile_json, "errors": errors},
                      toast="Profile not saved - fix the errors", kind="error")
    with session_scope() as session:
        profile = get_active_profile(session)
        if profile is None:
            seed_profile(session)
            profile = get_active_profile(session)
        apply_profile(profile, data)
        session.add(profile)
        pretty = json.dumps(profile_to_dict(profile), indent=2, ensure_ascii=False)
    log.info("Candidate profile updated from the dashboard")
    return render(request, "partials/profile_form.html", {"profile_json": pretty, "errors": []},
                  toast="Profile saved - re-tailor resumes to apply it")


@router.post("/profile/reload", response_class=HTMLResponse)
def reload_profile(request: Request):
    with session_scope() as session:
        profile, _ = seed_profile(session, force=True)
        pretty = json.dumps(profile_to_dict(profile), indent=2, ensure_ascii=False)
    return render(request, "partials/profile_form.html", {"profile_json": pretty, "errors": []},
                  toast="Reloaded config/candidate_profile.json")


# --- JSON API --------------------------------------------------------------------------

@router.get("/api/stats")
def api_stats(session: SessionDep):
    status = get_queue().status(session)
    return JSONResponse({
        "pipeline": views.pipeline_counts(session),
        "queue": {
            "backend": status.backend, "dry_run": status.dry_run, "sent_24h": status.sent_24h,
            "limit": status.limit, "queued": status.queued, "drafts": status.drafts, "paused": status.paused,
            "in_window": status.in_window,
            "next_send_at": status.next_send_at.isoformat() if status.next_send_at else None,
        },
        "harvesting": harvest_running(),
    })


@router.get("/healthz")
def healthz():
    return {"ok": True}
