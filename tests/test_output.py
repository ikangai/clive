"""Tests for output routing (quiet mode)."""
import sys
from io import StringIO
from output import progress, result, set_quiet


def test_progress_default_goes_to_stdout(capsys):
    set_quiet(False)
    progress("hello")
    captured = capsys.readouterr()
    assert captured.out.strip() == "hello"
    assert captured.err == ""


def test_progress_quiet_goes_to_stderr(capsys):
    set_quiet(True)
    progress("hello")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == "hello"
    set_quiet(False)  # reset


def test_result_always_goes_to_stdout(capsys):
    set_quiet(True)
    result("final answer")
    captured = capsys.readouterr()
    assert captured.out.strip() == "final answer"
    assert captured.err == ""
    set_quiet(False)  # reset
