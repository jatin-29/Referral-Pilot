from __future__ import annotations

import httpx
import pytest
from sqlmodel import select

from referralpilot.config import get_settings
from referralpilot.db import session_scope
from referralpilot.demo import demo_client
from referralpilot.fetch import build_client
from referralpilot.models import ContactStatus, Job, ReferralContact, Suppression
from referralpilot.prospector import (
    add_manual_contact,
    build_queries,
    domain_from_url,
    generate_candidates,
    infer_pattern,
    prospect_job,
    registrable_domain,
    resolve_domain,
    split_name,
)
from referralpilot.prospector.providers import (
    DuckDuckGoProvider,
    ProspectContext,
    SearchResult,
    build_providers,
    parse_duckduckgo_html,
    parse_linkedin_results,
)


@pytest.mark.parametrize(
    "url,domain",
    [
        ("https://stripe.com/jobs/listing/software-engineer/123", "stripe.com"),
        ("https://careers.tata.co.in/job/1", "tata.co.in"),
        ("https://boards.greenhouse.io/stripe/jobs/1", None),
        ("https://jobs.lever.co/meesho/abc", None),
        ("https://www.ycombinator.com/companies/x/jobs/1", None),
        (None, None),
    ],
)
def test_domain_from_url(url, domain):
    assert domain_from_url(url) == domain


def test_registrable_domain_and_resolution():
    assert registrable_domain("www.eng.example.co.uk") == "example.co.uk"
    assert resolve_domain("Stripe", configured="Stripe.com") == ("stripe.com", "config")
    assert resolve_domain("Loop Labs", posting_url="https://jobs.ashbyhq.com/loop", use_dns=False) == ("looplabs.com", "guess")


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Priya Sharma", ("priya", "sharma")),
        ("Dr. Jane O'Brien-Smith, PhD (She/Her)", ("jane", "obriensmith")),
        ("José Álvarez", ("jose", "alvarez")),
        ("Ravi", ("ravi", "")),
        ("Anil Kumar Jr.", ("anil", "kumar")),
    ],
)
def test_split_name(name, expected):
    assert split_name(name) == expected


def test_generate_candidates_prefers_known_pattern():
    default = generate_candidates("Priya Sharma", "stripe.com")
    assert default[0].email == "priya.sharma@stripe.com"
    known = generate_candidates("Priya Sharma", "stripe.com", known_pattern="{f}{last}")
    assert known[0].email == "psharma@stripe.com" and known[0].confidence == pytest.approx(0.85)
    single = generate_candidates("Ravi", "x.com")
    assert all("{" not in g.email for g in single) and single[0].email == "ravi@x.com"


def test_infer_pattern():
    pattern, support = infer_pattern([
        ("Rahul Verma", "rverma@x.com"), ("Ananya Iyer", "aiyer@x.com"), ("Jo Bloggs", "jo@x.com"),
    ])
    assert pattern == "{f}{last}" and support == pytest.approx(2 / 3)


def test_queries_prioritise_engineers_then_alumni():
    queries = build_queries(ProspectContext(company="Stripe", domain="stripe.com", institutions=["DTU"]))
    assert queries[0][1] == 'site:linkedin.com/in "Stripe" ("Software Engineer" OR "SDE")'
    assert queries[1][1] == 'site:linkedin.com/in "Stripe" "DTU"'


def test_linkedin_result_parsing():
    ctx = ProspectContext(company="Stripe", domain="stripe.com", institutions=["Delhi Technological University"])
    results = [
        SearchResult("Priya Sharma - Software Engineer - Stripe | LinkedIn", "https://in.linkedin.com/in/priya-s?trk=x",
                     "Education: Delhi Technological University"),
        SearchResult("Vikram Singh - Senior Engineer - OtherCorp | LinkedIn", "https://www.linkedin.com/in/vikram",
                     "Previously at Stripe"),
        SearchResult("Stripe | LinkedIn", "https://www.linkedin.com/company/stripe", "Company page"),
        SearchResult("Arjun Nair | LinkedIn", "https://www.linkedin.com/in/arjun", "SDE 1 at Stripe · Bengaluru"),
    ]
    contacts = parse_linkedin_results(results, ctx, "test")
    assert [c.name for c in contacts] == ["Priya Sharma", "Arjun Nair"]
    assert contacts[0].is_alumni and contacts[0].linkedin_url == "https://www.linkedin.com/in/priya-s"
    assert contacts[1].role == "SDE 1"


