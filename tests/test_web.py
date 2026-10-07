from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from referralpilot.db import session_scope
from referralpilot.models import ContactStatus, Job, JobStatus, OutreachLog, OutreachStatus, ReferralContact

HX = {"HX-Request": "true"}


@pytest.fixture()
def client(demo_jobs, monkeypatch):
    from referralpilot.demo import enable_demo_env
    from referralpilot.ui.app import create_app

    enable_demo_env()  # mock APIs + demo provider keys for "Find contacts"
    monkeypatch.setenv("LATEX_ENGINE", "fpdf")  # fast, always available
    from referralpilot.config import reset_settings

    reset_settings()
    with TestClient(create_app(start_scheduler=False, init_logging=False), base_url="http://127.0.0.1:8000") as test_client:
        yield test_client


def _trigger(response) -> dict:
    return json.loads(response.headers.get("HX-Trigger", "{}"))


def test_pages_render(client):
    for path in ["/", "/outbox", "/companies", "/profile", "/logs", "/partials/status"]:
        response = client.get(path)
        assert response.status_code == 200, path
    board = client.get("/partials/board").text
    for label in ["Discovered", "Matched / Tailored", "Outreach Queued", "Contacted", "Follow-Up Sent", "Replied / Referred"]:
        assert label in board
    assert "Software Engineer, New Grad" in board


def test_board_filters(client):
    html = client.get("/partials/board", params={"company": "Meesho"}).text
    assert "SDE-1 (Backend)" in html and "Stripe" not in html
    html = client.get("/partials/board", params={"q": "frontend"}).text
    assert "Frontend Engineer" in html and "SDE-1" not in html


def test_state_changes_require_htmx_same_origin(client, demo_jobs):
    job_id = demo_jobs["Stripe|Frontend Engineer"]
    assert client.post(f"/jobs/{job_id}/status", data={"status": "archived"}).status_code == 403
    cross = client.post(f"/jobs/{job_id}/status", data={"status": "archived"},
                        headers={**HX, "Origin": "http://evil.example"})
    assert cross.status_code == 403
    ok = client.post(f"/jobs/{job_id}/status", data={"status": "archived"}, headers=HX)
    assert ok.status_code == 204 and "refreshBoard" in _trigger(ok)
    with session_scope() as session:
        assert session.get(Job, job_id).status == JobStatus.ARCHIVED


def test_full_ui_flow_tailor_prospect_draft_approve(client, demo_jobs):
    job_id = demo_jobs["Postman|Software Engineer I - Backend"]
    drawer = client.get(f"/jobs/{job_id}")
    assert drawer.status_code == 200 and "Compile tailored resume" in drawer.text

    tailored = client.post(f"/jobs/{job_id}/tailor", headers=HX)
    assert tailored.status_code == 200 and "View PDF" in tailored.text
    assert "compiled with fpdf" in _trigger(tailored)["toast"]["message"]
    pdf = client.get(f"/jobs/{job_id}/resume.pdf")
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"
    assert client.get(f"/jobs/{job_id}/resume.tex").text.startswith("\\documentclass")

    found = client.post(f"/jobs/{job_id}/prospect", headers=HX)
    assert found.status_code == 200 and "postman.com" in found.text
    with session_scope() as session:
        contact = session.exec(select(ReferralContact).where(ReferralContact.job_id == job_id)
                               .order_by(ReferralContact.priority_score.desc())).first()
        contact_id = contact.id

    editor = client.post(f"/contacts/{contact_id}/draft", headers=HX)
    assert editor.status_code == 200 and "Approve &amp; queue" in editor.text
    with session_scope() as session:
        draft = session.exec(select(OutreachLog).where(OutreachLog.contact_id == contact_id)).one()
        draft_id, body = draft.id, draft.body

    saved = client.post(f"/outreach/{draft_id}", headers=HX,
                        data={"subject": "Edited subject", "body": body, "to_email": draft.to_email, "approve": "true"})
    assert saved.status_code == 200 and "closeModal" in _trigger(saved)
    with session_scope() as session:
        item = session.get(OutreachLog, draft_id)
        assert item.status == OutreachStatus.QUEUED and item.subject == "Edited subject"
        assert session.get(Job, job_id).status == JobStatus.QUEUED
    outbox = client.get("/outbox").text
    assert "Edited subject" in outbox and "Send queue" in outbox


