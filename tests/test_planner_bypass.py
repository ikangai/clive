"""Tests for trivial task detection."""
import sys
sys.path.insert(0, '.')


def test_trivial_ls():
    from clive import _is_trivial
    assert _is_trivial("list files in /tmp", 1) is True


def test_trivial_grep():
    from clive import _is_trivial
    assert _is_trivial("grep TODO in all files", 1) is True


def test_trivial_curl():
    from clive import _is_trivial
    assert _is_trivial("curl https://example.com", 1) is True


def test_not_trivial_long_task():
    from clive import _is_trivial
    assert _is_trivial("research the history of computing and write a detailed report about the top 10 most influential computers of all time", 1) is False


def test_not_trivial_multi_pane():
    from clive import _is_trivial
    assert _is_trivial("list files", 2) is False


def test_not_trivial_complex():
    from clive import _is_trivial
    assert _is_trivial("browse example.com and summarize the content then email it", 1) is False
