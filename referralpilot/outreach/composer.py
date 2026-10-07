"""Personalised referral-request emails (3-4 sentences) and one-sentence follow-ups."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from ..models import CandidateProfile, Job, OutreachLog, ReferralContact
from ..profile import first_name, primary_education
from ..textutil import human_join

OPT_OUT_LINE = (
    "P.S. If you'd rather not get emails like this, just reply \"unsubscribe\" "
    "and I won't contact you again."
)

# Concepts read better in a sentence than as bare skill names.
_PHRASE = {
    "Data Structures & Algorithms": "strong DSA fundamentals",
    "REST APIs": "REST API design",
    "System Design": "system design",
    "Distributed Systems": "distributed systems",
    "Testing": "automated testing",
    "Caching": "caching",
    "Concurrency": "concurrency",
    "Message Queues": "message queues",
    "Computer Networks": "networking fundamentals",
    "Operating Systems": "OS fundamentals",
    "OOP": "object-oriented design",
}


@dataclass
class EmailDraft:
    subject: str
    body: str
    attachment_path: str | None = None


def _variant(seed: str, options: int) -> int:
    """Stable per-recipient choice so emails are varied but reproducible."""
    return int(hashlib.sha1(seed.encode("utf-8")).hexdigest(), 16) % options


def _skills_phrase(job: Job) -> str:
    required = (job.jd_skills or {}).get("required") or []
    matched = list(job.matched_skills or [])
    ordered = [s for s in required if s in matched] + [s for s in matched if s not in required]
    phrases = [_PHRASE.get(skill, skill) for skill in ordered[:3]]
    return human_join(phrases) if phrases else "strong engineering fundamentals"


def _top_projects(profile: CandidateProfile, job: Job, count: int = 2) -> list[dict]:
    ranked = (job.match_details or {}).get("projects") or []
    indices = [p["index"] for p in ranked if p.get("score", 0) > 0][:count]
    if not indices:
        indices = list(range(min(count, len(profile.projects or []))))
    return [profile.projects[i] for i in indices if i < len(profile.projects or [])]


def _project_clause(project: dict) -> str:
    summary = (project.get("summary") or "").strip().rstrip(".")
    return f"{project['name']}, {summary}" if summary else project["name"]


def _signature(profile: CandidateProfile) -> str:
    links = [url for url in (profile.linkedin_url, profile.github_url, profile.portfolio_url) if url]
    lines = [profile.full_name]
    if links:
        lines.append(" | ".join(links))
    if profile.phone:
        lines.append(profile.phone)
    return "\n".join(lines)


def _intro(profile: CandidateProfile, contact: ReferralContact) -> str:
    me = first_name(profile.full_name)
    education = primary_education(profile)
    school = education.get("short_name") or education.get("institution")
    headline = (profile.headline or "software engineer").strip().rstrip(".")
    if contact.is_alumni and school:
        year = f" ('{str(profile.graduation_year)[-2:]})" if profile.graduation_year else ""
        return f"I'm {me}, a fellow {school} alum{year} and {headline}"
    article = "an" if headline[:1].lower() in "aeiou" else "a"
    return f"I'm {me}, {article} {headline}"


def compose_initial(
    profile: CandidateProfile,
    job: Job,
    contact: ReferralContact,
    *,
    attach_resume: bool = True,
) -> EmailDraft:
    seed = contact.email or contact.name
    greeting_name = first_name(contact.name) or "there"
    projects = _top_projects(profile, job)
    skills = _skills_phrase(job)

    s1_variants = [
        f"{_intro(profile, contact)}, and I came across the {job.title} opening at {job.company}.",
        f"{_intro(profile, contact)} - I just saw that {job.company} is hiring for a {job.title} role.",
    ]
    s2_variants = [
        f"The role calls for {skills}, which is exactly what I've been building with.",
        f"It asks for {skills}, which lines up closely with what I've been working on.",
    ]
    if len(projects) >= 2:
        s3 = f"Most recently I built {_project_clause(projects[0])}, and {_project_clause(projects[1])}."
    elif projects:
        s3 = f"Most recently I built {_project_clause(projects[0])}."
    else:
        s3 = ""
    has_resume = bool(attach_resume and job.resume_pdf_path)
    resume_note = " (attached)" if has_resume else ""
    s4_variants = [
        f"If you think I'd be a good fit, would you be open to referring me or forwarding my resume{resume_note} to the hiring team?",
        f"Would you be comfortable referring me for the role, or passing my resume{resume_note} along to the hiring team?",
    ]

    v = _variant(seed, 2)
    paragraph = " ".join(part for part in (s1_variants[v], s2_variants[_variant(seed + "s2", 2)], s3) if part)
    body = "\n\n".join([
        f"Hi {greeting_name},",
        paragraph,
        s4_variants[_variant(seed + "s4", 2)],
        f"Job posting: {job.url}",
        f"Thanks so much for your time,\n{_signature(profile)}",
        OPT_OUT_LINE,
    ])

    school = primary_education(profile).get("short_name")
    if contact.is_alumni and school:
        subject = f"Fellow {school} alum - {job.title} at {job.company}"
    else:
        subject = [
            f"Referral request: {job.title} at {job.company}",
            f"{job.title} at {job.company} - would you refer me?",
        ][_variant(seed + "subject", 2)]
    return EmailDraft(subject=subject, body=body, attachment_path=job.resume_pdf_path if has_resume else None)


def followup_subject(original_subject: str) -> str:
    return original_subject if re.match(r"^\s*re:", original_subject, re.IGNORECASE) else f"Re: {original_subject}"


def compose_followup(
    profile: CandidateProfile,
    job: Job,
    contact: ReferralContact,
    original: OutreachLog,
) -> EmailDraft:
    greeting_name = first_name(contact.name) or "there"
    sentence = (
        f"Just bumping this in case it got buried - I'd be really grateful for a referral for the "
        f"{job.title} role at {job.company} if you think I'm a fit, and no worries at all if not."
    )
    sent = original.sent_at.strftime("%a, %d %b %Y") if original.sent_at else "earlier"
    quoted = "\n".join(f"> {line}" if line else ">" for line in original.body.splitlines())
    body = "\n\n".join([
        f"Hi {greeting_name},",
        sentence,
        f"Thanks,\n{first_name(profile.full_name)}",
        OPT_OUT_LINE,
        f"On {sent}, {profile.full_name} wrote:\n{quoted}",
    ])
    return EmailDraft(subject=followup_subject(original.subject), body=body)
