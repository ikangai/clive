"""wrap_command heredoc safety (gh#40 live-eval finding).

Appending `; echo "EXIT:$? ..."` to a multi-line command whose last line
is a heredoc terminator corrupts the terminator (`EOF; echo ...` is not a
bare delimiter line), leaving the pane wedged in `heredoc>` continuation
mode and swallowing every subsequent command. For heredoc commands the
marker echo must go on its own line.
"""
from completion import wrap_command


def test_single_line_command_keeps_semicolon_join():
    wrapped, marker = wrap_command("ls -la", "t1")
    assert wrapped.startswith("ls -la; echo")
    assert marker in wrapped


def test_heredoc_command_marker_on_own_line():
    cmd = "cat <<EOF > f.txt\nhello\nEOF"
    wrapped, marker = wrap_command(cmd, "t2")
    lines = wrapped.splitlines()
    assert "EOF" in lines, "heredoc terminator must stay a bare line"
    # marker echo is a separate line after the terminator, not appended to it
    assert lines[-1].startswith('echo "EXIT:$?')
    assert not any(l.startswith("EOF;") for l in lines)


def test_heredoc_command_with_done_file_marker_on_own_line():
    cmd = "python3 <<PY\nprint(1)\nPY"
    wrapped, marker = wrap_command(cmd, "t3", done_file="/tmp/x.ec")
    lines = wrapped.splitlines()
    assert "PY" in lines
    assert lines[-1].startswith("_ec=$?") or lines[-1].startswith('_ec=')
