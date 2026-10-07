"""Candidate profile schema (validation for seed files and the profile editor)."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, field_validator
from sqlmodel import Session, col, select

from .models import CandidateProfile, utcnow


class ProjectIn(BaseModel):
    name: str
    summary: str = ""  # one-line "what it does / impact", reused in outreach emails
    tech: list[str] = Field(default_factory=list)
    link: Optional[str] = None
    bullets: list[str] = Field(default_factory=list)


class ExperienceIn(BaseModel):
    company: str
    role: str
    location: Optional[str] = None
    start: Optional[str] = None
    end: Optional[str] = None
    bullets: list[str] = Field(default_factory=list)


class EducationIn(BaseModel):
    institution: str
    short_name: Optional[str] = None  # e.g. "DTU" -- used for alumni search
    degree: str = ""
    location: Optional[str] = None
    start: Optional[str] = None
    end: Optional[str] = None
    score: Optional[str] = None
    coursework: list[str] = Field(default_factory=list)


class ProfileIn(BaseModel):
    full_name: str
    email: str
    phone: Optional[str] = None
    location: Optional[str] = None
    headline: str = ""
    summary: str = ""
    graduation_year: Optional[int] = None
    target_roles: list[str] = Field(default_factory=list)
    skills: dict[str, list[str]] = Field(default_factory=dict)
    projects: list[ProjectIn] = Field(default_factory=list)
    experience: list[ExperienceIn] = Field(default_factory=list)
    education: list[EducationIn] = Field(default_factory=list)
    achievements: list[str] = Field(default_factory=list)
    github_url: Optional[str] = None
    linkedin_url: Optional[str] = None
    portfolio_url: Optional[str] = None

    @field_validator("full_name", "email")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value.strip()


PROFILE_FIELDS = tuple(ProfileIn.model_fields)


def profile_to_dict(profile: CandidateProfile) -> dict:
    return {name: getattr(profile, name) for name in PROFILE_FIELDS}


def apply_profile(profile: CandidateProfile, data: ProfileIn) -> CandidateProfile:
    dumped = data.model_dump()
    for name in PROFILE_FIELDS:
        setattr(profile, name, dumped[name])
    profile.updated_at = utcnow()
    return profile


def get_active_profile(session: Session) -> CandidateProfile | None:
    stmt = (
        select(CandidateProfile)
        .where(CandidateProfile.is_active == True)  # noqa: E712 (SQL expression)
        .order_by(col(CandidateProfile.id).desc())
    )
    return session.exec(stmt).first()


def first_name(full_name: str) -> str:
    parts = full_name.split()
    return parts[0] if parts else full_name


def primary_education(profile: CandidateProfile) -> dict:
    return profile.education[0] if profile.education else {}
