"""Contact enrichment providers.

Every provider implements `find_contacts(ctx)`; API providers (Hunter, Apollo)
may also return a domain's email pattern. Search providers (Brave, Google
Custom Search, DuckDuckGo) look up public LinkedIn profiles with prioritised
queries such as:  site:linkedin.com/in "Stripe" ("Software Engineer" OR "SDE")
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import ClassVar
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import httpx
from bs4 import BeautifulSoup

from .. import fetch
from ..config import Settings
from ..fetch import FetchError, RobotsCache, get_json, request_with_retries
from .patterns import is_valid_email


class ProviderError(RuntimeError):
    pass


@dataclass
class ContactCandidate:
    name: str
    role: str | None = None
    email: str | None = None
    email_confidence: float = 0.0
    email_source: str | None = None
    linkedin_url: str | None = None
    source: str = "unknown"
    snippet: str = ""
    is_alumni: bool = False
    alternate_emails: list[str] = field(default_factory=list)


@dataclass
class ProspectContext:
    company: str
    domain: str | None
    role_title: str = ""
    institutions: list[str] = field(default_factory=list)
    limit: int = 5


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str = ""


# --- search queries --------------------------------------------------------------

def build_queries(ctx: ProspectContext) -> list[tuple[str, str]]:
    """Prioritised (label, query) pairs: engineers first, then alumni, then recruiters."""
    company = ctx.company.replace('"', "")
    queries = [("Engineers", f'site:linkedin.com/in "{company}" ("Software Engineer" OR "SDE")')]
    for institution in ctx.institutions[:2]:
        queries.append((f"Alumni ({institution})", f'site:linkedin.com/in "{company}" "{institution}"'))
    queries.append(("Recruiters", f'site:linkedin.com/in "{company}" ("Technical Recruiter" OR "Talent Acquisition")'))
    return queries


def search_links(ctx: ProspectContext) -> list[dict]:
    """Ready-to-click search URLs for manual prospecting (no API key needed)."""
    links = []
    for label, query in build_queries(ctx):
        links.append({"label": label, "engine": "Google", "url": f"https://www.google.com/search?q={quote_plus(query)}"})
        links.append({"label": label, "engine": "DuckDuckGo", "url": f"https://duckduckgo.com/?q={quote_plus(query)}"})
    keywords = quote_plus(f"{ctx.company} software engineer")
    links.append({"label": "People search", "engine": "LinkedIn",
                  "url": f"https://www.linkedin.com/search/results/people/?keywords={keywords}"})
    return links


# --- LinkedIn result parsing --------------------------------------------------------

_LINKEDIN_PROFILE = re.compile(r"^https?://(?:[a-z]{2,3}\.|www\.)?linkedin\.com/in/[^/?#]+", re.IGNORECASE)
_TITLE_SPLIT = re.compile(r"\s+[-–—|]\s+")
_NOT_A_NAME = re.compile(r"\b(?:jobs?|careers?|hiring|linkedin|company|team|official|page)\b", re.IGNORECASE)


def normalize_linkedin(url: str | None) -> str | None:
    if not url:
        return None
    match = _LINKEDIN_PROFILE.match(url.strip())
    if not match:
        return None
    path = urlparse(match.group(0)).path.rstrip("/")
    return f"https://www.linkedin.com{path}".lower()


def _mentions(text: str | None, company: str) -> bool:
    if not text:
        return False
    return re.search(rf"(?<![\w]){re.escape(company.lower())}(?![\w])", text.lower()) is not None


def _looks_like_name(name: str, company: str) -> bool:
    tokens = name.split()
    if not 2 <= len(tokens) <= 5 or len(name) > 50:
        return False
    if any(ch.isdigit() for ch in name) or _NOT_A_NAME.search(name) or _mentions(name, company):
        return False
    return all(token[0].isalpha() for token in tokens)


def parse_linkedin_results(results: list[SearchResult], ctx: ProspectContext, source: str) -> list[ContactCandidate]:
    contacts: list[ContactCandidate] = []
    for result in results:
        url = normalize_linkedin(result.url)
        if not url:
            continue
        title = re.sub(r"\s*[|\-–—]\s*LinkedIn\s*$", "", result.title.strip(), flags=re.IGNORECASE)
        parts = [part.strip() for part in _TITLE_SPLIT.split(title) if part.strip()]
        if not parts:
            continue
        name, rest = parts[0], parts[1:]
        role = current = None
        if len(rest) >= 2:
            role, current = rest[0], rest[-1]
        elif rest:
            if _mentions(rest[0], ctx.company):
                current = rest[0]
            else:
                role = rest[0]
        snippet = result.snippet or ""
        if current and not _mentions(current, ctx.company):
            continue  # currently works somewhere else
        if not current and not _mentions(snippet, ctx.company) and not _mentions(role, ctx.company):
            continue
        if not _looks_like_name(name, ctx.company):
            continue
        if role and _mentions(role, ctx.company):
            role = re.split(r"\s+(?:at|@)\s+", role, flags=re.IGNORECASE)[0].strip() or None
        if not role:
            match = re.match(rf"^(.{{3,60}}?)\s+(?:at|@)\s+{re.escape(ctx.company)}", snippet, re.IGNORECASE)
            role = match.group(1).strip() if match else None
        haystack = f"{title} {snippet}".lower()
        alumni = any(inst.lower() in haystack for inst in ctx.institutions if len(inst) >= 3)
        contacts.append(ContactCandidate(name=name, role=role, linkedin_url=url, source=source,
                                         snippet=snippet[:300], is_alumni=alumni))
    return contacts


# --- providers ---------------------------------------------------------------------

class ContactProvider(ABC):
    name: ClassVar[str]
    requires: ClassVar[tuple[str, ...]] = ()

    def __init__(self, client: httpx.Client, settings: Settings):
        self.client = client
        self.settings = settings

    def available(self) -> bool:
        return all(getattr(self.settings, attr, None) for attr in self.requires)

    def email_pattern(self, domain: str) -> str | None:
        return None

    @abstractmethod
    def find_contacts(self, ctx: ProspectContext) -> list[ContactCandidate]: ...


class HunterProvider(ContactProvider):
    """Hunter.io domain search (free tier: 25 searches/month)."""

    name = "hunter"
    requires = ("hunter_api_key",)
    URL = "https://api.hunter.io/v2/domain-search"

    def __init__(self, client, settings):
        super().__init__(client, settings)
        self._cache: dict[str, dict] = {}

    def _domain_search(self, domain: str) -> dict:
        if domain not in self._cache:
            try:
                payload = get_json(self.client, self.URL, params={
                    "domain": domain, "api_key": self.settings.hunter_api_key, "limit": 10, "type": "personal",
                })
            except FetchError as exc:
                raise ProviderError(f"Hunter: {exc}") from exc
            self._cache[domain] = payload.get("data") or {}
        return self._cache[domain]

    def email_pattern(self, domain: str) -> str | None:
        return self._domain_search(domain).get("pattern") or None

    def find_contacts(self, ctx: ProspectContext) -> list[ContactCandidate]:
        if not ctx.domain:
            return []
        contacts = []
        for entry in self._domain_search(ctx.domain).get("emails") or []:
            if entry.get("type") == "generic" or not entry.get("first_name"):
                continue
            name = f"{entry.get('first_name', '')} {entry.get('last_name') or ''}".strip()
            contacts.append(ContactCandidate(
                name=name,
                role=entry.get("position"),
                email=entry.get("value"),
                email_confidence=min(1.0, (entry.get("confidence") or 0) / 100),
                email_source="hunter",
                linkedin_url=normalize_linkedin(entry.get("linkedin")),
                source=self.name,
            ))
        return contacts


class ApolloProvider(ContactProvider):
    """Apollo.io people search (emails are only returned on plans that unlock them)."""

    name = "apollo"
    requires = ("apollo_api_key",)
    TITLES = ["software engineer", "software development engineer", "sde", "backend engineer",
              "engineering manager", "technical recruiter"]

    def find_contacts(self, ctx: ProspectContext) -> list[ContactCandidate]:
        if not ctx.domain:
            return []
        body = {
            "q_organization_domains_list": [ctx.domain],
            "q_organization_domains": ctx.domain,
            "person_titles": self.TITLES,
            "page": 1,
            "per_page": max(ctx.limit * 2, 10),
        }
        headers = {"X-Api-Key": self.settings.apollo_api_key, "Cache-Control": "no-cache"}
        try:
            response = request_with_retries(self.client, "POST", self.settings.apollo_search_url,
                                            json=body, headers=headers)
        except FetchError as exc:
            raise ProviderError(f"Apollo: {exc}") from exc
        if response.status_code >= 400:
            raise ProviderError(f"Apollo: HTTP {response.status_code} {response.text[:200]}")
        payload = response.json()
        contacts = []
        for person in (payload.get("people") or []) + (payload.get("contacts") or []):
            name = person.get("name") or f"{person.get('first_name', '')} {person.get('last_name', '')}".strip()
            email = person.get("email")
            if not is_valid_email(email) or "not_unlocked" in (email or "") or (email or "").endswith("@domain.com"):
                email = None
            if not name:
                continue
            contacts.append(ContactCandidate(
                name=name,
                role=person.get("title"),
                email=email,
                email_confidence=0.9 if email else 0.0,
                email_source="apollo" if email else None,
                linkedin_url=normalize_linkedin(person.get("linkedin_url")),
                source=self.name,
            ))
        return contacts


class SearchProvider(ContactProvider):
    max_queries: ClassVar[int] = 2
    pause_seconds: ClassVar[float] = 1.0

    @abstractmethod
    def search(self, query: str, count: int = 10) -> list[SearchResult]: ...

    def find_contacts(self, ctx: ProspectContext) -> list[ContactCandidate]:
        results: list[SearchResult] = []
        for i, (_, query) in enumerate(build_queries(ctx)[: self.max_queries]):
            if i:
                fetch.sleep(self.pause_seconds)
            results.extend(self.search(query))
        return parse_linkedin_results(results, ctx, self.name)


class BraveSearchProvider(SearchProvider):
    name = "brave"
    requires = ("brave_search_api_key",)
    URL = "https://api.search.brave.com/res/v1/web/search"

    def search(self, query: str, count: int = 10) -> list[SearchResult]:
        headers = {"X-Subscription-Token": self.settings.brave_search_api_key, "Accept": "application/json"}
        try:
            payload = get_json(self.client, self.URL, params={"q": query, "count": min(count, 20)}, headers=headers)
        except FetchError as exc:
            raise ProviderError(f"Brave: {exc}") from exc
        results = []
        for item in (payload.get("web") or {}).get("results") or []:
            snippet = BeautifulSoup(item.get("description") or "", "html.parser").get_text(" ", strip=True)
            results.append(SearchResult(item.get("title", ""), item.get("url", ""), snippet))
        return results


class GoogleCSEProvider(SearchProvider):
    name = "google_cse"
    requires = ("google_cse_api_key", "google_cse_id")
    URL = "https://www.googleapis.com/customsearch/v1"

    def search(self, query: str, count: int = 10) -> list[SearchResult]:
        params = {"key": self.settings.google_cse_api_key, "cx": self.settings.google_cse_id,
                  "q": query, "num": min(count, 10)}
        try:
            payload = get_json(self.client, self.URL, params=params)
        except FetchError as exc:
            raise ProviderError(f"Google CSE: {exc}") from exc
        return [SearchResult(i.get("title", ""), i.get("link", ""), i.get("snippet", "")) for i in payload.get("items") or []]


class DuckDuckGoProvider(SearchProvider):
    """DuckDuckGo's HTML endpoint (no key). Honours robots.txt when RESPECT_ROBOTS_TXT=true."""

    name = "duckduckgo"
    URL = "https://html.duckduckgo.com/html/"
    pause_seconds = 2.0

    def __init__(self, client, settings):
        super().__init__(client, settings)
        self.robots = RobotsCache(settings.http_user_agent)

    def search(self, query: str, count: int = 10) -> list[SearchResult]:
        url = f"{self.URL}?q={quote_plus(query)}"
        if self.settings.respect_robots_txt and not self.robots.allowed(self.client, url):
            raise ProviderError("DuckDuckGo: robots.txt disallows automated queries - use the search links or an API provider")
        try:
            response = request_with_retries(self.client, "GET", self.URL, params={"q": query},
                                            headers={"Accept": "text/html"})
        except FetchError as exc:
            raise ProviderError(f"DuckDuckGo: {exc}") from exc
        if response.status_code == 202 or "anomaly" in response.text[:5000].lower():
            raise ProviderError("DuckDuckGo: rate-limited (bot challenge) - try later or use an API provider")
        if response.status_code >= 400:
            raise ProviderError(f"DuckDuckGo: HTTP {response.status_code}")
        return parse_duckduckgo_html(response.text)[:count]


def parse_duckduckgo_html(html: str) -> list[SearchResult]:
    soup = BeautifulSoup(html, "html.parser")
    results = []
    for anchor in soup.select("a.result__a"):
        href = anchor.get("href", "")
        query = parse_qs(urlparse(href).query)
        url = unquote(query["uddg"][0]) if "uddg" in query else href
        container = anchor.find_parent(class_="result") or anchor.parent
        snippet_tag = container.select_one(".result__snippet") if container else None
        results.append(SearchResult(anchor.get_text(" ", strip=True), url,
                                    snippet_tag.get_text(" ", strip=True) if snippet_tag else ""))
    return results


PROVIDERS: dict[str, type[ContactProvider]] = {
    cls.name: cls for cls in (HunterProvider, ApolloProvider, BraveSearchProvider, GoogleCSEProvider, DuckDuckGoProvider)
}


def build_providers(settings: Settings, client: httpx.Client, names: list[str] | None = None) -> list[ContactProvider]:
    names = names if names is not None else settings.contact_providers
    providers = []
    for name in names:
        cls = PROVIDERS.get(name)
        if cls is not None:
            providers.append(cls(client, settings))
    return providers