def test_invalid_edits_are_rejected(client, demo_jobs):
    job_id = demo_jobs["Stripe|Software Engineer, New Grad"]
    with session_scope() as session:
        contact = ReferralContact(job_id=job_id, name="Test Person", email="test@stripe.com", email_confidence=1.0)
        session.add(contact)
        session.flush()
        contact_id = contact.id
    client.post(f"/contacts/{contact_id}/draft", headers=HX)
    with session_scope() as session:
        draft_id = session.exec(select(OutreachLog.id).where(OutreachLog.contact_id == contact_id)).one()
    bad = client.post(f"/outreach/{draft_id}", headers=HX, data={"subject": "x", "body": "y", "to_email": "not-an-email"})
    assert bad.status_code == 204 and _trigger(bad)["toast"]["kind"] == "error"

    opted = client.post(f"/contacts/{contact_id}/opt-out", headers=HX)
    assert opted.status_code == 200
    with session_scope() as session:
        assert session.get(ReferralContact, contact_id).status == ContactStatus.OPTED_OUT
        assert session.get(OutreachLog, draft_id).status == OutreachStatus.CANCELLED
    again = client.post(f"/outreach/{draft_id}/approve", headers=HX)
    assert _trigger(again)["toast"]["kind"] == "error"


def test_manual_contact_and_pause_controls(client, demo_jobs):
    job_id = demo_jobs["Meesho|SDE-1 (Backend)"]
    added = client.post(f"/jobs/{job_id}/contacts", headers=HX, data={"name": "Kiran Rao", "role": "SDE 1"})
    assert added.status_code == 200 and "kiran.rao@meesho.com" in added.text
    paused = client.post("/outbox/pause", headers=HX)
    assert paused.status_code == 204
    assert "Sending paused" in client.get("/partials/status").text
    client.post("/outbox/resume", headers=HX)
    assert client.get("/api/stats").json()["queue"]["paused"] is False


def test_profile_editor_validates(client):
    bad = client.post("/profile", headers=HX, data={"profile_json": "{not json"})
    assert "Invalid JSON" in bad.text and _trigger(bad)["toast"]["kind"] == "error"
    missing = client.post("/profile", headers=HX, data={"profile_json": json.dumps({"email": "x@y.z"})})
    assert "full_name" in missing.text
    good = client.post("/profile", headers=HX, data={"profile_json": json.dumps({
        "full_name": "New Name", "email": "new@example.com", "skills": {"Languages": ["Go"]}})})
    assert _trigger(good)["toast"]["kind"] == "success"


def test_companies_crud(client):
    added = client.post("/companies", headers=HX, data={"name": "Acme", "ats_type": "lever", "board_token": "acme",
                                                        "domain": "acme.io"})
    assert added.status_code == 200 and "acme.io" in added.text
    bad = client.post("/companies", headers=HX, data={"name": "Bad", "ats_type": "workday", "board_token": "x"})
    assert _trigger(bad)["toast"]["kind"] == "error"


def test_log_stream_helper_returns_new_rows(client):
    from referralpilot.activity import get_logger, setup_logging, shutdown_logging, flush_logging
    from referralpilot.ui.routes import _fetch_logs

    setup_logging("INFO", console=False)
    try:
        get_logger("harvester").info("stream test row", extra={"job_id": 1})
        flush_logging()
        rows = _fetch_logs(0)
        assert rows[-1]["message"] == "stream test row" and rows[-1]["source"] == "harvester"
        assert _fetch_logs(rows[-1]["id"]) == []
    finally:
        shutdown_logging()


def test_contact_with_unsent_draft_can_be_removed(client, demo_jobs):
    job_id = demo_jobs["Stripe|Software Engineer, New Grad"]
    with session_scope() as session:
        contact = ReferralContact(job_id=job_id, name="Remove Me", email="remove@stripe.com", email_confidence=1.0)
        session.add(contact)
        session.flush()
        contact_id = contact.id
    client.post(f"/contacts/{contact_id}/draft", headers=HX)
    removed = client.post(f"/contacts/{contact_id}/delete", headers=HX)
    assert removed.status_code == 200 and _trigger(removed)["toast"]["message"] == "Contact removed"
    with session_scope() as session:
        assert session.get(ReferralContact, contact_id) is None
        assert not session.exec(select(OutreachLog).where(OutreachLog.contact_id == contact_id)).all()
