"""Shared HTTP client with polite defaults, retries and a robots.txt cache."""

from __future__ import annotations

import time
from urllib.parse import quote, urlparse
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


class BlockedRequestError(httpx.ConnectError):
    """The browser refused the request (CORS, offline...). Retrying cannot help."""


# Response headers the browser has already acted on (it decompresses bodies itself).
_DROP_RESPONSE_HEADERS = {"content-encoding", "content-length", "transfer-encoding", "connection"}
# Request headers a page may not set (https://fetch.spec.whatwg.org/#forbidden-request-header).
_FORBIDDEN_REQUEST_HEADERS = {
    "accept-charset", "accept-encoding", "access-control-request-headers", "access-control-request-method",
    "connection", "content-length", "cookie", "cookie2", "date", "dnt", "expect", "host", "keep-alive",
    "origin", "referer", "te", "trailer", "transfer-encoding", "upgrade", "user-agent", "via",
}
# Hosts that must never be relayed through a third-party CORS proxy (they send CORS headers anyway).
_NEVER_PROXY = ("googleapis.com", "google.com", "dns.google")
BLOCKED_MESSAGE = "the browser blocked the request (the site does not allow it, or you are offline)"


class BrowserTransport(httpx.BaseTransport):
    """httpx transport for the browser build: synchronous XMLHttpRequest in the page's Web Worker.

    Browsers enforce CORS, so a cross-origin API only answers when it sends
    Access-Control-Allow-Origin. `cors_proxy` (opt-in, e.g.
    "https://corsproxy.io/?url={url}") relays requests the browser blocked.
    """

    def __init__(self, timeout: float = 20.0, cors_proxy: str = ""):
        self.timeout = timeout
        self.cors_proxy = cors_proxy.strip()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        try:
            return self._send(request, url)
        except BlockedRequestError:
            proxied = self._proxied(url)
            if proxied is None:
                raise
            return self._send(request, proxied)

    def _proxied(self, url: str) -> str | None:
        host = urlparse(url).hostname or ""
        if not self.cors_proxy or any(host == h or host.endswith("." + h) for h in _NEVER_PROXY):
            return None
        if "{url}" in self.cors_proxy:
            return self.cors_proxy.replace("{url}", quote(url, safe=""))
        return self.cors_proxy + quote(url, safe="")

    def _send(self, request: httpx.Request, url: str) -> httpx.Response:
        from js import Uint8Array, XMLHttpRequest  # type: ignore[import-not-found]  # Pyodide only

        xhr = XMLHttpRequest.new()
        xhr.open(request.method, url, False)
        xhr.responseType = "arraybuffer"
        xhr.timeout = int(self.timeout * 1000)
        for name, value in request.headers.items():
            lowered = name.lower()
            if lowered in _FORBIDDEN_REQUEST_HEADERS or lowered.startswith(("sec-", "proxy-")):
                continue
            xhr.setRequestHeader(name, value)
        body = request.read()
        payload = None
        if body:
            payload = Uint8Array.new(len(body))
            payload.assign(body)
        try:
            xhr.send(payload)
        except Exception as exc:  # pyodide.ffi.JsException: NetworkError / TimeoutError
            raise BlockedRequestError(BLOCKED_MESSAGE, request=request) from exc
        if xhr.status == 0:
            raise BlockedRequestError(BLOCKED_MESSAGE, request=request)
        headers = []
        for line in str(xhr.getAllResponseHeaders() or "").split("\r\n"):
            name, sep, value = line.partition(":")
            if sep and name.strip().lower() not in _DROP_RESPONSE_HEADERS:
                headers.append((name.strip(), value.strip()))
        content = bytes(xhr.response.to_py()) if xhr.response else b""
        return httpx.Response(xhr.status, headers=headers, content=content, request=request)


def build_client(settings: Settings | None = None, *, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    settings = settings or get_settings()
    if transport is None and settings.demo_mode:
        from .demo import demo_transport

        transport = demo_transport()
    if transport is None and settings.web_mode:
        transport = BrowserTransport(settings.http_timeout_seconds, settings.cors_proxy)
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
            if attempt < retries and not isinstance(exc, BlockedRequestError):
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
