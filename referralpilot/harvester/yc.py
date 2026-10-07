"""Y Combinator jobs board (best-effort HTML scraping).

YC has no public jobs API. The listing page at
https://www.ycombinator.com/jobs/role/{role} embeds its data as JSON in a
`data-page` attribute; this parser walks any embedded JSON for job-shaped
objects and falls back to `/companies/<slug>/jobs/<id>` links. Pages are only
fetched when robots.txt allows it (RESPECT_ROBOTS_TXT=true).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urljoin, urlparse

from bs4 import BeautifulSoup

from .. import fetch
from ..fetch import FetchError, RobotsCache, request_with_retries
from ..textutil import html_to_text, normalize_text
from .base import Harvester, HarvestTarget, RawJob, TitlePrefilter, parse_timestamp

BASE_URL = "https://www.ycombinator.com"
LIST_URL = BASE_URL + "/jobs/role/{token}"
JOB_PATH_RE = re.compile(r"^/companies/(?P<company>[a-z0-9][a-z0-9-]*)/jobs/(?P<id>[A-Za-z0-9]+)(?:-[^/?#]*)?/?$")


@dataclass
class YCListing:
    id: str
    title: str
    url: str
    company: str
    location: str | None = None
    employment_type: str | None = None
    experience: str | None = None
    description: str = ""
    posted_at: Any = None


class YCJobsHarvester(Harvester):
    ats_type = "yc"
    max_detail_fetches = 20
    detail_delay_seconds = 1.0

    def __init__(self, client, settings=None):
        super().__init__(client, settings)
        self.robots = RobotsCache(self.settings.http_user_agent)

    def fetch(self, target: HarvestTarget, title_prefilter: TitlePrefilter | None = None) -> list[RawJob]:
        token = target.board_token.strip()
        url = token if token.startswith("http") else LIST_URL.format(token=quote(token, safe=""))
        html = self._get_html(url)
        jobs: list[RawJob] = []
        details_fetched = 0
        for listing in parse_listing_page(html):
            wanted = title_prefilter is None or title_prefilter(listing.title)
            description = listing.description
            if wanted and not description and details_fetched < self.max_detail_fetches:
                if details_fetched:
                    fetch.sleep(self.detail_delay_seconds)
                details_fetched += 1
                try:
                    description = parse_detail_page(self._get_html(listing.url))
                except FetchError:
                    description = ""
            if listing.experience:
                description = f"{description}\n\nExperience: {listing.experience}".strip()
            jobs.append(
                RawJob(
                    company=listing.company,
                    external_id=f"yc-{listing.id}",
                    title=listing.title,
                    url=listing.url,
                    ats_type=self.ats_type,
                    location=listing.location,
                    employment_type=listing.employment_type,
                    description=description,
                    posted_at=parse_timestamp(listing.posted_at),
                )
            )
        return jobs

    def _get_html(self, url: str) -> str:
        if self.settings.respect_robots_txt and not self.robots.allowed(self.client, url):
            raise FetchError(f"robots.txt disallows fetching {url}")
        response = request_with_retries(self.client, "GET", url, headers={"Accept": "text/html"})
        if response.status_code >= 400:
            raise FetchError(f"HTTP {response.status_code} from {url}", response.status_code)
        return response.text


# --- parsing -------------------------------------------------------------------

def _embedded_json(soup: BeautifulSoup) -> Iterator[Any]:
    for tag in soup.find_all(attrs={"data-page": True}):
        try:
            yield json.loads(tag["data-page"])
        except (TypeError, ValueError):
            continue
    for tag in soup.find_all("script", attrs={"type": "application/json"}):
        try:
            yield json.loads(tag.string or "")
        except (TypeError, ValueError):
            continue


def _walk(node: Any) -> Iterator[dict]:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def _company_name(node: dict) -> str | None:
    company = node.get("company")
    if isinstance(company, dict):
        return company.get("name")
    return node.get("companyName") or node.get("company_name") or (company if isinstance(company, str) else None)


def _looks_like_job(node: dict) -> bool:
    has_title = isinstance(node.get("title"), str)
    has_company = bool(_company_name(node) or node.get("companySlug"))
    has_ref = node.get("id") is not None or any(node.get(k) for k in ("url", "jobUrl", "show_path"))
    return has_title and has_company and has_ref


def _slug_to_name(slug: str) -> str:
    return " ".join(part.capitalize() for part in slug.split("-"))


def _listing_from_json(node: dict) -> YCListing | None:
    raw_url = node.get("url") or node.get("jobUrl") or node.get("show_path") or ""
    url = urljoin(BASE_URL, raw_url) if raw_url else ""
    match = JOB_PATH_RE.match(urlparse(url).path) if url else None
    job_id = str(node.get("id") or (match.group("id") if match else ""))
    if not job_id:
        return None
    company = _company_name(node) or _slug_to_name(node.get("companySlug") or (match.group("company") if match else ""))
    if not url:
        url = f"{BASE_URL}/companies/{node.get('companySlug', 'unknown')}/jobs/{job_id}"
    location = node.get("location") or node.get("locations")
    if isinstance(location, list):
        location = " / ".join(str(loc) for loc in location)
    description = node.get("description") or node.get("descriptionHtml") or ""
    return YCListing(
        id=job_id,
        title=node["title"].strip(),
        url=url,
        company=company or "Unknown YC company",
        location=location or None,
        employment_type=node.get("type") or node.get("jobType"),
        experience=node.get("minExperience") or node.get("experience"),
        description=html_to_text(description) if "<" in description else normalize_text(description),
        posted_at=node.get("createdAt") or node.get("created_at") or node.get("lastActive"),
    )


def parse_listing_page(html: str) -> list[YCListing]:
    soup = BeautifulSoup(html, "html.parser")
    listings: dict[str, YCListing] = {}
    for blob in _embedded_json(soup):
        for node in _walk(blob):
            if _looks_like_job(node):
                listing = _listing_from_json(node)
                if listing:
                    listings.setdefault(listing.id, listing)
    if listings:
        return list(listings.values())
    # Fallback: plain links to job pages.
    for anchor in soup.find_all("a", href=True):
        path = urlparse(urljoin(BASE_URL, anchor["href"])).path
        match = JOB_PATH_RE.match(path)
        title = anchor.get_text(" ", strip=True)
        if not match or not title:
            continue
        listings.setdefault(
            match.group("id"),
            YCListing(
                id=match.group("id"),
                title=title,
                url=urljoin(BASE_URL, path),
                company=_slug_to_name(match.group("company")),
            ),
        )
    return list(listings.values())


def parse_detail_page(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for blob in _embedded_json(soup):
        for node in _walk(blob):
            description = node.get("description") if isinstance(node, dict) else None
            if isinstance(description, str) and len(description) > 80:
                return html_to_text(description) if "<" in description else normalize_text(description)
    main = soup.find("main") or soup.body or soup
    return html_to_text(str(main))[:8000]
