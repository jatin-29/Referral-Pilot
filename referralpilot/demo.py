"""Offline demo mode: an httpx MockTransport that serves recorded-style API payloads.

Used by the test-suite, `referralpilot verify --offline` and `referralpilot demo`
so the whole pipeline can run without network access.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import parse_qs, unquote

import httpx

from .textutil import slugify

DEMO_DIR = Path(__file__).resolve().parent / "demo_data"

DEMO_COMPANIES: list[dict] = [
    {"name": "Stripe", "ats_type": "greenhouse", "board_token": "stripe", "domain": "stripe.com", "tags": ["fintech"]},
    {"name": "Postman", "ats_type": "greenhouse", "board_token": "postman", "domain": "postman.com", "tags": ["devtools"]},
    {"name": "Meesho", "ats_type": "lever", "board_token": "meesho", "domain": "meesho.com", "tags": ["ecommerce"]},
    {"name": "Ramp", "ats_type": "ashby", "board_token": "ramp", "domain": "ramp.com", "tags": ["fintech", "yc"]},
    {"name": "YC startups - Software Engineer", "ats_type": "yc", "board_token": "software-engineer", "tags": ["yc"]},
]


def _file(name: str) -> Path | None:
    path = DEMO_DIR / name
    return path if path.exists() else None


def _json_response(path: Path | None, **replacements: str) -> httpx.Response:
    if path is None:
        return httpx.Response(404, json={"error": "Not Found"})
    text = path.read_text(encoding="utf-8")
    for key, value in replacements.items():
        text = text.replace("{" + key + "}", value)
    return httpx.Response(200, content=text.encode("utf-8"), headers={"Content-Type": "application/json"})


def _html_response(path: Path | None, **replacements: str) -> httpx.Response:
    if path is None:
        return httpx.Response(404, text="Not Found")
    text = path.read_text(encoding="utf-8")
    for key, value in replacements.items():
        text = text.replace("{" + key + "}", value)
    return httpx.Response(200, content=text.encode("utf-8"), headers={"Content-Type": "text/html; charset=utf-8"})


def _quoted_company(query: str) -> str:
    match = re.search(r'"([^"]+)"', query)
    return match.group(1) if match else "Acme"


def demo_handler(request: httpx.Request) -> httpx.Response:
    host = request.url.host
    parts = [unquote(p) for p in request.url.path.strip("/").split("/")]

    if host == "boards-api.greenhouse.io" and len(parts) >= 3:
        return _json_response(_file(f"greenhouse_{parts[2]}.json"))
    if host in {"api.lever.co", "api.eu.lever.co"} and len(parts) >= 3:
        return _json_response(_file(f"lever_{parts[2]}.json"))
    if host == "api.ashbyhq.com" and parts:
        return _json_response(_file(f"ashby_{parts[-1]}.json"))
    if host == "www.ycombinator.com":
        if parts == ["robots.txt"]:
            return httpx.Response(200, text=(DEMO_DIR / "yc_robots.txt").read_text())
        if parts[:2] == ["jobs", "role"]:
            return _html_response(_file("yc_listing.html"))
        if len(parts) >= 4 and parts[0] == "companies" and parts[2] == "jobs":
            job_key = parts[3].split("-")[0][:7]
            return _html_response(_file(f"yc_job_{job_key}.html"))
    if host in {"html.duckduckgo.com", "duckduckgo.com"}:
        if request.method == "POST":
            query = parse_qs(request.content.decode()).get("q", [""])[0]
        else:
            query = request.url.params.get("q", "")
        company = _quoted_company(query)
        return _html_response(_file("duckduckgo_results.html"), company=company, slug=slugify(company))
    if host == "api.hunter.io":
        domain = request.url.params.get("domain", "example.com")
        company = domain.split(".")[0].capitalize()
        return _json_response(_file("hunter_domain_search.json"), domain=domain, company=company)
    return httpx.Response(404, json={"error": f"no demo fixture for {request.url}"})


def demo_transport() -> httpx.MockTransport:
    return httpx.MockTransport(demo_handler)


def demo_client() -> httpx.Client:
    from .fetch import build_client

    return build_client(transport=demo_transport())


DEMO_ENV = {
    "DEMO_MODE": "true",
    "CONTACT_PROVIDERS": "hunter,duckduckgo",
    "HUNTER_API_KEY": "demo-key",
    "VERIFY_MX": "false",
}


def enable_demo_env() -> None:
    """Switch this process to offline demo mode (mock APIs, demo provider keys, dry-run)."""
    import os

    from .config import reset_settings

    os.environ.update(DEMO_ENV)
    os.environ["EMAIL_BACKEND"] = "dry_run"
    reset_settings()


def load_fixture_json(name: str):
    return json.loads((DEMO_DIR / name).read_text(encoding="utf-8"))
