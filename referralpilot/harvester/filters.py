"""Entry-level role filters.

A posting is kept when its title matches an include pattern, matches no
exclude pattern (Senior, Staff, Lead, level III...), and its description does
not ask for more than MAX_REQUIRED_YEARS of experience (3+, 5+...).
Patterns can be overridden in config/filters.json.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Settings, get_settings
from ..textutil import required_years
from .base import RawJob

DEFAULT_TITLE_INCLUDE = [
    r"\bsoftware\s+(?:development\s+)?engineer",
    r"\bsoftware\s+developer",
    r"\bsde\b",
    r"\bswe\b",
    r"\bback[\s-]?end\b.*\b(?:developer|engineer)",
    r"\bfront[\s-]?end\b.*\b(?:developer|engineer)",
    r"\bfull[\s-]?stack\b.*\b(?:developer|engineer)",
    r"\bgraduate\s+(?:software\s+)?(?:engineer|developer)",
    r"\b(?:associate|junior|jr\.?|entry[\s-]level)\s+(?:software\s+)?(?:developer|engineer)",
    r"\b(?:python|java|golang|go|node(?:\.?js)?|react(?:\.?js)?|javascript|typescript|web|android|ios|mobile|application|platform)\s+(?:developer|engineer)",
    r"\bengineer\s*[-–]?\s*(?:i|1)\b",
]

DEFAULT_TITLE_EXCLUDE = [
    r"\bsenior\b", r"\bsr\b", r"\bstaff\b", r"\blead\b", r"\bprincipal\b", r"\bmanager\b",
    r"\bmanagement\b", r"\bdirector\b", r"\bhead\s+of\b", r"\bvp\b", r"\bvice\s+president\b",
    r"\barchitect\b", r"\bdistinguished\b", r"\bfellow\b", r"\bchief\b", r"\bexpert\b",
    r"\b(?:iii|iv)\b", r"\b(?:engineer|developer|sde|swe)\s*[-–]?\s*(?:v|[3-9])\b", r"\b(?:l|ic)[5-9]\b",
]

LEVEL_TWO_PATTERNS = [r"\b(?:engineer|developer|sde|swe)\s*[-–]?\s*(?:ii|2)\b"]

INTERNSHIP_PATTERNS = [r"\bintern(?:ship)?s?\b", r"\bco-?op\b", r"\bapprentice"]


@dataclass
class FilterRules:
    title_include: list[str] = field(default_factory=lambda: list(DEFAULT_TITLE_INCLUDE))
    title_exclude: list[str] = field(default_factory=lambda: list(DEFAULT_TITLE_EXCLUDE))
    exclude_level_two: bool = True
    include_internships: bool = False
    max_required_years: int = 2
    location_keywords: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, settings: Settings | None = None, path: Path | None = None) -> FilterRules:
        settings = settings or get_settings()
        path = path or settings.config_dir / "filters.json"
        overrides: dict = {}
        if path.exists():
            with path.open(encoding="utf-8") as fh:
                overrides = json.load(fh)
        return cls(
            title_include=overrides.get("title_include") or list(DEFAULT_TITLE_INCLUDE),
            title_exclude=overrides.get("title_exclude") or list(DEFAULT_TITLE_EXCLUDE),
            exclude_level_two=bool(overrides.get("exclude_level_two", True)),
            include_internships=settings.include_internships,
            max_required_years=settings.max_required_years,
            location_keywords=list(settings.location_keywords),
        )


@dataclass
class FilterDecision:
    accepted: bool
    reason: str = "ok"
    min_years: int | None = None


def _compile(patterns: list[str]) -> list[re.Pattern]:
    return [re.compile(pattern, re.IGNORECASE) for pattern in patterns]


def _normalize_title(title: str) -> str:
    return re.sub(r"\s+", " ", title.replace("–", "-").replace("—", "-")).strip()


class JobFilter:
    def __init__(self, rules: FilterRules | None = None):
        self.rules = rules or FilterRules()
        self._include = _compile(self.rules.title_include)
        self._exclude = _compile(self.rules.title_exclude)
        self._level_two = _compile(LEVEL_TWO_PATTERNS)
        self._intern = _compile(INTERNSHIP_PATTERNS)
        self._locations = [kw.lower() for kw in self.rules.location_keywords if kw.strip()]

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> JobFilter:
        return cls(FilterRules.load(settings))

    def check_title(self, title: str) -> tuple[bool, str]:
        title = _normalize_title(title)
        if not any(p.search(title) for p in self._include):
            return False, "title_not_target_role"
        if any(p.search(title) for p in self._exclude):
            return False, "senior_title"
        if self.rules.exclude_level_two and any(p.search(title) for p in self._level_two):
            return False, "level_two_title"
        if not self.rules.include_internships and any(p.search(title) for p in self._intern):
            return False, "internship"
        return True, "ok"

    def title_ok(self, title: str) -> bool:
        return self.check_title(title)[0]

    def location_ok(self, location: str | None) -> bool:
        if not self._locations or not location:
            return True  # unknown locations are kept rather than silently dropped
        lowered = location.lower()
        return any(keyword in lowered for keyword in self._locations)

    def evaluate(self, job: RawJob) -> FilterDecision:
        ok, reason = self.check_title(job.title)
        if not ok:
            return FilterDecision(False, reason)
        if not self.location_ok(job.location):
            return FilterDecision(False, "location")
        years = required_years(job.description)
        if years is not None and years > self.rules.max_required_years:
            return FilterDecision(False, f"requires_{years}+_years", years)
        return FilterDecision(True, "ok", years)
