"""The browser (GitHub Pages) build: the web.py runtime, browser settings, backups,
job snapshots, the Gmail REST sender and the XMLHttpRequest transport."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import types

import httpx
import pytest
from sqlmodel import select

from referralpilot.db import session_scope
from referralpilot.models import Company, Job, OutreachLog, OutreachStatus, ReferralContact

HX = [["HX-Request", "true"]]


@pytest.fixture()
def browser(workspace):
    """Bootstrap web.py the way the page's worker does (offline: demo API responses)."""
    import anyio.to_thread

    from referralpilot import runtime, web
    from referralpilot.config import reset_settings
    from referralpilot.outreach import gmail_web

    saved_env = dict(os.environ)
    original_run_sync = anyio.to_thread.run_sync
    info = web.bootstrap({"DEMO_MODE": "true", "APP_TIMEZONE": "Asia/Kolkata", "LATEX_ENGINE": "fpdf",
                          "SITE_URL": "https://example.github.io/Referral-Pilot/"})
    yield info
    gmail_web.set_token(None)
    runtime.set_background_runner(None)
    anyio.to_thread.run_sync = original_run_sync
    web._pending.clear()
    web._current = None
    web._app = None
    os.environ.clear()
    os.environ.update(saved_env)
    reset_settings()


def call(method: str, url: str, headers=(), body: bytes = b""):
    from referralpilot import web

    status, response_headers, content = asyncio.run(web.handle(method, url, list(headers), body))
    return status, {k.lower(): v for k, v in response_headers}, content


def run_background() -> int:
    from referralpilot import web

    steps = 0
    while web.background_pending():
        web.run_task("background")
        steps += 1
        assert steps < 100
    return steps


def test_bootstrap_serves_pages_without_threads(browser):
    from referralpilot.config import get_settings

    settings = get_settings()
    assert browser["first_run"] is True and browser["email_backend"] == "dry_run"
    assert settings.web_mode and not settings.sqlite_wal and settings.app_timezone == "Asia/Kolkata"
    for path in ["/", "/outbox", "/companies", "/profile", "/logs", "/settings", "/partials/board",
                 "/partials/status"]:
        status, _, body = call("GET", path)
        assert status == 200, path
    status, _, body = call("GET", "/settings")
    assert b"Connect Gmail" in body and b"Download backup" in body
    status, _, body = call("GET", "/logs/poll?backlog=5")
    rows = json.loads(body)["rows"]
    assert any("Browser edition" in row["message"] for row in rows)
    last = rows[-1]["id"]
    assert json.loads(call("GET", f"/logs/poll?after={last}")[2])["rows"] == []


def test_unsafe_requests_still_need_htmx(browser):
    assert call("POST", "/outbox/pause")[0] == 403
    status, headers, _ = call("POST", "/outbox/pause", HX)
    assert status == 204 and "refreshStatus" in headers["hx-trigger"]
    assert b"Sending paused" in call("GET", "/partials/status")[2]


def test_harvest_runs_stepwise_between_requests(browser):
    from referralpilot.pipeline import harvest_running

    status, headers, _ = call("POST", "/harvest", HX)
    assert status == 204 and "Harvest started" in headers["hx-trigger"]
    assert harvest_running()
    assert call("POST", "/harvest", HX)[1]["hx-trigger"].count("already running") == 1
    steps = run_background()
    assert steps >= 4  # one step per enabled company plus scoring
    assert not harvest_running()
    with session_scope() as session:
        jobs = session.exec(select(Job)).all()
        assert {job.company for job in jobs} >= {"Stripe", "Postman", "Meesho"}
        assert all(job.match_score is not None for job in jobs)  # analysed after the harvest
        palantir = session.exec(select(Company).where(Company.name == "Palantir")).one()
        assert palantir.last_harvest_status.startswith("error")  # no demo fixture and no snapshot


def test_periodic_tasks_keep_their_schedule(browser):
    from referralpilot import web

    first = web.run_task("periodic")
    assert set(first["ran"]) == {"harvest", "replies", "followups", "prune"} and first["more"]
    run_background()
    assert web.run_task("periodic")["ran"] == []  # nothing due again yet
    assert web.run_task("send")["status"] in {"idle", "outside_window"}
    status, _, body = call("GET", "/logs")
    assert status == 200 and b"Crawl job boards" in body


