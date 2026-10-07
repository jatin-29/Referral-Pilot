"""SQLite schema (SQLModel).

The four tables from the spec are `Job`, `CandidateProfile`, `ReferralContact`
and `OutreachLog`. `Company`, `ActivityLog`, `AppState` and `Suppression`
support harvesting targets, the live log, scheduler state and opt-outs.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Optional

from sqlalchemy import JSON, Column, DateTime, Text, UniqueConstraint
from sqlalchemy.types import TypeDecorator
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    """Timezone-aware UTC timestamp; every datetime in the app is aware UTC."""
    return datetime.now(timezone.utc)


def as_utc(value: datetime | None) -> datetime | None:
    """Coerce a datetime to aware UTC (naive values are assumed to be UTC)."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class UTCDateTime(TypeDecorator):
    """Stores naive UTC in SQLite and always hands back aware UTC datetimes.

    Declared explicitly so behaviour is identical across SQLModel versions
    (newer releases reject naive values, older ones drop the offset).
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return as_utc(value).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        return as_utc(value)


def DateTimeField(**kwargs: Any) -> Any:  # noqa: N802 (field factory)
    return Field(sa_type=UTCDateTime, **kwargs)


class JobStatus(StrEnum):
    DISCOVERED = "discovered"
    TAILORED = "tailored"
    QUEUED = "queued"
    CONTACTED = "contacted"
    FOLLOWUP_SENT = "followup_sent"
    REPLIED = "replied"
    REFERRED = "referred"
    ARCHIVED = "archived"


# Forward-only pipeline order used when statuses advance automatically.
JOB_STATUS_RANK: dict[str, int] = {
    JobStatus.DISCOVERED: 0,
    JobStatus.TAILORED: 1,
    JobStatus.QUEUED: 2,
    JobStatus.CONTACTED: 3,
    JobStatus.FOLLOWUP_SENT: 4,
    JobStatus.REPLIED: 5,
    JobStatus.REFERRED: 6,
}

# Kanban columns: (column key, label, job statuses shown in the column).
BOARD_COLUMNS: list[tuple[str, str, tuple[str, ...]]] = [
    ("discovered", "Discovered", (JobStatus.DISCOVERED,)),
    ("tailored", "Matched / Tailored", (JobStatus.TAILORED,)),
    ("queued", "Outreach Queued", (JobStatus.QUEUED,)),
    ("contacted", "Contacted", (JobStatus.CONTACTED,)),
    ("followup_sent", "Follow-Up Sent", (JobStatus.FOLLOWUP_SENT,)),
    ("replied", "Replied / Referred", (JobStatus.REPLIED, JobStatus.REFERRED)),
]


def advance_job_status(job: Job, status: str) -> bool:
    """Move a job forward in the pipeline; never backwards, never out of ARCHIVED."""
    if job.status == JobStatus.ARCHIVED or status not in JOB_STATUS_RANK:
        return False
    if JOB_STATUS_RANK[status] > JOB_STATUS_RANK.get(job.status, -1):
        job.status = status
        job.updated_at = utcnow()
        return True
    return False


class ContactStatus(StrEnum):
    NEW = "new"
    DRAFTED = "drafted"
    QUEUED = "queued"
    CONTACTED = "contacted"
    FOLLOWUP_SENT = "followup_sent"
    REPLIED = "replied"
    REFERRED = "referred"
    OPTED_OUT = "opted_out"
    BOUNCED = "bounced"


# Contacts in these states must never be emailed again.
CONTACT_DO_NOT_EMAIL = {ContactStatus.OPTED_OUT, ContactStatus.BOUNCED}


class OutreachType(StrEnum):
    INITIAL = "initial"
    FOLLOWUP = "followup"


class OutreachStatus(StrEnum):
    DRAFT = "draft"
    QUEUED = "queued"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


class Company(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("ats_type", "board_token", name="uq_company_board"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    ats_type: str  # greenhouse | lever | ashby | yc
    board_token: str  # slug used in the ATS API URL
    domain: Optional[str] = None
    region: Optional[str] = None  # e.g. "eu" for api.eu.lever.co
    enabled: bool = True
    tags: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    last_harvested_at: Optional[datetime] = DateTimeField(default=None)
    last_harvest_status: Optional[str] = None
    created_at: datetime = DateTimeField(default_factory=utcnow)


class Job(SQLModel, table=True):
    # Dedupe key: the ATS job id is unique per company.
    __table_args__ = (UniqueConstraint("company", "external_id", name="uq_job_company_external"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    company: str = Field(index=True)
    company_id: Optional[int] = Field(default=None, foreign_key="company.id")
    external_id: str  # the ATS job_id
    title: str
    url: str
    ats_type: str
    location: Optional[str] = None
    department: Optional[str] = None
    employment_type: Optional[str] = None
    description: str = Field(default="", sa_column=Column(Text))
    posted_at: Optional[datetime] = DateTimeField(default=None)
    discovered_at: datetime = DateTimeField(default_factory=utcnow, index=True)
    updated_at: datetime = DateTimeField(default_factory=utcnow)
    status: str = Field(default=JobStatus.DISCOVERED, index=True)
    domain: Optional[str] = None
    domain_source: Optional[str] = None  # config | posting_url | dns_guess | guess
    last_seen_at: Optional[datetime] = DateTimeField(default=None)

    # Filled by the tailor module.
    match_score: Optional[float] = None
    jd_skills: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    matched_skills: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    missing_skills: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    match_details: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    min_years_required: Optional[int] = None
    resume_pdf_path: Optional[str] = None
    resume_tex_path: Optional[str] = None
    resume_engine: Optional[str] = None
    tailored_at: Optional[datetime] = DateTimeField(default=None)
    notes: Optional[str] = Field(default=None, sa_column=Column(Text))


class CandidateProfile(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    full_name: str
    email: str
    phone: Optional[str] = None
    location: Optional[str] = None
    headline: str = ""
    summary: str = Field(default="", sa_column=Column(Text))
    graduation_year: Optional[int] = None
    target_roles: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    # {"Languages": [...], "Frameworks": [...], "Systems": [...]} -- category order is kept.
    skills: dict[str, list[str]] = Field(default_factory=dict, sa_column=Column(JSON))
    # [{"name", "summary", "tech": [...], "link", "bullets": [...]}]
    projects: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    # [{"company", "role", "location", "start", "end", "bullets": [...]}]
    experience: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    # [{"institution", "short_name", "degree", "start", "end", "score", "coursework": [...]}]
    education: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    achievements: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    github_url: Optional[str] = None
    linkedin_url: Optional[str] = None
    portfolio_url: Optional[str] = None
    is_active: bool = True
    created_at: datetime = DateTimeField(default_factory=utcnow)
    updated_at: datetime = DateTimeField(default_factory=utcnow)


class ReferralContact(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="job.id", index=True)
    name: str
    role: Optional[str] = None
    email: Optional[str] = Field(default=None, index=True)
    email_confidence: float = 0.0
    email_source: Optional[str] = None  # hunter | apollo | pattern | manual
    alternate_emails: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    linkedin_url: Optional[str] = None
    source: str = "manual"  # provider that surfaced the person
    is_alumni: bool = False
    priority_score: float = 0.0
    status: str = Field(default=ContactStatus.NEW, index=True)
    notes: Optional[str] = Field(default=None, sa_column=Column(Text))
    created_at: datetime = DateTimeField(default_factory=utcnow)
    updated_at: datetime = DateTimeField(default_factory=utcnow)

    @property
    def first_name(self) -> str:
        parts = self.name.split()
        return parts[0] if parts else ""


class OutreachLog(SQLModel, table=True):
    """One email: a draft, then queued, then sent (or cancelled / skipped / failed)."""

    id: Optional[int] = Field(default=None, primary_key=True)
    contact_id: int = Field(foreign_key="referralcontact.id", index=True)
    job_id: int = Field(foreign_key="job.id", index=True)
    type: str = Field(default=OutreachType.INITIAL, index=True)
    status: str = Field(default=OutreachStatus.DRAFT, index=True)
    to_email: str
    subject: str
    body: str = Field(sa_column=Column(Text))
    attachment_path: Optional[str] = None
    parent_id: Optional[int] = Field(default=None, foreign_key="outreachlog.id")
    message_id: Optional[str] = None
    thread_id: Optional[str] = None
    in_reply_to: Optional[str] = None
    scheduled_for: Optional[datetime] = DateTimeField(default=None)
    queued_at: Optional[datetime] = DateTimeField(default=None)
    sent_at: Optional[datetime] = DateTimeField(default=None, index=True)
    dry_run: bool = False
    replied: bool = False
    replied_at: Optional[datetime] = DateTimeField(default=None)
    attempts: int = 0
    error: Optional[str] = Field(default=None, sa_column=Column(Text))
    created_at: datetime = DateTimeField(default_factory=utcnow)
    updated_at: datetime = DateTimeField(default_factory=utcnow)


class ActivityLog(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    created_at: datetime = DateTimeField(default_factory=utcnow, index=True)
    level: str = "INFO"
    source: str = Field(default="app", index=True)
    message: str = Field(sa_column=Column(Text))
    job_id: Optional[int] = None


class AppState(SQLModel, table=True):
    """Small key/value store for scheduler state (next send time, pause flag...)."""

    key: str = Field(primary_key=True)
    value: Optional[str] = None
    updated_at: datetime = DateTimeField(default_factory=utcnow)


class Suppression(SQLModel, table=True):
    """Addresses that must never be emailed (opt-outs, hard bounces)."""

    email: str = Field(primary_key=True)
    reason: str = "opt_out"
    created_at: datetime = DateTimeField(default_factory=utcnow)
