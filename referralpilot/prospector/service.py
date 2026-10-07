"""Target discovery: resolve the employer domain, find people, guess and rank emails."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import httpx
from sqlmodel import Session, select

from ..activity import get_logger
from ..config import get_settings
from ..db import state_get, state_set
from ..fetch import FetchError, build_client
from ..models import CandidateProfile, Company, ContactStatus, Job, ReferralContact, Suppression, utcnow
from ..profile import get_active_profile
from .domain import has_mx, resolve_domain
from .patterns import generate_candidates, infer_pattern, is_valid_email
from .providers import (
    ContactCandidate,
    ContactProvider,
    ProspectContext,
    ProviderError,
    build_providers,
    normalize_linkedin,
    search_links,
)

log = get_logger("prospector")

_ENGINEER = re.compile(r"software|\bsde\b|\bswe\b|developer|engineer|back[\s-]?end|front[\s-]?end|full[\s-]?stack", re.I)
_RECRUITER = re.compile(r"recruit|talent|hiring|\bhr\b|people partner", re.I)
_PEER = re.compile(r"\bsde[\s-]?(?:1|i)\b|\b(?:engineer|developer)\s*(?:i|1)\b|associate|junior|new grad|graduate", re.I)
# Pattern guesses on a domain that was itself guessed deserve much less trust.
GUESSED_DOMAIN_PENALTY = {"guess": 0.4, "dns_guess": 0.75}

_EXECUTIVE = re.compile(r"director|head of|\bvp\b|vice president|chief|\bcto\b|\bceo\b|founder|principal|staff", re.I)


@dataclass
class ProspectResult:
    job_id: int
    domain: str | None
    domain_source: str
    pattern: str | None = None
    pattern_source: str | None = None
    added: list[int] = field(default_factory=list)
    skipped_existing: int = 0
    providers_used: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    links: list[dict] = field(default_factory=list)
    mx_ok: bool | None = None


def institutions_for(profile: CandidateProfile | None) -> list[str]:
    names: list[str] = []
    for edu in (profile.education if profile else None) or []:
        for value in (edu.get("institution"), edu.get("short_name")):
            if value and value not in names:
                names.append(value)
    return names


def priority_score(candidate: ContactCandidate) -> float:
    role = candidate.role or ""
    score = 0.0
    if candidate.is_alumni:
        score += 3.0
    if _ENGINEER.search(role) and not _EXECUTIVE.search(role) and "manager" not in role.lower():
        score += 2.0
    if _RECRUITER.search(role):
        score += 1.5
    if _PEER.search(role):
        score += 1.0
    if re.search(r"manager|lead", role, re.I) and not _EXECUTIVE.search(role):
        score += 0.5
    if _EXECUTIVE.search(role):
        score -= 1.0
    score += candidate.email_confidence
    if candidate.email_source in {"hunter", "apollo", "manual"}:
        score += 0.5
    return round(score, 2)


def _key(candidate: ContactCandidate) -> str:
    return (
        normalize_linkedin(candidate.linkedin_url)
        or (candidate.email or "").lower()
        or re.sub(r"\s+", " ", candidate.name.lower()).strip()
    )


def merge_candidates(candidates: list[ContactCandidate]) -> list[ContactCandidate]:
    merged: dict[str, ContactCandidate] = {}
    by_name: dict[str, str] = {}
    for candidate in candidates:
        name_key = re.sub(r"\s+", " ", candidate.name.lower()).strip()
        key = by_name.get(name_key) or _key(candidate)
        existing = merged.get(key)
        if existing is None:
            merged[key] = candidate
            by_name[name_key] = key
            continue
        if candidate.email and candidate.email_confidence > existing.email_confidence:
            existing.email, existing.email_confidence = candidate.email, candidate.email_confidence
            existing.email_source = candidate.email_source
        existing.role = existing.role or candidate.role
        existing.linkedin_url = existing.linkedin_url or candidate.linkedin_url
        existing.is_alumni = existing.is_alumni or candidate.is_alumni
    return list(merged.values())


def _domain_for(session: Session, job: Job) -> tuple[str | None, str]:
    company = session.get(Company, job.company_id) if job.company_id else None
    configured = (company.domain if company else None) or job.domain
    return resolve_domain(job.company, configured=configured, posting_url=job.url,
                          use_dns=get_settings().verify_mx)


def _suppressed(session: Session, email: str | None) -> bool:
    return bool(email) and session.get(Suppression, email.lower()) is not None


def fill_email_guess(candidate: ContactCandidate, domain: str | None, pattern: str | None) -> None:
    if candidate.email or not domain:
        return
    guesses = generate_candidates(candidate.name, domain, known_pattern=pattern)
    if guesses:
        candidate.email = guesses[0].email
        candidate.email_confidence = guesses[0].confidence
        candidate.email_source = "pattern"
        candidate.alternate_emails = [g.email for g in guesses[1:5]]


def prospect_job(
    session: Session,
    job: Job,
    *,
    client: httpx.Client | None = None,
    providers: list[ContactProvider] | None = None,
    limit: int | None = None,
) -> ProspectResult:
    settings = get_settings()
    limit = limit or settings.max_contacts_per_job
    profile = get_active_profile(session)
    domain, domain_source = _domain_for(session, job)
    if domain and (job.domain != domain or job.domain_source != domain_source):
        job.domain, job.domain_source = domain, domain_source
        session.add(job)
    if domain_source in GUESSED_DOMAIN_PENALTY:
        log.warning("Email domain for %s is a guess (%s) - verify it before approving emails", job.company, domain,
                    extra={"job_id": job.id})

    ctx = ProspectContext(company=job.company, domain=domain, role_title=job.title,
                          institutions=institutions_for(profile), limit=limit)
    result = ProspectResult(job_id=job.id, domain=domain, domain_source=domain_source, links=search_links(ctx))

    pattern_key = f"email_pattern:{domain}" if domain else None
    if pattern_key:
        cached = state_get(session, pattern_key)
        if cached:
            result.pattern, _, result.pattern_source = cached.partition("|")

    own_client = client is None
    client = client or build_client(settings)
    candidates: list[ContactCandidate] = []
    try:
        for provider in providers if providers is not None else build_providers(settings, client):
            if not provider.available():
                result.errors.append(f"{provider.name}: API key not configured")
                continue
            try:
                if domain and not result.pattern:
                    pattern = provider.email_pattern(domain)
                    if pattern:
                        result.pattern, result.pattern_source = pattern, provider.name
                found = provider.find_contacts(ctx)
            except (ProviderError, FetchError, httpx.HTTPError, ValueError) as exc:
                result.errors.append(str(exc))
                log.warning("%s lookup for %s failed: %s", provider.name, job.company, exc, extra={"job_id": job.id})
                continue
            result.providers_used.append(provider.name)
            candidates.extend(found)
    finally:
        if own_client:
            client.close()

    if not result.pattern:
        examples = [(c.name, c.email) for c in candidates if c.email and c.email_source in {"hunter", "apollo"}]
        pattern, support = infer_pattern(examples)
        if pattern and support >= 0.6:
            result.pattern, result.pattern_source = pattern, "inferred"
    if pattern_key and result.pattern:
        state_set(session, pattern_key, f"{result.pattern}|{result.pattern_source}")

    if domain and settings.verify_mx:
        result.mx_ok = has_mx(domain)
        if result.mx_ok is False:
            log.warning("%s has no MX records - guessed addresses will bounce", domain, extra={"job_id": job.id})

    merged = merge_candidates(candidates)
    for candidate in merged:
        fill_email_guess(candidate, domain, result.pattern)
        if candidate.email_source == "pattern":
            factor = GUESSED_DOMAIN_PENALTY.get(domain_source, 1.0) * (0.3 if result.mx_ok is False else 1.0)
            candidate.email_confidence = round(candidate.email_confidence * factor, 3)
    merged.sort(key=lambda c: -priority_score(c))

    existing = session.exec(select(ReferralContact).where(ReferralContact.job_id == job.id)).all()
    seen = {(c.email or "").lower() for c in existing if c.email} | {c.linkedin_url for c in existing if c.linkedin_url}
    for candidate in merged:
        if len(result.added) >= limit:
            break
        email_key = (candidate.email or "").lower()
        if (email_key and email_key in seen) or (candidate.linkedin_url and candidate.linkedin_url in seen):
            result.skipped_existing += 1
            continue
        contact = save_candidate(session, job, candidate)
        result.added.append(contact.id)
        seen.update({email_key, candidate.linkedin_url})

    log.info(
        "Prospected %s (%s via %s): %d new contacts from %s%s",
        job.company, domain or "unknown domain", domain_source, len(result.added),
        ", ".join(result.providers_used) or "no providers",
        f", pattern {result.pattern} ({result.pattern_source})" if result.pattern else "",
        extra={"job_id": job.id},
    )
    return result


def save_candidate(session: Session, job: Job, candidate: ContactCandidate) -> ReferralContact:
    contact = ReferralContact(
        job_id=job.id,
        name=candidate.name,
        role=candidate.role,
        email=candidate.email.lower() if candidate.email else None,
        email_confidence=candidate.email_confidence,
        email_source=candidate.email_source,
        alternate_emails=candidate.alternate_emails,
        linkedin_url=candidate.linkedin_url,
        source=candidate.source,
        is_alumni=candidate.is_alumni,
        priority_score=priority_score(candidate),
        status=ContactStatus.OPTED_OUT if _suppressed(session, candidate.email) else ContactStatus.NEW,
        notes=candidate.snippet or None,
    )
    session.add(contact)
    session.flush()
    return contact


def add_manual_contact(
    session: Session,
    job: Job,
    *,
    name: str,
    email: str | None = None,
    role: str | None = None,
    linkedin_url: str | None = None,
) -> ReferralContact:
    """Add a person found by hand; the email is guessed from the domain pattern if omitted."""
    profile = get_active_profile(session)
    institutions = [i.lower() for i in institutions_for(profile)]
    candidate = ContactCandidate(
        name=name.strip(),
        role=(role or "").strip() or None,
        email=email.strip().lower() if is_valid_email(email) else None,
        email_confidence=1.0 if is_valid_email(email) else 0.0,
        email_source="manual" if is_valid_email(email) else None,
        linkedin_url=normalize_linkedin(linkedin_url) or (linkedin_url or None),
        source="manual",
        is_alumni=any(i in (role or "").lower() for i in institutions),
    )
    if not candidate.email:
        domain, domain_source = _domain_for(session, job)
        cached = state_get(session, f"email_pattern:{domain}") if domain else None
        fill_email_guess(candidate, domain, cached.partition("|")[0] if cached else None)
        if candidate.email:
            factor = GUESSED_DOMAIN_PENALTY.get(domain_source, 1.0)
            candidate.email_confidence = round(candidate.email_confidence * factor, 3)
    contact = save_candidate(session, job, candidate)
    log.info("Added contact %s for %s (%s)", contact.name, job.company, contact.email or "no email",
             extra={"job_id": job.id})
    return contact


def update_contact_email(session: Session, contact: ReferralContact, email: str) -> ReferralContact:
    if not is_valid_email(email):
        raise ValueError(f"Invalid email address: {email!r}")
    contact.email = email.strip().lower()
    contact.email_source = "manual"
    contact.email_confidence = 1.0
    contact.updated_at = utcnow()
    if _suppressed(session, contact.email):
        contact.status = ContactStatus.OPTED_OUT
    session.add(contact)
    return contact