def test_settings_are_validated_saved_and_applied(browser):
    from referralpilot import websettings
    from referralpilot.config import get_settings

    values = websettings.current_values()
    bad = {**values, "send_window_start": "25:99"}
    status, headers, body = call("POST", "/settings", [*HX, ["Content-Type", "application/x-www-form-urlencoded"]],
                                 httpx.QueryParams(bad).__str__().encode())
    assert status == 200 and b"Please fix" in body and "error" in headers["hx-trigger"]

    good = {**values, "email_backend": "gmail_web", "gmail_client_id": "123.apps.googleusercontent.com",
            "daily_send_limit": "50", "send_delay_min_seconds": "10"}
    good.pop("include_internships")  # unchecked box
    status, headers, _ = call("POST", "/settings", [*HX, ["Content-Type", "application/x-www-form-urlencoded"]],
                              str(httpx.QueryParams(good)).encode())
    assert status == 200 and "Settings saved" in headers["hx-trigger"]
    settings = get_settings()
    assert settings.email_backend == "gmail_web" and settings.gmail_client_id.startswith("123.")
    assert settings.daily_send_limit == 20 and settings.send_delay_min_seconds == 180  # hard caps hold
    assert settings.include_internships is False and settings.app_timezone == "Asia/Kolkata"
    with session_scope() as session:
        assert websettings.load(session)["email_backend"] == "gmail_web"
    status, _, body = call("GET", "/partials/status")
    assert b"Connect Gmail" in body  # gmail_web without a token blocks sending


def test_backup_restore_and_reset(browser):
    from referralpilot.backup import SQLITE_MAGIC

    call("POST", "/companies", [*HX, ["Content-Type", "application/x-www-form-urlencoded"]],
         b"name=Acme&ats_type=lever&board_token=acme&domain=acme.io")
    status, headers, backup = call("GET", "/settings/backup.db")
    assert status == 200 and backup.startswith(SQLITE_MAGIC) and "attachment" in headers["content-disposition"]

    status, headers, _ = call("POST", "/settings/reset", HX)
    assert status == 204 and "reloadPage" in headers["hx-trigger"]
    with session_scope() as session:
        assert session.exec(select(Company).where(Company.name == "Acme")).first() is None

    status, headers, _ = call("POST", "/settings/restore", [*HX, ["Content-Type", "application/octet-stream"]],
                              b"definitely not sqlite")
    assert "not a ReferralPilot backup" in headers["hx-trigger"]
    status, headers, _ = call("POST", "/settings/restore", [*HX, ["Content-Type", "application/octet-stream"]], backup)
    assert "Backup restored" in headers["hx-trigger"]
    with session_scope() as session:
        assert session.exec(select(Company).where(Company.name == "Acme")).one().domain == "acme.io"


def test_manual_send_counts_toward_the_daily_limit(browser, monkeypatch):
    from referralpilot import pipeline

    run_background()
    pipeline.start_harvest(None)
    run_background()
    with session_scope() as session:
        job = session.exec(select(Job).where(Job.company == "Stripe")).first()
        contact = ReferralContact(job_id=job.id, name="Priya Shah", email="priya@stripe.com", email_confidence=0.9)
        session.add(contact)
        session.flush()
        contact_id = contact.id
    status, _, body = call("POST", f"/contacts/{contact_id}/draft", HX)
    assert status == 200 and b"Open in Gmail" in body and b"Mark as sent" in body
    with session_scope() as session:
        draft = session.exec(select(OutreachLog).where(OutreachLog.contact_id == contact_id)).one()
    form = str(httpx.QueryParams({"subject": draft.subject, "body": draft.body, "to_email": draft.to_email,
                                  "mark_sent": "true"})).encode()
    status, headers, _ = call("POST", f"/outreach/{draft.id}",
                              [*HX, ["Content-Type", "application/x-www-form-urlencoded"]], form)
    assert "Recorded as sent" in headers["hx-trigger"]
    with session_scope() as session:
        item = session.get(OutreachLog, draft.id)
        assert item.status == OutreachStatus.SENT and item.dry_run is False and item.sent_at is not None
        assert session.get(ReferralContact, contact_id).status == "contacted"

    monkeypatch.setenv("DAILY_SEND_LIMIT", "1")
    from referralpilot.config import reset_settings

    reset_settings()
    with session_scope() as session:
        second = ReferralContact(job_id=job.id, name="Arjun Rao", email="arjun@stripe.com", email_confidence=0.9)
        session.add(second)
        session.flush()
        second_id = second.id
    call("POST", f"/contacts/{second_id}/draft", HX)
    with session_scope() as session:
        draft_id = session.exec(select(OutreachLog.id).where(OutreachLog.contact_id == second_id)).one()
    status, headers, _ = call("POST", f"/outreach/{draft_id}/mark-sent", HX)
    assert "Daily limit reached" in headers["hx-trigger"]


