"""Integration: the step-7 verification script, the CLI and the scheduler wiring."""

from __future__ import annotations

from referralpilot.cli import main as cli_main
from referralpilot.verify import run_verification


def test_verification_script_passes(capsys):
    assert run_verification() == 0
    output = capsys.readouterr().out
    assert "checks passed" in output and "✘" not in output


def test_cli_init_demo_and_jobs(workspace, capsys):
    assert cli_main(["init"]) == 0
    assert cli_main(["demo"]) == 0
    assert cli_main(["jobs", "--limit", "3"]) == 0
    out = capsys.readouterr().out
    assert "Database ready" in out and "Demo data loaded" in out
    assert out.count("discovered") >= 3
    assert cli_main(["engines"]) == 0
    assert "fpdf" in capsys.readouterr().out


def test_cli_end_to_end_commands(workspace, capsys, monkeypatch):
    monkeypatch.setenv("LATEX_ENGINE", "fpdf")
    from referralpilot.config import reset_settings

    reset_settings()
    assert cli_main(["demo"]) == 0
    assert cli_main(["tailor", "1"]) == 0
    assert cli_main(["prospect", "1", "--offline"]) == 0
    out = capsys.readouterr().out
    assert "ATS match" in out and "Domain:" in out
    assert cli_main(["queue"]) == 0
    assert "Sent in last 24h: 0/20" in capsys.readouterr().out


def test_scheduler_registers_jobs(workspace):
    from referralpilot.scheduler import build_scheduler, describe_jobs

    scheduler = build_scheduler()
    ids = {job["id"] for job in describe_jobs(scheduler)}
    assert ids == {"harvest", "send_tick", "replies", "followups", "prune"}
