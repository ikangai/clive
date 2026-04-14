"""Tests for tiered intent resolution — Tier 0 (regex direct detection)."""
import sys


def test_direct_ls():
    from clive import _is_direct
    assert _is_direct("ls -la", 1) is True


def test_direct_curl():
    from clive import _is_direct
    assert _is_direct("curl https://example.com", 1) is True


def test_direct_grep():
    from clive import _is_direct
    assert _is_direct("grep TODO *.py", 1) is True


def test_direct_pipe():
    from clive import _is_direct
    assert _is_direct("cat file.txt | sort | uniq -c", 1) is True


def test_direct_awk():
    from clive import _is_direct
    assert _is_direct("awk -F, '{print $1}' data.csv", 1) is True


def test_direct_jq():
    from clive import _is_direct
    assert _is_direct("jq '.name' package.json", 1) is True


def test_direct_rg():
    from clive import _is_direct
    assert _is_direct("rg TODO --type py", 1) is True


def test_not_direct_natural_language():
    from clive import _is_direct
    assert _is_direct("what is the average file size?", 1) is False


def test_not_direct_question():
    from clive import _is_direct
    assert _is_direct("how many python files are there?", 1) is False


def test_not_direct_show_me():
    from clive import _is_direct
    assert _is_direct("show me the biggest files", 1) is False


def test_not_direct_find_the():
    from clive import _is_direct
    assert _is_direct("find the error in the logs", 1) is False


def test_not_direct_multi_pane():
    from clive import _is_direct
    assert _is_direct("ls -la", 2) is False


def test_direct_find_command():
    from clive import _is_direct
    assert _is_direct("find . -name '*.py' -type f", 1) is True


def test_direct_wc():
    from clive import _is_direct
    assert _is_direct("wc -l *.py", 1) is True


def test_direct_echo():
    from clive import _is_direct
    assert _is_direct("echo hello world", 1) is True
