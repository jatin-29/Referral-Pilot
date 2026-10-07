"""Job-description parsing: which skills are required, preferred or just mentioned.

The text is split into sections by heading lines ("Requirements", "Nice to
have", ...). Each line yields *requirement units*: a single skill, or an
alternatives group when skills are listed with "or" / "such as" ("Java, Go,
Ruby or Python" is satisfied by any one of them).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from itertools import pairwise

from ..textutil import required_years
from .skills import category_of, find_mentions

WEIGHT_REQUIRED = 3.0
WEIGHT_MENTIONED = 2.0
WEIGHT_PREFERRED = 1.0

_APOS = r"(?:'|’)?"
PREFERRED_HEADING = re.compile(
    r"nice[\s-]to[\s-]have|preferred|bonus|pluses|good[\s-]to[\s-]have|extra credit|desired|"
    r"it" + _APOS + r"s a plus|would be (?:great|nice)|stand out|brownie points",
    re.IGNORECASE,
)
REQUIRED_HEADING = re.compile(
    r"requirement|qualification|what you" + _APOS + r"ll need|what you will need|what you need|"
    r"what we" + _APOS + r"re looking for|what we look for|looking for|what you bring|who you are|"
    r"you have|you should have|must[\s-]have|about you|ideal candidate|skills|eligibility|you" + _APOS + r"ll have",
    re.IGNORECASE,
)
RESPONSIBILITY_HEADING = re.compile(
    r"responsibilit|what you" + _APOS + r"ll do|what you will do|what you" + _APOS + r"ll work on|the role|your role|"
    r"day[\s-]to[\s-]day|you will (?:do|own|build|work|be doing)|in this role|your impact|"
    r"what you" + _APOS + r"ll be doing|the opportunity",
    re.IGNORECASE,
)
OTHER_HEADING = re.compile(
    r"about (?:us|the company|the team)|who we are|benefits|perks|why join|compensation|salary|"
    r"equal opportunity|our values|life at|how to apply|location",
    re.IGNORECASE,
)
PREFERRED_INLINE = re.compile(
    r"nice[\s-]to[\s-]have|a plus\b|is a plus|bonus|preferred|good[\s-]to[\s-]have|ideally|would be (?:great|nice)",
    re.IGNORECASE,
)
_LIST_SEPARATOR = re.compile(
    r"\s*(?:,|/|\(|\)|;)?\s*(?:,?\s*(?:and/or|or)(?:\s+(?:other|similar|equivalent|another|any|an?))?)?\s*[,(]?\s*",
    re.IGNORECASE,
)
_ALTERNATIVE_CUE = re.compile(r"(?:e\.g\.?|such as|like|including|for example|one of)[\s,:]*\(?\s*$", re.IGNORECASE)


@dataclass
class RequirementUnit:
    skills: tuple[str, ...]  # one skill, or alternatives (any satisfies the unit)
    weight: float
    section: str
    line: str


@dataclass
class ParsedJD:
    units: list[RequirementUnit] = field(default_factory=list)
    skill_weights: dict[str, float] = field(default_factory=dict)
    skill_counts: dict[str, int] = field(default_factory=dict)
    min_years: int | None = None

    @property
    def skills(self) -> list[str]:
        return sorted(self.skill_weights, key=lambda s: (-self.skill_weights[s], -self.skill_counts.get(s, 0), s))

    def by_section(self, section: str) -> list[str]:
        names = {s for unit in self.units if unit.section == section for s in unit.skills}
        return [s for s in self.skills if s in names]

    def to_dict(self) -> dict:
        return {
            "required": self.by_section("required"),
            "preferred": [s for s in self.by_section("preferred") if self.skill_weights.get(s) == WEIGHT_PREFERRED],
            "mentioned": self.by_section("mentioned"),
            "weights": self.skill_weights,
            "counts": self.skill_counts,
            "units": [{"skills": list(u.skills), "weight": u.weight, "section": u.section} for u in self.units],
            "min_years": self.min_years,
        }


def _classify_heading(line: str) -> str | None:
    stripped = line.strip().rstrip(":").strip()
    if not stripped or line.lstrip().startswith(("- ", "• ", "* ")):
        return None
    words = stripped.split()
    looks_like_heading = len(stripped) <= 60 and len(words) <= 8 and not stripped.endswith(".")
    if not looks_like_heading:
        return None
    if PREFERRED_HEADING.search(stripped):
        return "preferred"
    if REQUIRED_HEADING.search(stripped):
        return "required"
    if RESPONSIBILITY_HEADING.search(stripped):
        return "mentioned"
    if OTHER_HEADING.search(stripped):
        return "other"
    if line.rstrip().endswith(":"):
        return "mentioned"
    return None


def _section_weight(section: str, line: str) -> tuple[str, float]:
    if section == "preferred" or PREFERRED_INLINE.search(line):
        return "preferred", WEIGHT_PREFERRED
    if section == "required":
        return "required", WEIGHT_REQUIRED
    return "mentioned", WEIGHT_MENTIONED


def _units_for_line(line: str, section: str, weight: float) -> list[RequirementUnit]:
    mentions = find_mentions(line)
    if not mentions:
        return []
    chains: list[tuple[list[str], bool]] = []
    chain = [mentions[0].skill]
    has_or = bool(_ALTERNATIVE_CUE.search(line[max(0, mentions[0].start - 25): mentions[0].start]))
    for prev, cur in pairwise(mentions):
        separator = line[prev.end: cur.start]
        if _LIST_SEPARATOR.fullmatch(separator):
            chain.append(cur.skill)
            has_or = has_or or bool(re.search(r"\bor\b|/", separator, re.IGNORECASE))
        else:
            chains.append((chain, has_or))
            chain = [cur.skill]
            has_or = bool(_ALTERNATIVE_CUE.search(line[max(0, cur.start - 25): cur.start]))
    chains.append((chain, has_or))

    units: list[RequirementUnit] = []
    for skills, alternatives in chains:
        unique = tuple(dict.fromkeys(skills))
        if alternatives and len(unique) > 1:
            units.append(RequirementUnit(unique, weight, section, line))
        else:
            units.extend(RequirementUnit((skill,), weight, section, line) for skill in unique)
    return units


def parse_job_description(description: str, title: str = "") -> ParsedJD:
    parsed = ParsedJD(min_years=required_years(description))
    section = "mentioned"
    raw_units: list[RequirementUnit] = []

    if title:
        raw_units.extend(_units_for_line(title, "required", WEIGHT_REQUIRED))

    for line in description.splitlines():
        if not line.strip():
            continue
        heading = _classify_heading(line)
        if heading is not None:
            section = heading
            continue
        if section == "other":
            continue
        line_section, weight = _section_weight(section, line)
        raw_units.extend(_units_for_line(line, line_section, weight))

    # Deduplicate units, keeping the strongest weight for each.
    merged: dict[frozenset, RequirementUnit] = {}
    for unit in raw_units:
        key = frozenset(unit.skills)
        if key not in merged or unit.weight > merged[key].weight:
            merged[key] = unit
        for skill in unit.skills:
            parsed.skill_counts[skill] = parsed.skill_counts.get(skill, 0) + 1
            parsed.skill_weights[skill] = max(parsed.skill_weights.get(skill, 0.0), unit.weight)
    parsed.units = list(merged.values())
    return parsed


def role_family(title: str) -> str:
    lowered = title.lower()
    if re.search(r"full[\s-]?stack", lowered):
        return "fullstack"
    if re.search(r"front[\s-]?end|\bui\b|\bweb\b", lowered):
        return "frontend"
    if re.search(r"back[\s-]?end|\bapi\b|server|platform|payments|billing", lowered):
        return "backend"
    if re.search(r"android|\bios\b|mobile", lowered):
        return "mobile"
    if re.search(r"machine learning|\bml\b|\bai\b|data", lowered):
        return "data"
    if re.search(r"devops|\bsre\b|reliability|infrastructure|cloud", lowered):
        return "infra"
    return "general"


__all__ = [
    "ParsedJD",
    "RequirementUnit",
    "WEIGHT_MENTIONED",
    "WEIGHT_PREFERRED",
    "WEIGHT_REQUIRED",
    "category_of",
    "parse_job_description",
    "role_family",
]
