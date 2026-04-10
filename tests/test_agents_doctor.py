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
    """The handler runs the doctor and prints the formatted report.

    Uses a differentiated mock that returns sensible output for each
    sub-command: echo succeeds, clive imports, sshd -T reports
    could-not-verify (exit non-zero). This exercises the handler
    wiring without depending on the env-var contents of the test
    runner.
    """
    import cli_handlers
    reg = tmp_path / "agents.yaml"
    reg.write_text("myhost:\n  host: 10.0.0.1\n")
    monkeypatch.setenv("CLIVE_AGENTS_REGISTRY", str(reg))

    def fake_run(argv, *args, **kwargs):
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        cmd_str = " ".join(argv) if isinstance(argv, list) else argv
        if "echo clive-doctor-ok" in cmd_str:
            R.stdout = "clive-doctor-ok\n"
        elif "import clive" in cmd_str:
            R.stdout = "ok\n"
        elif "sshd -T" in cmd_str:
            # Could-not-verify path — exit 1 means the doctor reports
            # the check as OK with an informational message, which
            # keeps the handler exit code at 0 regardless of whether
            # the test runner has OPENROUTER_API_KEY etc. set.
            R.stdout = "<<SSHD_EXIT>> 1\n"
        return R()

    monkeypatch.setattr(subprocess, "run", fake_run)

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
            # sshd accepts LLM_PROVIDER but NOT OPENROUTER_API_KEY;
            # exit 0 marker confirms sshd -T itself succeeded
            R.stdout = "acceptenv LLM_PROVIDER\n<<SSHD_EXIT>> 0\n"
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


def test_accept_env_sshd_ok_but_no_directive_reports_missing(monkeypatch):
    """Regression test for M-A.

    Case 3 from the Phase 3.5 review: remote sshd is fine, the user
    has sudo (or doesn't need it), but sshd_config has no AcceptEnv
    directive at all. `sshd -T` succeeds with exit 0 but the grep
    filter finds nothing, so stdout is effectively empty of
    AcceptEnv lines.

    Under Phase 3.5's H1 fix, this was reported as "could not verify"
    (same as case 1 — sudo required). M-A distinguishes the two by
    capturing sshd -T's exit code: zero-with-no-AcceptEnv means the
    check did run and the AcceptEnv list is genuinely empty, so
    every forwarded env var IS missing and should be reported.
    """
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
            # sshd -T ran successfully (exit 0) but sshd_config had no
            # AcceptEnv directives — simulate by emitting the marker
            # with exit 0 and nothing else.
            R.stdout = "<<SSHD_EXIT>> 0\n"
        return R()

    monkeypatch.setattr("subprocess.run", fake_run)

    result = agents_doctor.check_agent("myhost", {})
    ok, detail = result.checks["accept_env"]
    assert ok is False, (
        f"case-3 must be reported as missing, not 'could not verify'. "
        f"Got: ({ok}, {detail!r})"
    )
    assert "missing AcceptEnv" in detail
    assert "OPENROUTER_API_KEY" in detail or "LLM_PROVIDER" in detail


def test_accept_env_sshd_failed_is_could_not_verify(monkeypatch):
    """Regression test for M-A.

    Case 1: sshd -T failed (non-zero exit, typically "must be run as
    root"). The marker carries a non-zero code and the doctor reports
    could-not-verify — unchanged behaviour from Phase 3.5 but now
    driven by the explicit exit code rather than a stdout heuristic.
    """
    import agents_doctor
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-fake")

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
            # sshd exited non-zero — permission denied on most distros
            R.stdout = "<<SSHD_EXIT>> 1\n"
            R.stderr = "/usr/sbin/sshd: must be run as root\n"
        return R()

    monkeypatch.setattr("subprocess.run", fake_run)

    result = agents_doctor.check_agent("myhost", {})
    ok, detail = result.checks["accept_env"]
    assert ok is True
    assert "could not verify" in detail.lower()


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
            R.stdout = (
                "acceptenv LLM_PROVIDER OPENROUTER_API_KEY\n"
                "<<SSHD_EXIT>> 0\n"
            )
        return R()

    monkeypatch.setattr("subprocess.run", fake_run)

    result = agents_doctor.check_agent("myhost", {})
    ok, detail = result.checks["accept_env"]
    assert ok is True
    assert "all set envs accepted" in detail
