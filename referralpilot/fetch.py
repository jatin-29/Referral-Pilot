"""Shared HTTP client with polite defaults, retries and a robots.txt cache."""

from __future__ import annotations

import time
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

from .config import Settings, get_settings

RETRY_STATUSES = {429, 500, 502, 503, 504}

# Indirection so tests can disable real sleeping.
sleep = time.sleep


class FetchError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def build_client(settings: Settings | None = None, *, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    settings = settings or get_settings()
    if transport is None and settings.demo_mode:
        from .demo import demo_transport

        transport = demo_transport()
    return httpx.Client(
        timeout=httpx.Timeout(settings.http_timeout_seconds),
        headers={
            "User-Agent": settings.http_user_agent,
            "Accept": "application/json, text/html;q=0.9, */*;q=0.5",
        },
        follow_redirects=True,
        transport=transport,
    )


def request_with_retries(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    retries: int | None = None,
    backoff: float = 1.5,
    **kwargs,
) -> httpx.Response:
    """Issue a request, retrying transport errors, 429s and 5xx with backoff."""
    retries = get_settings().http_max_retries if retries is None else retries
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            response = client.request(method, url, **kwargs)
        except httpx.TransportError as exc:
            last_error = exc
            if attempt < retries:
                sleep(backoff * (2**attempt))
                continue
            raise FetchError(f"{method} {url} failed: {exc}") from exc
        if response.status_code in RETRY_STATUSES and attempt < retries:
            retry_after = response.headers.get("Retry-After", "")
            delay = float(retry_after) if retry_after.isdigit() else backoff * (2**attempt)
            sleep(min(delay, 30.0))
            continue
        return response
    raise FetchError(f"{method} {url} failed: {last_error}")


def get_json(client: httpx.Client, url: str, **kwargs):
    response = request_with_retries(client, "GET", url, **kwargs)
    if response.status_code == 404:
        raise FetchError(f"Not found (404): {url}", 404)
    if response.status_code >= 400:
        raise FetchError(f"HTTP {response.status_code} from {url}", response.status_code)
    try:
        return response.json()
    except ValueError as exc:
        raise FetchError(f"Invalid JSON from {url}") from exc


class RobotsCache:
    """Caches robots.txt per host; used for HTML (non-API) scraping only."""

    def __init__(self, user_agent: str):
        self.user_agent = user_agent
        self._parsers: dict[str, RobotFileParser | None] = {}

    def allowed(self, client: httpx.Client, url: str) -> bool:
        parsed = urlparse(url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        if base not in self._parsers:
            self._parsers[base] = self._load(client, base)
        parser = self._parsers[base]
        return True if parser is None else parser.can_fetch(self.user_agent, url)

    def _load(self, client: httpx.Client, base: str) -> RobotFileParser | None:
        try:
            response = client.get(f"{base}/robots.txt")
        except httpx.HTTPError:
            return None
        parser = RobotFileParser()
        if response.status_code >= 500:
            parser.disallow_all = True
            return parser
        if response.status_code >= 400:
            return None  # no robots.txt: everything allowed
        parser.parse(response.text.splitlines())
        return parser
