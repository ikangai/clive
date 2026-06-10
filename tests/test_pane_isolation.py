"""Tests for the response isolation layer (gh#14)."""
import threading
import time

import pytest

from pane_isolation import (
    PaneIsolation,
    isolation_enabled,
    make_tag,
    wrap_isolated,
)


class TestWrapIsolated:
    def test_bookends_and_subshell(self):
        w = wrap_isolated("ls -la", "task_3_ab12")
        assert w.startswith('echo "===BEGIN_task_3_ab12==="')
        assert w.endswith('echo "===END_task_3_ab12=== EXIT:$?"')
        # Env isolation: the command runs in a subshell.
        assert "( ls -la )" in w

    def test_cwd_confines_the_subshell(self):
        w = wrap_isolated("ls", "t_1", cwd="/tmp/clive dir/s1")
        # Paths are shell-quoted (this one needs it).
        assert "cd '/tmp/clive dir/s1'" in w
        # cd happens INSIDE the subshell so it can't leak.
        assert w.index("(") < w.index("cd ")

    def test_exit_code_captured_from_subshell(self):
        # EXIT:$? must follow the subshell close so it reports the
        # command's code, not the echo's.
        w = wrap_isolated("false", "t_2")
        assert w.index(")") < w.index("EXIT:$?")

    def test_heredoc_commands_get_newline_joined_bookends(self):
        """Appending '); echo ...' to a heredoc terminator line corrupts
        it (gh#40 finding on wrap_command) — heredoc commands must close
        the subshell and emit END on their own lines."""
        w = wrap_isolated("cat <<EOF\nhello\nEOF", "t_3")
        lines = w.splitlines()
        assert "EOF" in lines  # terminator intact on its own line
        assert ")" in lines  # subshell close on its own line
        assert lines[-1] == 'echo "===END_t_3=== EXIT:$?"'


class TestMakeTag:
    def test_unique_and_shell_safe(self):
        a, b = make_tag("task-3"), make_tag("task-3")
        assert a != b
        assert all(c.isalnum() or c == "_" for c in a)

    def test_sanitizes_hostile_ids(self):
        t = make_tag('x"; rm -rf /; echo "')
        assert all(c.isalnum() or c == "_" for c in t)


class TestPaneIsolationDemux:
    def _iso(self):
        sent = []
        iso = PaneIsolation(send_fn=sent.append)
        return iso, sent

    def test_submit_sends_wrapped_command(self):
        iso, sent = self._iso()
        iso.submit("ls", "t_1")
        assert len(sent) == 1
        assert "===BEGIN_t_1===" in sent[0]

    def test_output_routed_to_future(self):
        iso, _ = self._iso()
        fut = iso.submit("ls", "t_1")
        iso.feed("===BEGIN_t_1===")
        iso.feed("file1.txt")
        iso.feed("file2.txt")
        iso.feed("===END_t_1=== EXIT:0")
        exit_code, output = fut.result(timeout=1)
        assert exit_code == 0
        assert output == "file1.txt\nfile2.txt"

    def test_nonzero_exit_code(self):
        iso, _ = self._iso()
        fut = iso.submit("false", "t_2")
        iso.feed("===BEGIN_t_2===")
        iso.feed("===END_t_2=== EXIT:1")
        exit_code, output = fut.result(timeout=1)
        assert exit_code == 1
        assert output == ""

    def test_sequential_tags_demuxed_independently(self):
        iso, _ = self._iso()
        f1 = iso.submit("cmd1", "t_1")
        f2 = iso.submit("cmd2", "t_2")
        # Shell executes sequentially: t_1's block fully precedes t_2's.
        iso.feed("===BEGIN_t_1===")
        iso.feed("out1")
        iso.feed("===END_t_1=== EXIT:0")
        iso.feed("===BEGIN_t_2===")
        iso.feed("out2")
        iso.feed("===END_t_2=== EXIT:0")
        assert f1.result(timeout=1)[1] == "out1"
        assert f2.result(timeout=1)[1] == "out2"

    def test_command_echo_does_not_open_a_block(self):
        """The typed command echoes back containing the literal markers —
        a full-line anchor must not treat the echo as BEGIN/END."""
        iso, _ = self._iso()
        fut = iso.submit("ls", "t_1")
        # tmux echoes the wrapped command line first:
        iso.feed('echo "===BEGIN_t_1==="; ( ls ); echo "===END_t_1=== EXIT:$?"')
        assert not fut.done()
        iso.feed("===BEGIN_t_1===")
        iso.feed("data")
        iso.feed("===END_t_1=== EXIT:0")
        assert fut.result(timeout=1) == (0, "data")

    def test_lines_outside_blocks_ignored(self):
        iso, _ = self._iso()
        fut = iso.submit("ls", "t_1")
        iso.feed("[AGENT_READY] $ ")
        iso.feed("random prompt noise")
        iso.feed("===BEGIN_t_1===")
        iso.feed("===END_t_1=== EXIT:0")
        assert fut.result(timeout=1)[0] == 0

    def test_unknown_end_tag_ignored(self):
        iso, _ = self._iso()
        fut = iso.submit("ls", "t_1")
        iso.feed("===END_t_999=== EXIT:0")
        assert not fut.done()

    def test_failed_send_does_not_leak_pending_future(self):
        def boom(cmd):
            raise RuntimeError("pane gone")

        iso = PaneIsolation(send_fn=boom)
        with pytest.raises(RuntimeError, match="pane gone"):
            iso.submit("ls", "t_1")
        assert iso._pending == {}

    def test_cancel_all_fails_pending_futures(self):
        iso, _ = self._iso()
        fut = iso.submit("ls", "t_1")
        iso.cancel_all("pane torn down")
        with pytest.raises(Exception, match="pane torn down"):
            fut.result(timeout=1)

    def test_concurrent_submitters_wait_independently(self):
        """Two threads submit to the same pane; the feeder resolves them
        in shell order while both wait concurrently — the second waiter
        must not be blocked behind the first's full execution."""
        iso, _ = self._iso()
        results = {}

        def worker(name, tag):
            fut = iso.submit(f"cmd_{name}", tag)
            results[name] = fut.result(timeout=5)

        t1 = threading.Thread(target=worker, args=("a", "t_a"))
        t2 = threading.Thread(target=worker, args=("b", "t_b"))
        t1.start(); t2.start()
        time.sleep(0.05)  # both submitted, both waiting

        iso.feed("===BEGIN_t_a===")
        iso.feed("alpha")
        iso.feed("===END_t_a=== EXIT:0")
        iso.feed("===BEGIN_t_b===")
        iso.feed("beta")
        iso.feed("===END_t_b=== EXIT:0")
        t1.join(timeout=5); t2.join(timeout=5)

        assert results["a"] == (0, "alpha")
        assert results["b"] == (0, "beta")


