"""Tests for the observation event system."""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from observation import EventType, ScreenEvent, ScreenClassifier, format_event_for_llm, _MAX_SUMMARY


classifier = ScreenClassifier()


def test_success_with_exit_code_zero():
    event = classifier.classify("some output\nall done", exit_code=0)
    assert event.type == EventType.SUCCESS
    assert event.needs_llm is False
    assert event.exit_code == 0


def test_success_with_agent_ready_marker():
    screen = "build complete\n[AGENT_READY] $"
    event = classifier.classify(screen)
    assert event.type == EventType.SUCCESS
    assert event.needs_llm is False
    assert "ready" in event.summary.lower()


def test_error_with_nonzero_exit_code():
    screen = "Error: file not found\n"
    event = classifier.classify(screen, exit_code=1)
    assert event.type == EventType.ERROR
    assert event.needs_llm is True
    assert event.exit_code == 1
    assert "exit 1" in event.summary


def test_error_exit_code_127():
    screen = "bash: foobar: command not found\n"
    event = classifier.classify(screen, exit_code=127)
    assert event.type == EventType.ERROR
    assert event.exit_code == 127
    assert "exit 127" in event.summary


def test_needs_input_confirmation():
    screen = "Do you want to proceed? [Y/n] "
    event = classifier.classify(screen)
    assert event.type == EventType.NEEDS_INPUT
    assert event.needs_llm is True
    assert "confirmation" in event.summary.lower()


def test_needs_input_password():
    screen = "Password: "
    event = classifier.classify(screen)
    assert event.type == EventType.NEEDS_INPUT
    assert event.needs_llm is True
    assert "password" in event.summary.lower()


def test_needs_input_overwrite():
    screen = "File exists. Overwrite it? "
    event = classifier.classify(screen)
    assert event.type == EventType.NEEDS_INPUT
    assert event.needs_llm is True


def test_running_detection_percentage():
    screen = "Downloading packages... 45% complete"
    event = classifier.classify(screen)
    assert event.type == EventType.RUNNING
    assert event.needs_llm is False


def test_running_detection_progress():
    screen = "Building module 3/10"
    event = classifier.classify(screen)
    assert event.type == EventType.RUNNING
    assert event.needs_llm is False


def test_fatal_error_detection():
    screen = "FATAL: cannot connect to database"
    event = classifier.classify(screen)
    assert event.type == EventType.ERROR
    assert event.needs_llm is True
    assert "fatal" in event.summary.lower()


def test_permission_denied():
    screen = "Permission denied: /etc/shadow"
    event = classifier.classify(screen)
    assert event.type == EventType.ERROR
    assert event.needs_llm is True
    assert "permission" in event.summary.lower()


def test_disk_error():
    screen = "No space left on device"
    event = classifier.classify(screen)
    assert event.type == EventType.ERROR
    assert event.needs_llm is True
    assert "disk" in event.summary.lower()


def test_unknown_state():
    screen = "some random output with no clear signal"
    event = classifier.classify(screen)
    assert event.type == EventType.UNKNOWN
    assert event.needs_llm is True


def test_summary_truncation():
    long_summary = "x" * 1000
    event = ScreenEvent(
        type=EventType.SUCCESS,
        summary=long_summary,
        needs_llm=False,
    )
    assert len(event.summary) == _MAX_SUMMARY
    assert event.summary.endswith("...")


def test_summary_not_truncated_when_short():
    short = "hello"
    event = ScreenEvent(type=EventType.SUCCESS, summary=short, needs_llm=False)
    assert event.summary == "hello"


def test_raw_output_extraction():
    screen = "a" * 2000
    event = classifier.classify(screen, exit_code=0)
    assert len(event.raw_output) == 1000  # _RAW_TAIL


def test_format_event_success():
    event = ScreenEvent(type=EventType.SUCCESS, summary="done", needs_llm=False, exit_code=0)
    formatted = format_event_for_llm(event)
    assert formatted == "[OK exit:0] done"


def test_format_event_success_no_exit_code():
    event = ScreenEvent(type=EventType.SUCCESS, summary="ready", needs_llm=False)
    formatted = format_event_for_llm(event)
    assert formatted == "[OK] ready"


def test_format_event_error():
    event = ScreenEvent(type=EventType.ERROR, summary="failed", needs_llm=True, exit_code=1)
    formatted = format_event_for_llm(event)
    assert formatted == "[ERROR exit:1] failed"


def test_format_event_needs_input():
    event = ScreenEvent(type=EventType.NEEDS_INPUT, summary="password prompt", needs_llm=True)
    formatted = format_event_for_llm(event)
    assert formatted == "[NEEDS INPUT] password prompt"


def test_format_event_running():
    event = ScreenEvent(type=EventType.RUNNING, summary="building", needs_llm=False)
    formatted = format_event_for_llm(event)
    assert formatted == "[RUNNING] building"


def test_format_event_unknown():
    event = ScreenEvent(type=EventType.UNKNOWN, summary="unclear", needs_llm=True)
    formatted = format_event_for_llm(event)
    assert formatted == "[SCREEN] unclear"


def test_intervention_before_exit_code():
    """Intervention patterns take priority over exit code."""
    screen = "Permission denied: /root/secret"
    event = classifier.classify(screen, exit_code=0)
    # Intervention pattern fires before exit_code=0 check
    assert event.type == EventType.ERROR
    assert "permission" in event.summary.lower()


def test_continue_prompt():
    screen = "Press ENTER to continue"
    event = classifier.classify(screen)
    assert event.type == EventType.NEEDS_INPUT
    assert event.needs_llm is True
