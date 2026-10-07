"""Engine-independent resume content: what goes on the page, in which order, bolded where."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..models import CandidateProfile
from ..textutil import human_join
from .matcher import MatchResult
from .skills import find_mentions, normalize_skill


@dataclass
class Span:
    text: str
    bold: bool = False


RichText = list[Span]


@dataclass
class ResumeDocument:
    name: str
    contact: list[tuple[str, str | None]]  # (display text, url)
    summary: RichText
    education: list[dict]
    skills: list[tuple[str, list[tuple[str, bool]]]]
    experience: list[dict]
    projects: list[dict]
    achievements: list[RichText]
    sections: list[str]
    target_company: str = ""
    target_role: str = ""
    ats_score: float | None = None
    keywords: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        """Plain JSON (used by the Typst template)."""

        def rich(text: RichText) -> list[dict]:
            return [{"text": s.text, "bold": s.bold} for s in text]

        return {
            "name": self.name,
            "contact": [{"text": t, "url": u} for t, u in self.contact],
            "summary": rich(self.summary),
            "education": [{**e, "coursework": [{"text": c, "bold": b} for c, b in e["coursework"]]} for e in self.education],
            "skills": [{"category": c, "items": [{"text": t, "bold": b} for t, b in items]} for c, items in self.skills],
            "experience": [{**x, "bullets": [rich(b) for b in x["bullets"]]} for x in self.experience],
            "projects": [
                {**p, "tech": [{"text": t, "bold": b} for t, b in p["tech"]], "bullets": [rich(b) for b in p["bullets"]]}
                for p in self.projects
            ],
            "achievements": [rich(a) for a in self.achievements],
            "sections": self.sections,
            "target": {"company": self.target_company, "role": self.target_role, "score": self.ats_score},
        }


def emphasize(text: str, skills: set[str]) -> RichText:
    """Split `text` into spans, bolding mentions of any skill in `skills`."""
    spans: RichText = []
    cursor = 0
    for mention in find_mentions(text):
        if mention.skill not in skills:
            continue
        if mention.start > cursor:
            spans.append(Span(text[cursor:mention.start]))
        spans.append(Span(text[mention.start:mention.end], True))
        cursor = mention.end
    if cursor < len(text):
        spans.append(Span(text[cursor:]))
    return spans or [Span(text)]


def _relevance(text: str, weights: dict[str, float]) -> float:
    return sum(weights.get(m.skill, 0.0) for m in find_mentions(text))


def _item_weight(raw: str, weights: dict[str, float]) -> float:
    return max((weights.get(skill, 0.0) for skill in normalize_skill(raw)), default=0.0)


def _order_items(items: list[str], weights: dict[str, float], matched: set[str]) -> list[tuple[str, bool]]:
    indexed = list(enumerate(items))
    indexed.sort(key=lambda pair: (-_item_weight(pair[1], weights), pair[0]))
    return [(item, bool(normalize_skill(item) & matched)) for _, item in indexed]


def _order_bullets(bullets: list[str], weights: dict[str, float], matched: set[str]) -> list[RichText]:
    indexed = list(enumerate(bullets))
    indexed.sort(key=lambda pair: (-_relevance(pair[1], weights), pair[0]))
    return [emphasize(bullet, matched) for _, bullet in indexed]


def _dates(start: str | None, end: str | None) -> str:
    if start and end:
        return f"{start} – {end}"
    return start or end or ""


def _display_url(url: str) -> str:
    return url.split("://", 1)[-1].removeprefix("www.").rstrip("/")


def _summary(profile: CandidateProfile, match: MatchResult, top_projects: list[str]) -> RichText:
    headline = (profile.headline or "").strip()
    matched = match.matched[:4]
    if not matched:
        return [Span(profile.summary)] if profile.summary else []
    spans: RichText = []
    lead = headline[:1].upper() + headline[1:] if headline else "Software engineer"
    spans.append(Span(f"{lead} with hands-on experience in "))
    for i, skill in enumerate(matched):
        if i:
            spans.append(Span(" and " if i == len(matched) - 1 else ", "))
        spans.append(Span(skill, True))
    if top_projects:
        spans.append(Span(f", demonstrated through projects such as {human_join(top_projects[:2])}."))
    else:
        spans.append(Span("."))
    return spans


def build_resume(
    profile: CandidateProfile,
    match: MatchResult,
    *,
    company: str,
    role: str,
    max_projects: int = 3,
) -> ResumeDocument:
    weights = match.jd.skill_weights
    matched = set(match.matched)

    contact: list[tuple[str, str | None]] = []
    if profile.phone:
        contact.append((profile.phone, f"tel:{profile.phone.replace(' ', '')}"))
    contact.append((profile.email, f"mailto:{profile.email}"))
    for url in (profile.linkedin_url, profile.github_url, profile.portfolio_url):
        if url:
            contact.append((_display_url(url), url))
    if profile.location:
        contact.append((profile.location, None))

    education = [
        {
            "institution": edu.get("institution", ""),
            "degree": edu.get("degree", ""),
            "location": edu.get("location") or "",
            "dates": _dates(edu.get("start"), edu.get("end")),
            "score": edu.get("score") or "",
            "coursework": _order_items(list(edu.get("coursework") or []), weights, matched),
        }
        for edu in profile.education or []
    ]

    skills = [
        (category, _order_items(list(items), weights, matched))
        for category, items in (profile.skills or {}).items()
        if items
    ]

    experience = [
        {
            "company": job.get("company", ""),
            "role": job.get("role", ""),
            "location": job.get("location") or "",
            "dates": _dates(job.get("start"), job.get("end")),
            "bullets": _order_bullets(list(job.get("bullets") or []), weights, matched),
        }
        for job in profile.experience or []
    ]

    projects = []
    for rank in match.projects[:max_projects]:
        project = profile.projects[rank.index]
        link = project.get("link") or ""
        projects.append(
            {
                "name": project.get("name", ""),
                "link": link,
                "link_label": _display_url(link) if link else "",
                "tech": _order_items(list(project.get("tech") or []), weights, matched),
                "bullets": _order_bullets(list(project.get("bullets") or []), weights, matched),
                "relevance": rank.score,
            }
        )

    achievements = [emphasize(item, matched) for item in profile.achievements or []]
    summary = _summary(profile, match, [p["name"] for p in projects])

    sections = [name for name, present in (
        ("summary", bool(summary)),
        ("education", bool(education)),
        ("skills", bool(skills)),
        ("experience", bool(experience)),
        ("projects", bool(projects)),
        ("achievements", bool(achievements)),
    ) if present]

    return ResumeDocument(
        name=profile.full_name,
        contact=contact,
        summary=summary,
        education=education,
        skills=skills,
        experience=experience,
        projects=projects,
        achievements=achievements,
        sections=sections,
        target_company=company,
        target_role=role,
        ats_score=match.score,
        keywords=match.matched[:12],
    )