class TestFlag:
    def test_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("CLIVE_PANE_ISOLATION", raising=False)
        assert isolation_enabled() is False

    def test_enabled_via_env(self, monkeypatch):
        monkeypatch.setenv("CLIVE_PANE_ISOLATION", "1")
        assert isolation_enabled() is True


class TestDirectRunnerIntegration:
    """run_subtask_direct under CLIVE_PANE_ISOLATION=1 locks only the
    send and waits on its own exit-code file."""

    def _setup(self, tmp_path, monkeypatch, flag: bool):
        from unittest.mock import MagicMock
        from models import Subtask, PaneInfo

        if flag:
            monkeypatch.setenv("CLIVE_PANE_ISOLATION", "1")
        else:
            monkeypatch.delenv("CLIVE_PANE_ISOLATION", raising=False)

        session_dir = str(tmp_path)
        subtask = Subtask(
            id="d1", description="echo hi", pane="shell", mode="direct",
        )
        out_file = tmp_path / "_direct_d1.out"
        ec_file = tmp_path / "_direct_d1.ec"

        pane = MagicMock()

        def fake_send(cmd, enter=True):
            # Simulate the shell completing the redirect immediately.
            out_file.write_text("hi\n")
            ec_file.write_text("0\n")

        pane.send_keys.side_effect = fake_send
        pane_info = PaneInfo(pane=pane, app_type="shell", description="Bash", name="shell")
        return subtask, pane_info, session_dir, pane

    def test_isolated_path_skips_screen_wait(self, tmp_path, monkeypatch):
        from unittest.mock import patch
        subtask, pane_info, session_dir, pane = self._setup(tmp_path, monkeypatch, flag=True)

        with patch("executor.wait_for_ready") as mock_wait:
            from executor import run_subtask_direct
            result = run_subtask_direct(subtask, pane_info, session_dir=session_dir)

        mock_wait.assert_not_called()
        assert result.exit_code == 0
        assert "hi" in result.summary
        # Subshell wrapping for env isolation.
        sent = pane.send_keys.call_args[0][0]
        assert sent.startswith("( echo hi )")

    def test_default_path_unchanged(self, tmp_path, monkeypatch):
        from unittest.mock import patch
        subtask, pane_info, session_dir, pane = self._setup(tmp_path, monkeypatch, flag=False)

        with patch("executor.wait_for_ready") as mock_wait:
            mock_wait.return_value = ("screen", "marker")
            from executor import run_subtask_direct
            result = run_subtask_direct(subtask, pane_info, session_dir=session_dir)

        mock_wait.assert_called_once()
        assert result.exit_code == 0
        sent = pane.send_keys.call_args[0][0]
        assert sent.startswith("echo hi >")

    def test_isolation_not_applied_to_non_shell_panes(self, tmp_path, monkeypatch):
        from unittest.mock import patch
        subtask, pane_info, session_dir, pane = self._setup(tmp_path, monkeypatch, flag=True)
        pane_info.app_type = "agent"  # not shell-like → keep full locking

        with patch("executor.wait_for_ready") as mock_wait:
            mock_wait.return_value = ("screen", "marker")
            from executor import run_subtask_direct
            run_subtask_direct(subtask, pane_info, session_dir=session_dir)

        mock_wait.assert_called_once()
