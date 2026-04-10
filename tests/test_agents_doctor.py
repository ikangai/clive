"""Tests for the `clive --agents-doctor` subcommand.

The doctor runs a series of checks against each configured agent host
and produces a pass/fail report per check. It exists because the
single biggest class of production bugs in the remote-clive subsystem
is silent misconfig: AcceptEnv missing, wrong key path, clive not
installed on the remote. The doctor surfaces all of these proactively.
"""
import subprocess

import pytest


def test_check_agent_with_missing_key(tmp_path):
    from agents_doctor import check_agent
    config = {"host": "fake.example.com", "key": "/nonexistent/key"}
    result = check_agent("fake", config)
    assert result.checks["key_exists"][0] is False
    assert "missing" in result.checks["key_exists"][1]


def test_check_agent_with_no_key_ok():
    from agents_doctor import check_agent
    # An entry with no key uses the SSH default identity — not a failure.
    result = check_agent("ok", {"host": "unreachable-host-no-dns-match-xx"})
    assert result.checks["key_exists"][0] is True


def test_check_agent_ssh_timeout(monkeypatch):
    """When the SSH connect times out or fails, the ssh_connect check
    must fail but the whole doctor must not crash."""
    from agents_doctor import check_agent

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=10)

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = check_agent("fake", {})
    assert result.checks["ssh_connect"][0] is False


def test_check_agent_aggregate_ok_and_ok_reports_all_pass(monkeypatch):
    """When every individual check passes, AgentCheck.ok() is True."""
    from agents_doctor import AgentCheck
    r = AgentCheck(host="good")
    r.checks["key_exists"] = (True, "default identity")
    r.checks["ssh_connect"] = (True, "ok")
    r.checks["clive_installed"] = (True, "ok")
    r.checks["accept_env"] = (True, "all set envs accepted")
    assert r.ok() is True


def test_check_agent_aggregate_ok_any_fail_reports_not_ok():
    from agents_doctor import AgentCheck
    r = AgentCheck(host="bad")
    r.checks["key_exists"] = (True, "default identity")
    r.checks["ssh_connect"] = (False, "connection refused")
    assert r.ok() is False


def test_format_report_has_per_check_lines():
    from agents_doctor import AgentCheck, format_report
    r = AgentCheck(host="prod")
    r.checks["key_exists"] = (True, "ok")
    r.checks["ssh_connect"] = (False, "connection refused")
    out = format_report([r])
    assert "prod" in out
    assert "key_exists" in out
    assert "ssh_connect" in out
    assert "connection refused" in out


def test_format_report_empty_list_is_empty_string():
    from agents_doctor import format_report
    assert format_report([]) == ""


def test_run_doctor_with_empty_registry(tmp_path):
    """With no agents.yaml present, run_doctor returns an empty list
    instead of crashing. Useful for first-time setup feedback."""
    from agents_doctor import run_doctor
    # Point at a non-existent path
    result = run_doctor(registry_path=str(tmp_path / "missing.yaml"))
    assert result == []


def test_run_doctor_with_populated_registry(tmp_path, monkeypatch):
    """When agents.yaml has entries, run_doctor produces one AgentCheck
    per entry. We mock subprocess.run to avoid real SSH calls."""
    from agents_doctor import run_doctor
    reg = tmp_path / "agents.yaml"
    reg.write_text("devbox:\n  host: 10.0.0.1\nprod:\n  host: 10.0.0.2\n")

    def fake_run(*args, **kwargs):
        class R:
            returncode = 0
            stdout = "clive-doctor-ok"
            stderr = ""
        return R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    results = run_doctor(registry_path=str(reg))
    assert len(results) == 2
    hosts = {r.host for r in results}
    assert hosts == {"devbox", "prod"}


# ─── CLI wiring ──────────────────────────────────────────────────────────────

def test_agents_doctor_flag_is_registered():
    """The --agents-doctor flag must be registered in the CLI parser."""
    from cli_args import build_parser
    parser = build_parser()
    args = parser.parse_args(["--agents-doctor"])
    assert getattr(args, "agents_doctor", False) is True


def test_handle_agents_doctor_prints_report(tmp_path, capsys, monkeypatch):
    """The handler runs the doctor and prints the formatted report."""
    import cli_handlers
    from agents import _FORWARD_ENVS
    reg = tmp_path / "agents.yaml"
    reg.write_text("myhost:\n  host: 10.0.0.1\n")

    # Point the handler at our temp registry via env override
    monkeypatch.setenv("CLIVE_AGENTS_REGISTRY", str(reg))

    # Clear every env var the accept_env check would test, so the
    # mocked sshd output (which lacks AcceptEnv lines) does not flag
    # this test as failed. This test is about handler wiring, not
    # about the accept_env logic.
    for v in _FORWARD_ENVS:
        monkeypatch.delenv(v, raising=False)

    def fake_run(*args, **kwargs):
        class R:
            returncode = 0
            stdout = "clive-doctor-ok"
            stderr = ""
        return R()

    monkeypatch.setattr(subprocess, "run", fake_run)

    # Simulate the argparse namespace
    class Args:
        agents_doctor = True

    with pytest.raises(SystemExit) as exc:
        cli_handlers.handle_agents_doctor(Args())
    assert exc.value.code == 0

    captured = capsys.readouterr()
    assert "myhost" in captured.out


def test_handle_agents_doctor_exits_1_when_any_check_fails(tmp_path, capsys, monkeypatch):
    """If any host has a failing check, the handler exits 1 so shell
    pipelines can detect the failure."""
    import cli_handlers
    reg = tmp_path / "agents.yaml"
    reg.write_text("dead:\n  host: 10.0.0.1\n")
    monkeypatch.setenv("CLIVE_AGENTS_REGISTRY", str(reg))

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=10)

    monkeypatch.setattr(subprocess, "run", fake_run)

    class Args:
        agents_doctor = True

    with pytest.raises(SystemExit) as exc:
        cli_handlers.handle_agents_doctor(Args())
    assert exc.value.code == 1


def test_handle_agents_doctor_empty_registry_exits_0(tmp_path, capsys, monkeypatch):
    """No agents configured → exit 0 with a helpful message, not 1."""
    import cli_handlers
    monkeypatch.setenv("CLIVE_AGENTS_REGISTRY", str(tmp_path / "missing.yaml"))

    class Args:
        agents_doctor = True

    with pytest.raises(SystemExit) as exc:
        cli_handlers.handle_agents_doctor(Args())
    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert "No agents configured" in captured.out