def test_snapshot_round_trip_and_fallback(workspace, demo_jobs):
    from referralpilot.harvester import JobFilter
    from referralpilot.harvester.service import HarvestStats, harvest_company
    from referralpilot.harvester.snapshot import Snapshot, export_snapshot

    with session_scope() as session:
        data = export_snapshot(session, [HarvestStats(company="Ramp", ats_type="ashby", error="boom")])
    assert data["version"] == 1 and len(data["jobs"]) == len(demo_jobs)
    assert {c["name"]: c["ok"] for c in data["companies"]}["Ramp"] is False
    assert not any("contact" in key or "profile" in key for key in data)
    snapshot = Snapshot(json.loads(json.dumps(data)))

    def blocked(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("blocked by CORS", request=request)

    with session_scope() as session:
        for job in session.exec(select(Job)).all():
            session.delete(job)
    with session_scope() as session, httpx.Client(transport=httpx.MockTransport(blocked)) as client:
        stripe = session.exec(select(Company).where(Company.name == "Stripe")).one()
        stats = harvest_company(session, stripe, client=client, job_filter=JobFilter.from_settings(),
                                fallback=snapshot.jobs_for)
        assert stats.error is None and stats.new > 0
        assert stripe.last_harvest_status.endswith("(the scheduled crawl from <1h ago)")
        ramp = session.exec(select(Company).where(Company.name == "Ramp")).one()
        failed = harvest_company(session, ramp, client=client, job_filter=JobFilter.from_settings(),
                                 fallback=snapshot.jobs_for)
        assert failed.error and failed.new == 0  # the crawl failed for Ramp: nothing to fall back on


def test_export_jobs_cli(workspace, monkeypatch, tmp_path, capsys):
    from referralpilot.cli import main as cli_main
    from referralpilot.config import reset_settings

    monkeypatch.setenv("DEMO_MODE", "true")
    reset_settings()
    out = tmp_path / "site" / "jobs.json"
    assert cli_main(["export-jobs", "--out", str(out), "--all-companies"]) == 0
    data = json.loads(out.read_text())
    crawled = {c["name"] for c in data["companies"] if c["ok"]}
    assert {"Stripe", "Postman", "Meesho", "Ramp"} <= crawled and "Databricks" not in crawled
    assert data["jobs"] and {"company", "external_id", "title", "url", "description"} <= set(data["jobs"][0])
    assert "Wrote" in capsys.readouterr().out


def test_gmail_web_sender_and_queue_blocking(workspace, monkeypatch):
    from referralpilot.outreach import gmail_web
    from referralpilot.outreach.queue import OutreachQueue
    from referralpilot.outreach.senders import SendError, build_message

    sender = gmail_web.GmailWebSender()
    assert sender.blocked_reason() == "Connect Gmail to start sending"
    assert OutreachQueue(sender).tick().status == "blocked"

    seen: list[httpx.Request] = []

    def gmail_api(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/messages/send"):
            if request.headers["Authorization"] != "Bearer good-token":
                return httpx.Response(401, json={"error": {"message": "Invalid Credentials"}})
            return httpx.Response(200, json={"id": "m1", "threadId": "t1"})
        return httpx.Response(200, json={"payload": {"headers": [{"name": "Message-Id", "value": "<gmail@x>"}]}})

    monkeypatch.setattr(gmail_web, "_client", lambda: httpx.Client(transport=httpx.MockTransport(gmail_api)))
    message = build_message(sender_name="A", sender_email="a@gmail.com", to_email="b@stripe.com", to_name="B",
                            subject="Hi", body="Hello", message_id="<local@x>")
    gmail_web.set_token("good-token", 3600, "a@gmail.com")
    assert sender.blocked_reason() is None and sender.account_email == "a@gmail.com"
    result = sender.send(message, thread_id="t0")
    assert (result.message_id, result.thread_id, result.provider_id) == ("<gmail@x>", "t1", "m1")
    sent_body = json.loads(seen[0].content)
    assert sent_body["threadId"] == "t0" and sent_body["raw"]

    gmail_web.set_token("stale-token", 3600)
    with pytest.raises(SendError) as excinfo:
        sender.send(message)
    assert not excinfo.value.permanent and "reconnect" in str(excinfo.value)
    assert gmail_web.current_token() is None  # sending pauses until the user reconnects
    gmail_web.set_token("x", 30)
    assert gmail_web.current_token() is None  # tokens are treated as expired a minute early


class _FakeXHR:
    """Just enough of XMLHttpRequest for BrowserTransport."""

    instances: list[_FakeXHR] = []
    blocked_hosts: set[str] = set()

    def __init__(self):
        self.headers: dict[str, str] = {}
        self.status = 0
        self.response = None
        _FakeXHR.instances.append(self)

    @classmethod
    def new(cls):
        return cls()

    def open(self, method, url, is_async):
        assert is_async is False
        self.method, self.url = method, url

    def setRequestHeader(self, name, value):  # noqa: N802 (browser API)
        self.headers[name.lower()] = value

    def send(self, payload):
        self.payload = payload
        host = httpx.URL(self.url).host
        if host in self.blocked_hosts:
            raise RuntimeError("NetworkError: Failed to execute 'send'")
        self.status = 200
        body = b'{"ok": true}'
        self.response = types.SimpleNamespace(to_py=lambda: memoryview(body))

    def getAllResponseHeaders(self):  # noqa: N802
        return "content-type: application/json\r\ncontent-encoding: gzip\r\ncontent-length: 999\r\n"


class _FakeUint8Array:
    @staticmethod
    def new(size):
        array = types.SimpleNamespace(size=size, data=None)
        array.assign = lambda data: setattr(array, "data", bytes(data))
        return array


def test_browser_transport_uses_sync_xhr_and_optional_relay(monkeypatch):
    from referralpilot.fetch import BlockedRequestError, BrowserTransport

    monkeypatch.setitem(sys.modules, "js", types.SimpleNamespace(XMLHttpRequest=_FakeXHR, Uint8Array=_FakeUint8Array))
    _FakeXHR.instances.clear()
    _FakeXHR.blocked_hosts = {"api.lever.co"}

    with httpx.Client(transport=BrowserTransport(5), headers={"User-Agent": "x", "X-Api-Key": "k"}) as client:
        response = client.post("https://boards-api.greenhouse.io/v1/x", json={"a": 1})
        assert response.json() == {"ok": True} and "content-encoding" not in response.headers
        xhr = _FakeXHR.instances[-1]
        assert "user-agent" not in xhr.headers and xhr.headers["x-api-key"] == "k"
        assert xhr.payload.data == b'{"a":1}'
        with pytest.raises(BlockedRequestError):
            client.get("https://api.lever.co/v0/postings/x")

    relay = BrowserTransport(5, "https://relay.example/?url={url}")
    with httpx.Client(transport=relay) as client:
        assert client.get("https://api.lever.co/v0/postings/x?mode=json").json() == {"ok": True}
        assert _FakeXHR.instances[-1].url == ("https://relay.example/?url="
                                              "https%3A%2F%2Fapi.lever.co%2Fv0%2Fpostings%2Fx%3Fmode%3Djson")
        _FakeXHR.blocked_hosts = {"gmail.googleapis.com"}
        with pytest.raises(BlockedRequestError):  # Google APIs are never sent through a relay
            client.get("https://gmail.googleapis.com/gmail/v1/users/me/profile")
