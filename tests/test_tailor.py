from __future__ import annotations

import shutil

import pytest

from referralpilot.db import session_scope
from referralpilot.models import Job, JobStatus
from referralpilot.tailor import build_resume, compile_resume, parse_job_description, score_match, tailor_job
from referralpilot.tailor.compiler import engine_available
from referralpilot.tailor.render import render_latex, render_markdown, tex_escape, tex_url
from referralpilot.tailor.skills import extract_skills, normalize_skill

JD = """About the team
We build payment APIs.

Requirements
- Experience writing production code in Java, Go, Ruby or Python
- Familiarity with SQL databases such as PostgreSQL or MySQL
- Strong data structures and algorithms

Nice to have
- Experience with Kafka or other message queues
- Exposure to AWS or Kubernetes
"""


@pytest.mark.parametrize(
    "text,expected,absent",
    [
        ("Go above and beyond for users", set(), {"Go"}),
        ("We react quickly to incidents", set(), {"React"}),
        ("the rest of the team", set(), {"REST APIs"}),
        ("Built REST APIs in Go with React front ends", {"REST APIs", "Go", "React"}, set()),
        ("C/C++ and Node.js with Postgres on k8s via GitHub Actions", {"C", "C++", "Node.js", "PostgreSQL", "Kubernetes", "CI/CD"}, set()),
        ("React Native mobile apps", {"React Native"}, {"React"}),
    ],
)
def test_skill_extraction(text, expected, absent):
    found = extract_skills(text)
    assert expected <= found
    assert not (absent & found)


def test_normalize_skill_aliases():
    assert normalize_skill("Postgres") == {"PostgreSQL"}
    assert normalize_skill("golang") == {"Go"}
    assert normalize_skill("C") == {"C"}
    assert normalize_skill("FAISS") == {"FAISS"}  # unknown skills pass through


def test_jd_sections_and_alternatives():
    parsed = parse_job_description(JD, "Software Engineer, New Grad")
    data = parsed.to_dict()
    assert {"Java", "Go", "Python", "PostgreSQL", "Data Structures & Algorithms"} <= set(data["required"])
    assert {"Kafka", "AWS"} <= set(data["preferred"])
    groups = [set(u.skills) for u in parsed.units if len(u.skills) > 1]
    assert {"Java", "Go", "Ruby", "Python"} in groups
    assert {"PostgreSQL", "MySQL"} in groups
    assert parsed.skill_weights["Java"] > parsed.skill_weights["Kafka"]


def test_score_rewards_coverage(profile):
    strong = score_match(profile, "Backend Engineer", JD)
    weak = score_match(profile, "Backend Engineer", "Requirements\n- Elixir\n- Erlang/OTP\n- Haskell")
    assert 0 <= weak.score < strong.score <= 100
    assert "Go" in strong.matched and "Ruby" not in strong.missing  # alternatives already satisfied
    senior = score_match(profile, "Backend Engineer", JD + "\nRequirements\n- 5+ years of experience")
    assert senior.score < strong.score
    assert strong.projects[0].score >= strong.projects[-1].score


def test_resume_document_ranks_and_bolds(profile):
    match = score_match(profile, "Backend Engineer", JD)
    doc = build_resume(profile, match, company="Stripe", role="Backend Engineer", max_projects=2)
    assert len(doc.projects) == 2
    assert doc.projects[0]["name"] == match.projects[0].name
    languages = dict(doc.skills)["Languages"]
    assert languages[0][1] is True  # matched skills first and bolded
    bolded = {span.text for bullet in doc.projects[0]["bullets"] for span in bullet if span.bold}
    assert bolded
    assert "summary" in doc.sections and doc.summary


def test_latex_escaping():
    assert tex_escape("R&D 100% #1 C++_x {y} ~ ^") == r"R\&D 100\% \#1 C++\_x \{y\} \textasciitilde{} \textasciicircum{}"
    assert tex_escape("2021 – 2025 “quoted” ₹10") == "2021 -- 2025 ``quoted'' Rs.~10"
    assert tex_url("https://x.dev/a b#c%") == r"https://x.dev/a\%20b\#c\%"


def test_render_latex_and_markdown(profile, workspace):
    match = score_match(profile, "Backend Engineer", JD)
    doc = build_resume(profile, match, company="Stripe & Co", role="Backend Engineer")
    tex = render_latex(doc, workspace.templates_dir)
    assert tex.startswith("\\documentclass")
    assert "\\VAR{" not in tex and "%%" not in tex
    assert r"Stripe \& Co" in tex
    assert "\\textbf{" in tex and doc.projects[0]["name"] in tex
    md = render_markdown(doc)
    assert md.startswith(f"# {profile.full_name}") and "## Projects" in md


def test_fpdf_fallback_always_works(profile, workspace, tmp_path):
    match = score_match(profile, "Backend Engineer", JD)
    doc = build_resume(profile, match, company="Stripe", role="Backend Engineer")
    result = compile_resume(doc, tmp_path, "fallback", templates_dir=workspace.templates_dir, engine="fpdf")
    assert result.ok and result.engine == "fpdf"
    assert result.pdf_path.read_bytes()[:4] == b"%PDF"
    assert result.tex_path.exists() and result.md_path.exists()


@pytest.mark.skipif(not shutil.which("pdflatex"), reason="pdflatex not installed")
def test_pdflatex_compiles_template(profile, workspace, tmp_path):
    match = score_match(profile, "Backend Engineer", JD)
    doc = build_resume(profile, match, company="Stripe", role="Backend Engineer")
    result = compile_resume(doc, tmp_path, "latex", templates_dir=workspace.templates_dir, engine="pdflatex")
    assert result.ok, result.describe_failures()
    assert result.pdf_path.read_bytes()[:4] == b"%PDF"


@pytest.mark.skipif(not engine_available("typst"), reason="typst not installed")
def test_typst_compiles_template(profile, workspace, tmp_path):
    match = score_match(profile, "Backend Engineer", JD)
    doc = build_resume(profile, match, company="Stripe", role="Backend Engineer")
    result = compile_resume(doc, tmp_path, "typst", templates_dir=workspace.templates_dir, engine="typst")
    assert result.ok, result.describe_failures()


def test_auto_chain_falls_back_when_engines_fail(profile, workspace, tmp_path, monkeypatch):
    from referralpilot.tailor import compiler

    monkeypatch.setattr(compiler, "engine_available", lambda name: name == "fpdf")
    match = score_match(profile, "Backend Engineer", JD)
    doc = build_resume(profile, match, company="Stripe", role="Backend Engineer")
    result = compile_resume(doc, tmp_path, "chain", templates_dir=workspace.templates_dir, engine="auto")
    assert result.engine == "fpdf"
    assert ("pdflatex", "not installed") in result.attempts


def test_tailor_job_updates_job(demo_jobs, workspace):
    with session_scope() as session:
        job = session.get(Job, demo_jobs["Stripe|Software Engineer, New Grad"])
        outcome = tailor_job(session, job, engine="fpdf")
        assert outcome.compile.ok
        assert job.status == JobStatus.TAILORED
        assert job.match_score == outcome.match.score
        assert job.resume_pdf_path.endswith("stripe_software-engineer-new-grad_resume.pdf")
        assert job.matched_skills and job.match_details["projects"]
