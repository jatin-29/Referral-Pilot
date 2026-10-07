"""Seed the database with the candidate profile and target companies."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from .activity import get_logger
from .config import get_settings
from .models import CandidateProfile, Company
from .profile import ProfileIn, apply_profile, get_active_profile

log = get_logger("app")

SUPPORTED_ATS = {"greenhouse", "lever", "ashby", "yc"}


@dataclass
class SeedResult:
    profile_created: bool
    companies_added: int
    companies_updated: int


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def seed_profile(session: Session, path: Path | None = None, *, force: bool = False) -> tuple[CandidateProfile, bool]:
    """Create the active profile from JSON. Existing profiles are kept unless `force`."""
    path = path or get_settings().config_dir / "candidate_profile.json"
    existing = get_active_profile(session)
    if existing is not None and not force:
        return existing, False
    data = ProfileIn.model_validate(load_json(path))
    profile = existing or CandidateProfile(full_name=data.full_name, email=data.email)
    apply_profile(profile, data)
    session.add(profile)
    session.flush()
    log.info("Loaded candidate profile for %s from %s", profile.full_name, path.name)
    return profile, existing is None


def upsert_company(session: Session, entry: dict[str, Any]) -> tuple[Company, bool]:
    ats_type = str(entry["ats_type"]).strip().lower()
    if ats_type not in SUPPORTED_ATS:
        raise ValueError(f"Unsupported ats_type {ats_type!r} for {entry.get('name')}")
    token = str(entry["board_token"]).strip()
    company = session.exec(
        select(Company).where(Company.ats_type == ats_type, Company.board_token == token)
    ).first()
    created = company is None
    if company is None:
        company = Company(name=entry["name"], ats_type=ats_type, board_token=token)
    company.name = entry.get("name", company.name)
    company.domain = entry.get("domain") or company.domain
    company.region = entry.get("region") or company.region
    company.enabled = bool(entry.get("enabled", True))
    company.tags = list(entry.get("tags", company.tags or []))
    session.add(company)
    return company, created


def seed_companies(session: Session, path: Path | None = None) -> tuple[int, int]:
    path = path or get_settings().config_dir / "companies.json"
    payload = load_json(path)
    entries = payload["companies"] if isinstance(payload, dict) else payload
    added = updated = 0
    for entry in entries:
        _, created = upsert_company(session, entry)
        added += created
        updated += not created
    session.flush()
    log.info("Synced %d target companies from %s (%d new)", added + updated, path.name, added)
    return added, updated


def seed_all(session: Session, *, force_profile: bool = False) -> SeedResult:
    _, created = seed_profile(session, force=force_profile)
    added, updated = seed_companies(session)
    return SeedResult(profile_created=created, companies_added=added, companies_updated=updated)
