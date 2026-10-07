"""Probable corporate email formats ({first}.{last}@domain, {first}@domain, ...)."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from ..textutil import ascii_fold

# Rough prevalence of corporate address formats; a known pattern overrides these.
PATTERN_PRIORS: list[tuple[str, float]] = [
    ("{first}.{last}", 0.38),
    ("{first}", 0.16),
    ("{f}{last}", 0.14),
    ("{first}{last}", 0.08),
    ("{first}_{last}", 0.04),
    ("{f}.{last}", 0.04),
    ("{first}{l}", 0.03),
    ("{last}.{first}", 0.03),
    ("{last}{f}", 0.02),
    ("{last}", 0.02),
    ("{first}-{last}", 0.02),
    ("{first}.{l}", 0.02),
]
KNOWN_PATTERNS = {pattern for pattern, _ in PATTERN_PRIORS}

_HONORIFICS = {"dr", "mr", "mrs", "ms", "miss", "prof", "sir", "er", "shri", "smt"}
_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "phd", "mba", "pmp", "cfa", "md"}
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


@dataclass(frozen=True)
class EmailGuess:
    email: str
    pattern: str
    confidence: float


def is_valid_email(value: str | None) -> bool:
    return bool(value and _EMAIL_RE.match(value.strip()))


def _clean_part(part: str) -> str:
    return re.sub(r"[^a-z0-9]", "", ascii_fold(part).lower())


def split_name(full_name: str) -> tuple[str, str]:
    """(first, last) normalised for email local parts; last may be ''."""
    name = re.sub(r"\(.*?\)|\[.*?\]", " ", full_name)  # "(He/Him)", "[Hiring]"
    name = name.split(",")[0]  # "Jane Doe, PhD"
    tokens = [t for t in re.split(r"\s+", name.strip()) if t]
    tokens = [t for t in tokens if _clean_part(t) not in _HONORIFICS or len(tokens) <= 2]
    tokens = [t for t in tokens if _clean_part(t) not in _SUFFIXES]
    tokens = [t for t in tokens if _clean_part(t)]
    if not tokens:
        return "", ""
    first = _clean_part(tokens[0])
    last = _clean_part(tokens[-1]) if len(tokens) > 1 else ""
    return first, last


def render_pattern(pattern: str, first: str, last: str) -> str | None:
    if not first or (("{last}" in pattern or "{l}" in pattern) and not last):
        return None
    return pattern.format(first=first, last=last, f=first[:1], l=last[:1])


def generate_candidates(
    full_name: str,
    domain: str,
    known_pattern: str | None = None,
    known_confidence: float = 0.85,
) -> list[EmailGuess]:
    first, last = split_name(full_name)
    if not first or not domain:
        return []
    guesses: dict[str, EmailGuess] = {}
    for pattern, prior in PATTERN_PRIORS:
        local = render_pattern(pattern, first, last)
        if not local:
            continue
        if known_pattern:
            confidence = known_confidence if pattern == known_pattern else prior * (1 - known_confidence)
        else:
            confidence = prior
        email = f"{local}@{domain}"
        if email not in guesses or guesses[email].confidence < confidence:
            guesses[email] = EmailGuess(email, pattern, round(confidence, 3))
    if known_pattern and known_pattern not in KNOWN_PATTERNS:
        local = render_pattern(known_pattern, first, last)
        if local:
            guesses[f"{local}@{domain}"] = EmailGuess(f"{local}@{domain}", known_pattern, known_confidence)
    return sorted(guesses.values(), key=lambda g: -g.confidence)


def infer_pattern(examples: list[tuple[str, str]]) -> tuple[str | None, float]:
    """Infer the domain's format from (full name, known email) pairs -> (pattern, support)."""
    votes: Counter[str] = Counter()
    usable = 0
    for full_name, email in examples:
        if not is_valid_email(email):
            continue
        first, last = split_name(full_name)
        local = email.split("@", 1)[0].lower()
        matches = [p for p, _ in PATTERN_PRIORS if render_pattern(p, first, last) == local]
        if matches:
            usable += 1
            votes[matches[0]] += 1
    if not votes:
        return None, 0.0
    pattern, count = votes.most_common(1)[0]
    return pattern, count / usable
