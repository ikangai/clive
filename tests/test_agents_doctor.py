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


# ─── Regression: accept_env must not false-negative on empty sshd -T ─────────

def test_accept_env_empty_sshd_output_is_could_not_verify(monkeypatch):
    """Regression test for H1.

    Most Linux distros require sudo to run `sshd -T`. Running it as a
    regular user over SSH prints to stderr and exits non-zero. The
    accept_cmd's `2>/dev/null | grep ... || true` dance swallows the
    stderr and forces exit 0, so subprocess.run returns rc=0 with
    stdout="". The previous implementation then computed
    `missing = every _FORWARD_ENVS entry set in env` and reported
    accept_env=False — a false positive that told users their remote
    sshd was misconfigured when in fact nothing had been verified.

    The docstring said "false positives on AcceptEnv are less bad
    than false negatives"; the fix makes the code actually match
    that intent by treating empty stdout as "could not verify".
    """
    import agents_doctor
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-fake")
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")

    def fake_run(argv, *args, **kwargs):
        class R:
            returncode = 0
            stderr = ""
            stdout = ""
        # Let the earlier checks pass so we reach accept_env
        cmd_str = " ".join(argv) if isinstance(argv, list) else argv
        if "echo clive-doctor-ok" in cmd_str:
            R.stdout = "clive-doctor-ok\n"
        elif "import clive" in cmd_str:
            R.stdout = "ok\n"
        elif "sshd -T" in cmd_str:
            # Simulated: sshd needs root, 2>/dev/null swallows, || true
            # forces exit 0, leaving stdout empty.
            R.stdout = ""
        return R()

    monkeypatch.setattr("subprocess.run", fake_run)

    result = agents_doctor.check_agent("myhost", {})
    ok, detail = result.checks["accept_env"]
    assert ok is True, (
        f"Empty sshd -T output must be treated as could-not-verify, "
        f"not as missing-everything. Got: ({ok}, {detail!r})"
    )
    assert "could not verify" in detail.lower()


def test_accept_env_populated_sshd_output_detects_actual_missing(monkeypatch):
    """When sshd -T returns real output, the check must still detect
    env vars that are genuinely missing from AcceptEnv."""
    import agents_doctor
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-fake")
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")

    def fake_run(argv, *args, **kwargs):
        class R:
            returncode = 0
            stderr = ""
            stdout = ""
        cmd_str = " ".join(argv) if isinstance(argv, list) else argv
        if "echo clive-doctor-ok" in cmd_str:
            R.stdout = "clive-doctor-ok\n"
        elif "import clive" in cmd_str:
            R.stdout = "ok\n"
        elif "sshd -T" in cmd_str:
            # sshd accepts LLM_PROVIDER but NOT OPENROUTER_API_KEY
            R.stdout = "acceptenv LLM_PROVIDER\n"
        return R()

    monkeypatch.setattr("subprocess.run", fake_run)

    result = agents_doctor.check_agent("myhost", {})
    ok, detail = result.checks["accept_env"]
    assert ok is False
    assert "OPENROUTER_API_KEY" in detail


def test_clive_installed_uses_python3_not_wrapper_path(monkeypatch):
    """Regression test for M3.

    The previous implementation did `clive_path.split()[0]` to pick
    an "interpreter" — fine for the default "python3 clive.py" but
    wrong for a legitimate wrapper path like "/opt/clive/bin/clive".
    Such a path is NOT a Python interpreter and does not accept
    -c, so the check would silently fail.

    The check's purpose is "can the remote import clive?", which is
    independent of how clive is normally launched. Always use
    `python3 -c 'import clive; ...'`.
    """
    import agents_doctor
    captured_cmds = []

    def fake_run(argv, *args, **kwargs):
        captured_cmds.append(list(argv) if isinstance(argv, list) else [argv])
        class R:
            returncode = 0
            stderr = ""
            stdout = ""
        cmd_str = " ".join(argv) if isinstance(argv, list) else argv
        if "echo clive-doctor-ok" in cmd_str:
            R.stdout = "clive-doctor-ok\n"
        elif "import clive" in cmd_str:
            R.stdout = "ok\n"
        return R()

    monkeypatch.setattr("subprocess.run", fake_run)

    # User's registry specifies a wrapper script path — NOT a python interp
    agents_doctor.check_agent("myhost", {"path": "/opt/clive/bin/clive-wrapper"})

    # Find the import check command among captured calls
    import_cmd_strs = [
        " ".join(argv) for argv in captured_cmds
        if any("import clive" in arg for arg in argv)
    ]
    assert len(import_cmd_strs) == 1
    import_cmd_str = import_cmd_strs[0]
    assert "python3 -c" in import_cmd_str, (
        f"clive-install check must use `python3 -c`, not a wrapper path. "
        f"Got: {import_cmd_str}"
    )
    assert "/opt/clive/bin/clive-wrapper -c" not in import_cmd_str


def test_accept_env_all_present_reports_ok(monkeypatch):
    """When sshd -T has AcceptEnv for every outer-set env var, pass."""
    import agents_doctor
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-fake")
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")

    def fake_run(argv, *args, **kwargs):
        class R:
            returncode = 0
            stderr = ""
            stdout = ""
        cmd_str = " ".join(argv) if isinstance(argv, list) else argv
        if "echo clive-doctor-ok" in cmd_str:
            R.stdout = "clive-doctor-ok\n"
        elif "import clive" in cmd_str:
            R.stdout = "ok\n"
        elif "sshd -T" in cmd_str:
            R.stdout = "acceptenv LLM_PROVIDER OPENROUTER_API_KEY\n"
        return R()

    monkeypatch.setattr("subprocess.run", fake_run)

    result = agents_doctor.check_agent("myhost", {})
    ok, detail = result.checks["accept_env"]
    assert ok is True
    assert "all set envs accepted" in detail
