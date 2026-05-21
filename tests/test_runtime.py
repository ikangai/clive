"""Tests for runtime.py shared primitives."""
import threading
import pytest


class TestRuntimeImport:
    def test_clean_import(self):
        """runtime.py imports without error and has no circular deps."""
        import runtime
        assert hasattr(runtime, '_pane_locks')
        assert hasattr(runtime, '_cancel_event')
        assert hasattr(runtime, 'cancel')
        assert hasattr(runtime, 'is_cancelled')
        assert hasattr(runtime, 'reset_cancel')
        assert hasattr(runtime, '_emit')
        assert hasattr(runtime, 'BLOCKED_COMMANDS')
        assert hasattr(runtime, '_check_command_safety')
        assert hasattr(runtime, '_wrap_for_sandbox')
        assert hasattr(runtime, 'write_file')
        assert hasattr(runtime, '_extract_script')

    def test_pane_locks_type(self):
        import runtime
        assert isinstance(runtime._pane_locks, dict)


class TestCancelLifecycle:
    def setup_method(self):
        import runtime
        runtime.reset_cancel()

    def test_initially_not_cancelled(self):
        import runtime
        assert not runtime.is_cancelled()

    def test_cancel_sets_event(self):
        import runtime
        runtime.cancel()
        assert runtime.is_cancelled()

    def test_reset_clears_event(self):
        import runtime
        runtime.cancel()
        runtime.reset_cancel()
        assert not runtime.is_cancelled()

    def test_cancel_event_is_threading_event(self):
        import runtime
        assert isinstance(runtime._cancel_event, threading.Event)


class TestEmit:
    def test_emit_calls_callback(self):
        import runtime
        calls = []
        runtime._emit(lambda *a: calls.append(a), "turn", "s1", 1, "ls")
        assert calls == [("turn", "s1", 1, "ls")]

    def test_emit_none_callback(self):
        import runtime
        # Should not raise
        runtime._emit(None, "turn", "s1", 1, "ls")

    def test_emit_swallows_callback_error(self):
        import runtime
        def bad(*a):
            raise ValueError("boom")
        # Should not raise
        runtime._emit(bad, "turn", "s1", 1)


class TestCheckCommandSafety:
    def test_safe_command(self):
        import runtime
        assert runtime._check_command_safety("ls -la") is None

    def test_blocked_rm_rf_root(self):
        import runtime
        result = runtime._check_command_safety("rm -rf /")
        assert result is not None
        assert "Blocked" in result

    def test_blocked_fork_bomb(self):
        import runtime
        result = runtime._check_command_safety(":(){ :|:& };:")
        assert result is not None

    def test_blocked_shutdown(self):
        import runtime
        result = runtime._check_command_safety("shutdown -h now")
        assert result is not None


class TestCheckCommandSafetyH10Regressions:
    """Bug H10 (2026-05-20): bypasses the old regex-only blocklist missed,
    and false positives that the new structured check now allows through."""

    # ─── Bypasses that must now be caught ─────────────────────────────────

    @pytest.mark.parametrize("cmd", [
        "rm -fr /",                    # flag order swapped
        "rm -rfv /",                   # extra flag char
        "rm -rf / # comment",          # trailing comment
        "rm -rf / && echo done",       # shell sequence
        "sudo rm -rf /",               # sudo prefix
        "rm -rf /home/alice",          # specific home subdir
        "rm -rf /Users/alice",         # macOS equivalent
        "chmod 0777 /",                # octal mode with leading 0
        "chmod 777 / && ls",           # trailing sequence
        "while :; do echo x; done",    # `:` as null command
        "ls /tmp && rm -rf /",         # rm in second segment
        "cat file | rm -rf /",         # rm via pipe
        "dd if=/dev/zero of=/dev/sda", # disk wipe
        "mkfs.ext4 /dev/sda1",         # mkfs.* variant
    ])
    def test_bypass_now_blocked(self, cmd):
        import runtime
        result = runtime._check_command_safety(cmd)
        assert result is not None, f"expected to block: {cmd!r}"
        assert "Blocked" in result

    # ─── False positives that must now be allowed ─────────────────────────

    @pytest.mark.parametrize("cmd", [
        "echo 'shutdown sequence initiated'",
        "grep shutdown /var/log/syslog",
        "ls /sbin/shutdown",
        "echo 'I will reboot now'",
        "rm -rf /tmp/foo",             # subdir under /tmp is fine
        "rm -rf ./old",                # relative path
        "chmod 755 /etc/passwd",       # 777-only is blocked, 755 fine
        "echo halt",                   # halt as argument, not command
        "dd if=/dev/zero of=/tmp/foo", # /tmp target is fine
        "dd if=/dev/urandom of=/dev/null count=10",  # /dev/null exclusion
    ])
    def test_benign_not_falsely_blocked(self, cmd):
        import runtime
        assert runtime._check_command_safety(cmd) is None, \
            f"false positive on: {cmd!r}"


class TestWrapForSandbox:
    def test_no_sandbox(self):
        import runtime
        import os
        os.environ.pop("CLIVE_SANDBOX", None)
        cmd = runtime._wrap_for_sandbox("ls", "/tmp/clive", sandboxed=False)
        assert cmd == "ls"

    def test_sandbox_enabled(self):
        import runtime
        cmd = runtime._wrap_for_sandbox("ls", "/tmp/clive", sandboxed=True)
        assert "sandbox" in cmd
        assert "run.sh" in cmd


class TestWriteFile:
    def test_write_and_read(self, tmp_path):
        import runtime
        path = str(tmp_path / "test.txt")
        result = runtime.write_file(path, "hello")
        assert "[Written:" in result
        with open(path) as f:
            assert f.read() == "hello"

    def test_write_creates_dirs(self, tmp_path):
        import runtime
        path = str(tmp_path / "sub" / "dir" / "test.txt")
        result = runtime.write_file(path, "nested")
        assert "[Written:" in result

    def test_write_error(self):
        import runtime
        result = runtime.write_file("/proc/nonexistent/file", "x")
        assert "[Error" in result


class TestExtractScript:
    def test_fenced_bash(self):
        import runtime
        text = "Here is the script:\n```bash\necho hello\n```"
        assert runtime._extract_script(text) == "echo hello"

    def test_fenced_python(self):
        import runtime
        text = "```python\nprint('hi')\n```"
        assert runtime._extract_script(text) == "print('hi')"

    def test_shebang(self):
        import runtime
        text = "#!/bin/bash\necho hello"
        assert "echo hello" in runtime._extract_script(text)

    def test_no_script_raises(self):
        import runtime
        with pytest.raises(ValueError):
            runtime._extract_script("no script here")
