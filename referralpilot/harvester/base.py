"""Common types for ATS harvesters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import ClassVar

import httpx

from ..config import Settings, get_settings


@dataclass
class HarvestTarget:
    """What to crawl: a company's board on a given ATS."""

    name: str
    ats_type: str
    board_token: str
    domain: str | None = None
    region: str | None = None
    company_id: int | None = None


@dataclass
class RawJob:
    """A posting normalised across ATS providers."""

    company: str
    external_id: str
    title: str
    url: str
    ats_type: str
    location: str | None = None
    department: str | None = None
    employment_type: str | None = None
    description: str = ""
    posted_at: datetime | None = None
    company_domain: str | None = None


TitlePrefilter = Callable[[str], bool]


class Harvester(ABC):
    ats_type: ClassVar[str]

    def __init__(self, client: httpx.Client, settings: Settings | None = None):
        self.client = client
        self.settings = settings or get_settings()

    @abstractmethod
    def fetch(self, target: HarvestTarget, title_prefilter: TitlePrefilter | None = None) -> list[RawJob]:
        """Return every posting on the target's board.

        `title_prefilter` lets sources that need one request per posting (YC)
        skip detail fetches for titles that would be filtered out anyway.
        """


def parse_timestamp(value) -> datetime | None:
    """Parse ISO-8601 strings or epoch milliseconds into aware UTC datetimes."""
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):
            seconds = value / 1000 if value > 10**11 else value
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        text = str(value).strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
    except (ValueError, OverflowError, OSError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
