"""Tests for PaneStream lifecycle attached to PaneInfo."""
import os
import stat
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from fifo_stream import PaneStream
from models import PaneInfo
from session import _maybe_attach_stream, detach_stream


@pytest.fixture
def fake_pane_info():
    # MagicMock for libtmux.Pane — the helper only uses .cmd("pipe-pane", ...)
    pane = MagicMock()
    pane.cmd.return_value.stdout = []  # harmless
    return PaneInfo(
        pane=pane, app_type="shell", description="test", name="shell",
    )


@pytest.fixture
def tmp_session_dir():
    with tempfile.TemporaryDirectory(prefix="clive-stream-test-") as d:
        yield d


def test_no_stream_when_flag_set_to_zero(monkeypatch, fake_pane_info, tmp_session_dir):
    monkeypatch.setenv("CLIVE_STREAMING_OBS", "0")
    _maybe_attach_stream(fake_pane_info, tmp_session_dir)
    assert fake_pane_info.stream is None
    assert fake_pane_info.pane_loop is None


def test_stream_attached_by_default_when_flag_unset(monkeypatch, fake_pane_info, tmp_session_dir):
    """Phase 1 ships default-on: unset env = streaming attached."""
    monkeypatch.delenv("CLIVE_STREAMING_OBS", raising=False)
    _maybe_attach_stream(fake_pane_info, tmp_session_dir)
    try:
        assert fake_pane_info.stream is not None
        assert fake_pane_info.pane_loop is not None
    finally:
        detach_stream(fake_pane_info)


def test_fifo_created_with_owner_only_permissions(monkeypatch, fake_pane_info, tmp_session_dir):
    """Regression for security audit F-1: pane-bytes FIFO must be 0o600.

    Pane bytes can carry passwords, tokens, file contents. Default umask
    on macOS/Linux is 0o022 which would make the FIFO 0o644 — readable by
    any local user. os.mkfifo must be called with an explicit mode=0o600.
    """
    # Set a permissive umask to prove the fix is independent of process umask
    old_umask = os.umask(0o000)
    try:
        monkeypatch.setenv("CLIVE_STREAMING_OBS", "1")
        _maybe_attach_stream(fake_pane_info, tmp_session_dir)
        try:
            fifo_path = os.path.join(tmp_session_dir, "pipes", "shell.fifo")
            assert os.path.exists(fifo_path)
            mode = stat.S_IMODE(os.stat(fifo_path).st_mode)
            assert mode == 0o600, (
                f"FIFO permissions are 0o{mode:o}, expected 0o600. "
                "Pane bytes would be readable by other local users."
            )
        finally:
            detach_stream(fake_pane_info)
    finally:
        os.umask(old_umask)


def test_stream_attached_when_flag_set(monkeypatch, fake_pane_info, tmp_session_dir):
    monkeypatch.setenv("CLIVE_STREAMING_OBS", "1")
    _maybe_attach_stream(fake_pane_info, tmp_session_dir)
    try:
        assert fake_pane_info.stream is not None
        assert isinstance(fake_pane_info.stream, PaneStream)
        assert fake_pane_info.pane_loop is not None
        # FIFO was created at the expected path
        expected = os.path.join(tmp_session_dir, "pipes", "shell.fifo")
        assert os.path.exists(expected)
        # tmux pipe-pane command was issued exactly once, targeting that FIFO
        fake_pane_info.pane.cmd.assert_any_call(
            "pipe-pane", "-o", f"cat > {expected}",
        )
    finally:
        detach_stream(fake_pane_info)


def test_detach_closes_stream_and_stops_loop(monkeypatch, fake_pane_info, tmp_session_dir):
    monkeypatch.setenv("CLIVE_STREAMING_OBS", "1")
    _maybe_attach_stream(fake_pane_info, tmp_session_dir)

    expected = os.path.join(tmp_session_dir, "pipes", "shell.fifo")
    assert os.path.exists(expected)
    loop = fake_pane_info.pane_loop

    detach_stream(fake_pane_info)

    # Loop thread joined
    assert not loop.thread.is_alive()
    # pipe-pane was toggled off (-o with no arg)
    cmds = [call.args for call in fake_pane_info.pane.cmd.call_args_list]
    assert any(c == ("pipe-pane", "-o") for c in cmds)
    # FIFO unlinked
    assert not os.path.exists(expected)
    # State nulled
    assert fake_pane_info.stream is None
    assert fake_pane_info.pane_loop is None


def test_mkfifo_failure_falls_back_silently(monkeypatch, fake_pane_info, tmp_session_dir):
    """If mkfifo raises, attach silently fails — pane_info.stream stays None."""
    monkeypatch.setenv("CLIVE_STREAMING_OBS", "1")
    with patch("os.mkfifo", side_effect=OSError("disk full")):
        _maybe_attach_stream(fake_pane_info, tmp_session_dir)
    assert fake_pane_info.stream is None
    # pane_loop may have been created then torn down; accept either None or stopped
    if fake_pane_info.pane_loop is not None:
        assert not fake_pane_info.pane_loop.thread.is_alive()
