"""ATS match score (0-100) and project ranking against a parsed job description.

score = 100 * (0.60 * coverage      weighted share of JD requirement units the candidate satisfies
             + 0.15 * evidence      share of satisfied weight backed by projects/experience (not just listed)
             + 0.15 * title_fit     role family (backend/frontend/...) vs. the candidate's target roles
             + 0.10 * experience)   penalty when the JD asks for more years than a fresher has
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..models import CandidateProfile
from .jd_parser import ParsedJD, parse_job_description, role_family
from .skills import extract_skills, normalize_skill

W_COVERAGE, W_EVIDENCE, W_TITLE, W_EXPERIENCE = 0.60, 0.15, 0.15, 0.10


@dataclass
class CandidateSkills:
    listed: set[str] = field(default_factory=set)
    evidence: set[str] = field(default_factory=set)

    @property
    def all(self) -> set[str]:
        return self.listed | self.evidence

    def has(self, skill: str) -> bool:
        return skill in self.listed or skill in self.evidence


@dataclass
class ProjectRank:
    index: int
    name: str
    score: float
    matched: list[str]


@dataclass
class MatchResult:
    score: float
    breakdown: dict[str, float]
    matched: list[str]
    missing: list[str]
    jd: ParsedJD
    candidate: CandidateSkills
    projects: list[ProjectRank]
    role_family: str

    def to_details(self) -> dict:
        return {
            "score": self.score,
            "breakdown": self.breakdown,
            "role_family": self.role_family,
            "projects": [
                {"index": p.index, "name": p.name, "score": p.score, "matched": p.matched} for p in self.projects
            ],
        }


def _normalize_all(values) -> set[str]:
    out: set[str] = set()
    for value in values or []:
        out |= normalize_skill(str(value))
    return out


def project_skills(project: dict) -> set[str]:
    text = " ".join([project.get("summary") or "", *(project.get("bullets") or [])])
    return _normalize_all(project.get("tech")) | extract_skills(text)


def candidate_skillset(profile: CandidateProfile) -> CandidateSkills:
    listed: set[str] = set()
    for items in (profile.skills or {}).values():
        listed |= _normalize_all(items)
    evidence: set[str] = set()
    for project in profile.projects or []:
        evidence |= project_skills(project)
    for job in profile.experience or []:
        evidence |= extract_skills(" ".join(job.get("bullets") or []))
    for edu in profile.education or []:
        listed |= _normalize_all(edu.get("coursework"))
    return CandidateSkills(listed=listed, evidence=evidence)


def title_fit(job_title: str, target_roles: list[str]) -> float:
    family = role_family(job_title)
    targets = {role.lower() for role in target_roles or []}
    if not targets:
        return 0.8
    if family in targets:
        return 1.0
    if family == "general":
        return 0.85
    if family == "fullstack" and targets & {"backend", "frontend"}:
        return 0.85
    if family in {"backend", "frontend"} and "fullstack" in targets:
        return 0.8
    return 0.55


def experience_fit(min_years: int | None) -> float:
    if min_years is None or min_years <= 0:
        return 1.0
    return {1: 0.9, 2: 0.7}.get(min_years, 0.3)


def rank_projects(profile: CandidateProfile, jd: ParsedJD) -> list[ProjectRank]:
    ranks: list[ProjectRank] = []
    for index, project in enumerate(profile.projects or []):
        skills = project_skills(project)
        matched = [skill for skill in jd.skills if skill in skills]
        score = sum(jd.skill_weights[skill] for skill in matched)
        relevant = set(jd.skill_weights)
        score += 0.25 * sum(1 for bullet in project.get("bullets") or [] if extract_skills(bullet) & relevant)
        ranks.append(ProjectRank(index, project.get("name", f"Project {index + 1}"), round(score, 2), matched))
    ranks.sort(key=lambda rank: (-rank.score, rank.index))
    return ranks


def score_match(profile: CandidateProfile, job_title: str, description: str, min_years: int | None = None) -> MatchResult:
    jd = parse_job_description(description, job_title)
    if min_years is None:
        min_years = jd.min_years
    candidate = candidate_skillset(profile)

    total = sum(unit.weight for unit in jd.units)
    satisfied = [unit for unit in jd.units if any(candidate.has(s) for s in unit.skills)]
    satisfied_weight = sum(unit.weight for unit in satisfied)
    if total:
        coverage = satisfied_weight / total
        backed = sum(u.weight for u in satisfied if any(s in candidate.evidence for s in u.skills))
        evidence = backed / satisfied_weight if satisfied_weight else 0.0
    else:  # nothing recognisable in the JD: stay neutral
        coverage = evidence = 0.5

    fit_title = title_fit(job_title, profile.target_roles)
    fit_years = experience_fit(min_years)
    score = 100 * (W_COVERAGE * coverage + W_EVIDENCE * evidence + W_TITLE * fit_title + W_EXPERIENCE * fit_years)

    matched = [skill for skill in jd.skills if candidate.has(skill)]
    missing: list[str] = []
    for unit in sorted(jd.units, key=lambda u: -u.weight):
        if unit in satisfied:
            continue
        for skill in unit.skills:
            if skill not in missing:
                missing.append(skill)

    return MatchResult(
        score=round(max(0.0, min(100.0, score)), 1),
        breakdown={
            "coverage": round(coverage, 3),
            "evidence": round(evidence, 3),
            "title_fit": round(fit_title, 3),
            "experience_fit": round(fit_years, 3),
        },
        matched=matched,
        missing=missing,
        jd=jd,
        candidate=candidate,
        projects=rank_projects(profile, jd),
        role_family=role_family(job_title),
    )
