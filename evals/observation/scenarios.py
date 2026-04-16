"""Synthetic scenarios for observation-layer latency benchmarking.

Each scenario is reproducible shell one-liner that generates a known
signal pattern. The bench harness runs each scenario N times per mode
(baseline/phase1/phase2) and reports detection latency, e2e latency,
and missed-event rate.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Scenario:
    id: str
    shell_command: str
    expected_l2_kinds: tuple[str, ...]  # L2 event kinds expected to fire
    target_description: str             # for reports
    baseline_blind: bool = False        # true if today's loop fundamentally can't detect


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        id="error_scroll",
        shell_command=r"sleep 0.5 && printf '\x1b[31mERROR: boom\x1b[0m\n' && sleep 2",
        expected_l2_kinds=("color_alert", "error_keyword"),
        target_description="Red ERROR scrolls past quickly, no exit",
    ),
    Scenario(
        id="password_prompt",
        # `sudo -S` with a bogus non-existent command to force a password prompt without
        # escalating; -k invalidates cached creds so prompt definitely appears.
        shell_command="sudo -k && sudo -S echo pw_test 2>&1 || true",
        expected_l2_kinds=("password_prompt",),
        target_description="Password prompt mid-command",
    ),
    Scenario(
        id="confirm_prompt",
        shell_command="printf 'continue? [y/N] '",
        expected_l2_kinds=("confirm_prompt",),
        target_description="y/N confirmation prompt",
    ),
    Scenario(
        id="spinner_ok",
        shell_command="for i in 1 2 3 4 5; do printf '.'; sleep 0.3; done; echo done",
        expected_l2_kinds=("cmd_end",),
        target_description="Brief spinner-like activity then exit=0",
    ),
    Scenario(
        id="spinner_fail",
        shell_command="for i in 1 2 3; do printf '.'; sleep 0.3; done; exit 1",
        expected_l2_kinds=("cmd_end",),
        target_description="Spinner-like activity then exit=1",
    ),
    Scenario(
        id="color_only",
        # Print a line, then rewrite it with only color change — no new text.
        # Tests whether observation can see pure-SGR signals.
        shell_command=r"printf 'status\n'; sleep 1; printf '\x1b[A\x1b[31mstatus\x1b[0m\n'",
        expected_l2_kinds=("color_alert",),
        target_description="Color-only change to existing text (baseline can't detect)",
        baseline_blind=True,
    ),
)