def test_duckduckgo_html_parsing():
    from referralpilot.demo import DEMO_DIR

    html = (DEMO_DIR / "duckduckgo_results.html").read_text().replace("{company}", "Stripe").replace("{slug}", "stripe")
    results = parse_duckduckgo_html(html)
    assert len(results) == 6
    assert results[0].url == "https://in.linkedin.com/in/priya-sharma-demo"
    assert "Delhi Technological University" in results[0].snippet


def test_duckduckgo_rate_limit_is_reported(workspace):
    from referralpilot.prospector.providers import ProviderError

    transport = httpx.MockTransport(lambda r: httpx.Response(202, text="anomaly detected"))
    with build_client(transport=transport) as client:
        provider = DuckDuckGoProvider(client, get_settings())
        with pytest.raises(ProviderError, match="rate-limited"):
            provider.search('site:linkedin.com/in "Stripe"')


def test_prospect_job_with_hunter_and_duckduckgo(demo_jobs, monkeypatch, workspace):
    monkeypatch.setenv("HUNTER_API_KEY", "demo")
    from referralpilot.config import reset_settings

    reset_settings()
    settings = get_settings()
    job_id = demo_jobs["Stripe|Software Engineer, New Grad"]
    with demo_client() as client, session_scope() as session:
        job = session.get(Job, job_id)
        result = prospect_job(session, job, client=client,
                              providers=build_providers(settings, client, ["hunter", "duckduckgo"]))
        assert result.domain == "stripe.com" and result.domain_source == "config"
        assert result.pattern == "{first}.{last}" and result.pattern_source == "hunter"
        contacts = session.exec(select(ReferralContact).where(ReferralContact.job_id == job_id)
                                .order_by(ReferralContact.priority_score.desc())).all()
    assert len(contacts) == settings.max_contacts_per_job
    assert contacts[0].is_alumni  # alumni engineers rank first
    by_name = {c.name: c for c in contacts}
    assert by_name["Rahul Verma"].email_source == "hunter"
    assert by_name["Priya Sharma"].email == "priya.sharma@stripe.com"
    assert "Vikram Singh" not in by_name  # works elsewhere now
    # Re-running only adds people beyond the first batch (6 candidates, limit 5), never duplicates.
    for expected_new in (1, 0):
        with demo_client() as client, session_scope() as session:
            again = prospect_job(session, session.get(Job, job_id), client=client,
                                 providers=build_providers(settings, client, ["hunter", "duckduckgo"]))
        assert len(again.added) == expected_new and again.skipped_existing >= 5
    with session_scope() as session:
        emails = [c.email for c in session.exec(select(ReferralContact).where(ReferralContact.job_id == job_id)).all()]
    assert len(emails) == len(set(emails)) == 6


def test_guessed_domain_lowers_confidence(demo_jobs):
    job_id = demo_jobs["Ledgerly|Full Stack Engineer"]
    with demo_client() as client, session_scope() as session:
        job = session.get(Job, job_id)
        result = prospect_job(session, job, client=client,
                              providers=build_providers(get_settings(), client, ["duckduckgo"]))
        assert result.domain_source == "guess"
        contacts = session.exec(select(ReferralContact).where(ReferralContact.job_id == job_id)).all()
    assert contacts and all(c.email_confidence < 0.2 for c in contacts)


def test_manual_contact_guesses_email_and_respects_suppression(demo_jobs):
    job_id = demo_jobs["Stripe|Frontend Engineer"]
    with session_scope() as session:
        session.add(Suppression(email="neha.gupta@stripe.com", reason="opt_out"))
    with session_scope() as session:
        job = session.get(Job, job_id)
        guessed = add_manual_contact(session, job, name="Neha Gupta", role="SDE 1")
        explicit = add_manual_contact(session, job, name="Kabir Rao", email="kabir@stripe.com")
        assert guessed.email == "neha.gupta@stripe.com" and guessed.email_source == "pattern"
        assert guessed.status == ContactStatus.OPTED_OUT  # suppressed address
        assert explicit.email_confidence == 1.0 and explicit.status == ContactStatus.NEW
