"""Tailor orchestration: score a job against the profile, then build + compile the resume."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlmodel import Session, select

from ..activity import get_logger
from ..config import get_settings
from ..models import CandidateProfile, Job, JobStatus, advance_job_status, utcnow
from ..profile import get_active_profile
from .compiler import CompileResult, compile_resume, resume_basename
from .document import build_resume
from .matcher import MatchResult, score_match

log = get_logger("tailor")


class TailorError(RuntimeError):
    pass


@dataclass
class TailorOutcome:
    job_id: int
    match: MatchResult
    compile: CompileResult | None


def apply_match(job: Job, match: MatchResult) -> None:
    job.match_score = match.score
    job.jd_skills = match.jd.to_dict()
    job.matched_skills = match.matched
    job.missing_skills = match.missing
    job.match_details = match.to_details()
    if job.min_years_required is None:
        job.min_years_required = match.jd.min_years
    job.updated_at = utcnow()


def analyze_job(session: Session, job: Job, profile: CandidateProfile | None = None) -> MatchResult:
    """Score only (cheap); used for every newly discovered job."""
    profile = profile or get_active_profile(session)
    if profile is None:
        raise TailorError("No candidate profile - run `referralpilot init` or create one on the Profile page")
    match = score_match(profile, job.title, job.description, job.min_years_required)
    apply_match(job, match)
    session.add(job)
    return match


def _unique_basename(session: Session, job: Job, exports_dir: Path) -> str:
    base = resume_basename(job.company, job.title)
    target = str(exports_dir / f"{base}.pdf")
    clash = session.exec(select(Job.id).where(Job.resume_pdf_path == target, Job.id != job.id)).first()
    return f"{base}-{job.id}" if clash else base


def tailor_job(
    session: Session,
    job: Job,
    profile: CandidateProfile | None = None,
    *,
    engine: str | None = None,
) -> TailorOutcome:
    """Score the job, render the tailored .tex/.md and compile the PDF into exports/."""
    settings = get_settings()
    profile = profile or get_active_profile(session)
    if profile is None:
        raise TailorError("No candidate profile - run `referralpilot init` or create one on the Profile page")

    match = analyze_job(session, job, profile)
    doc = build_resume(profile, match, company=job.company, role=job.title,
                       max_projects=settings.max_projects_on_resume)
    result = compile_resume(
        doc,
        settings.exports_dir,
        _unique_basename(session, job, settings.exports_dir),
        templates_dir=settings.templates_dir,
        engine=engine or settings.latex_engine,
        timeout=settings.latex_timeout_seconds,
    )
    job.resume_tex_path = str(result.tex_path)
    if result.ok:
        job.resume_pdf_path = str(result.pdf_path)
        job.resume_engine = result.engine
        job.tailored_at = utcnow()
        advance_job_status(job, JobStatus.TAILORED)
        skipped = [name for name, _ in result.attempts]
        note = f" (skipped: {', '.join(skipped)})" if skipped else ""
        log.info("Tailored resume for %s - %s: ATS %.0f%%, compiled with %s in %.1fs%s",
                 job.company, job.title, match.score, result.engine, result.duration, note,
                 extra={"job_id": job.id})
    else:
        log.error("Resume compile failed for %s - %s: %s", job.company, job.title, result.describe_failures(),
                  extra={"job_id": job.id})
    session.add(job)
    return TailorOutcome(job_id=job.id, match=match, compile=result)
