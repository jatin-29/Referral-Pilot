from __future__ import annotations

import httpx
import pytest
from sqlmodel import select

from referralpilot.db import session_scope
from referralpilot.demo import demo_client, demo_transport
from referralpilot.fetch import build_client
from referralpilot.harvester import HarvestTarget, JobFilter, harvest
from referralpilot.harvester.ashby import AshbyHarvester
from referralpilot.harvester.base import RawJob
from referralpilot.harvester.filters import FilterRules
from referralpilot.harvester.greenhouse import GreenhouseHarvester
from referralpilot.harvester.lever import LeverHarvester
from referralpilot.harvester.yc import YCJobsHarvester, parse_listing_page
from referralpilot.models import Company, Job
from referralpilot.seed import upsert_company
from referralpilot.textutil import html_to_text, required_years


def test_greenhouse_parses_escaped_html(workspace):
    with demo_client() as client:
        jobs = GreenhouseHarvester(client).fetch(HarvestTarget("Stripe", "greenhouse", "stripe", domain="stripe.com"))
    assert len(jobs) == 7
    new_grad = next(j for j in jobs if j.title == "Software Engineer, New Grad")
    assert new_grad.external_id == "6001001"
    assert new_grad.location == "Bengaluru, India"
    assert "Minimum requirements" in new_grad.description
    assert "- Familiarity with SQL databases such as PostgreSQL or MySQL" in new_grad.description
    assert "<li>" not in new_grad.description and "&lt;" not in new_grad.description
    assert new_grad.posted_at is not None and new_grad.posted_at.tzinfo is not None


def test_lever_includes_list_sections(workspace):
    with demo_client() as client:
        jobs = LeverHarvester(client).fetch(HarvestTarget("Meesho", "lever", "meesho"))
    sde = next(j for j in jobs if j.title == "SDE-1 (Backend)")
    assert sde.url.startswith("https://jobs.lever.co/meesho/")
    assert sde.employment_type == "Full-time"
    assert "What you will need" in sde.description
    assert "0-2 years of experience" in sde.description


def test_ashby_skips_unlisted_and_merges_locations(workspace):
    with demo_client() as client:
        jobs = AshbyHarvester(client).fetch(HarvestTarget("Ramp", "ashby", "ramp"))
    assert [j.title for j in jobs] == ["Software Engineer, New Grad (2025)", "Senior Software Engineer, Backend"]
    assert jobs[0].location == "New York, NY / Remote (US)"
    assert "Kotlin" in jobs[0].description


@pytest.mark.parametrize("harvester, target", [
    (GreenhouseHarvester, HarvestTarget("Stripe", "greenhouse", "stripe")),
    (LeverHarvester, HarvestTarget("Meesho", "lever", "meesho")),
    (AshbyHarvester, HarvestTarget("Ramp", "ashby", "ramp")),
])
def test_title_prefilter_only_skips_work(workspace, harvester, target):
    """Postings whose titles the filter rejects keep no description; decisions are unchanged."""
    job_filter = JobFilter.from_settings()
    with demo_client() as client:
        full = harvester(client).fetch(target)
        quick = harvester(client).fetch(target, title_prefilter=job_filter.title_ok)
    assert [j.external_id for j in full] == [j.external_id for j in quick]
    assert [job_filter.evaluate(j) for j in full] == [job_filter.evaluate(j) for j in quick]
    assert all(j.description == "" for j in quick if not job_filter.title_ok(j.title))
    assert all(j.description for j in quick if job_filter.title_ok(j.title))


def test_yc_listing_uses_embedded_json_and_fetches_details(workspace):
    with demo_client() as client:
        jobs = YCJobsHarvester(client).fetch(HarvestTarget("YC", "yc", "software-engineer"))
    by_title = {j.title: j for j in jobs}
    assert by_title["Software Engineer (New Grad)"].company == "Loop Labs"
    assert "ClickHouse" in by_title["Software Engineer (New Grad)"].description
    assert "Experience: 3+ years" in by_title["Founding Backend Engineer"].description


def test_yc_falls_back_to_links():
    html = '<a href="/companies/acme/jobs/AbC123-backend-engineer">Backend Engineer</a><a href="/about">x</a>'
    listings = parse_listing_page(html)
    assert len(listings) == 1
    assert listings[0].company == "Acme" and listings[0].id == "AbC123"


def test_yc_respects_robots_txt(workspace):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /jobs\n")
        return httpx.Response(200, text="<html></html>")

    with build_client(transport=httpx.MockTransport(handler)) as client:
        from referralpilot.fetch import FetchError

        with pytest.raises(FetchError, match="robots.txt"):
            YCJobsHarvester(client).fetch(HarvestTarget("YC", "yc", "software-engineer"))


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Software Engineer, New Grad", "ok"),
        ("SDE-1", "ok"),
        ("Backend Developer", "ok"),
        ("Graduate Engineer Trainee", "ok"),
        ("Frontend Developer", "ok"),
        ("Full Stack Engineer", "ok"),
        ("Senior Software Engineer", "senior_title"),
        ("Staff Backend Engineer", "senior_title"),
        ("Lead Frontend Engineer", "senior_title"),
        ("Software Engineer III", "senior_title"),
        ("SDE 2", "level_two_title"),
        ("Software Engineering Intern", "internship"),
        ("Account Executive", "title_not_target_role"),
        ("Engineering Manager", "title_not_target_role"),
    ],
)
def test_title_filter(title, expected):
    assert JobFilter(FilterRules()).check_title(title)[1] == expected


def test_years_and_location_filters():
    job_filter = JobFilter(FilterRules(max_required_years=2, location_keywords=["india", "remote"]))
    base = dict(company="X", external_id="1", url="https://x", ats_type="greenhouse")
    assert not job_filter.evaluate(RawJob(title="Backend Engineer", location="Pune, India",
                                          description="5+ years of experience in Java", **base)).accepted
    assert job_filter.evaluate(RawJob(title="Backend Engineer", location="Pune, India",
                                      description="0-2 years of experience", **base)).accepted
    assert job_filter.evaluate(RawJob(title="Backend Engineer", location=None, description="", **base)).accepted
    decision = job_filter.evaluate(RawJob(title="Backend Engineer", location="Berlin", description="", **base))
    assert decision.reason == "location"


@pytest.mark.parametrize(
    "text,years",
    [
        ("3+ years of experience with Python", 3),
        ("Minimum of two years of professional experience", 2),
        ("1-3 yrs exp in Java", 1),
        ("We have grown 3x over the last 5 years.", None),
        ("Up to 2 years of experience", None),
        ("5+ years of industry experience; 2+ years with Kubernetes", 5),
    ],
)
def test_required_years(text, years):
    assert required_years(text) == years


def test_html_to_text_keeps_structure():
    text = html_to_text("&lt;h3&gt;Requirements&lt;/h3&gt;&lt;ul&gt;&lt;li&gt;Go&lt;/li&gt;&lt;li&gt;SQL&lt;/li&gt;&lt;/ul&gt;")
    assert text == "Requirements\n\n- Go\n- SQL"


def test_harvest_dedupes_and_records_status(demo_jobs):
    assert "Stripe|Software Engineer, New Grad" in demo_jobs
    assert not any("Senior" in key or "Staff" in key for key in demo_jobs)
    with demo_client() as client:
        second = harvest(client=client)
    assert sum(s.new for s in second) == 0
    with session_scope() as session:
        jobs = session.exec(select(Job)).all()
        assert len(jobs) == len(demo_jobs)
        assert len({(j.company, j.external_id) for j in jobs}) == len(jobs)
        stripe = session.exec(select(Company).where(Company.board_token == "stripe")).one()
        assert stripe.last_harvest_status.startswith("7 fetched")


def test_harvest_reports_missing_board(workspace):
    with session_scope() as session:
        upsert_company(session, {"name": "Nope", "ats_type": "greenhouse", "board_token": "does-not-exist"})
    with build_client(transport=demo_transport()) as client:
        [stats] = harvest(client=client)
    assert stats.error and "404" in stats.error
    with session_scope() as session:
        company = session.exec(select(Company)).one()
        assert company.last_harvest_status.startswith("error")
