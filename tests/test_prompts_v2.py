# tests/test_prompts_v2.py
"""Tests for the refocused prompts."""
from prompts import build_script_prompt, build_interactive_prompt


class TestScriptPrompt:
    def test_contains_professional_framing(self):
        p = build_script_prompt("count files", "shell", "shell", "Bash shell", "")
        assert "professional" in p.lower() or "skilled" in p.lower() or "engineer" in p.lower()

    def test_contains_task(self):
        p = build_script_prompt("count .py files", "shell", "shell", "Bash shell", "")
        assert "count .py files" in p

    def test_contains_driver(self):
        p = build_script_prompt("do stuff", "shell", "shell", "Bash shell", "")
        assert "bash" in p.lower() or "shell" in p.lower()

    def test_no_xml_in_template(self):
        # Use a nonexistent driver to isolate the template from driver content
        p = build_script_prompt("do stuff", "shell", "nonexistent_driver_xyz", "Bash shell", "")
        assert "<cmd" not in p

    def test_contains_session_dir(self):
        p = build_script_prompt("do stuff", "shell", "shell", "Bash shell", "",
                                session_dir="/tmp/clive/abc")
        assert "/tmp/clive/abc" in p

    def test_dep_context_included(self):
        p = build_script_prompt("do stuff", "shell", "shell", "Bash shell",
                                "Dep [1] DONE: got 3 files")
        assert "got 3 files" in p

    def test_warns_against_interactive_blocking(self):
        # Script mode has NO observation during execution, so a command that
        # blocks on an interactive prompt wedges the pane (#44). The prompt
        # must tell the model the script must never wait for input.
        p = build_script_prompt("install jq and parse data", "shell", "shell",
                                 "Bash shell", "")
        low = p.lower()
        assert "non-interactive" in low or "interactive" in low
        assert "block" in low or "wait" in low or "prompt" in low

    def test_suggests_non_interactive_flags(self):
        # The model should be steered toward auto-confirm flags rather than
        # commands that stop on a [Y/n] confirmation.
        p = build_script_prompt("install a package", "shell", "shell",
                                 "Bash shell", "")
        assert "-y" in p or "--yes" in p or "--noconfirm" in p or "--no-input" in p

    def test_suggests_stdin_and_pager_neutralization(self):
        # App-level pagers/REPLs that tesla's env backstop can't cover must be
        # neutralized: feed /dev/null on stdin, use --no-pager / pipe to cat.
        p = build_script_prompt("query a tool that may page output", "shell",
                                 "shell", "Bash shell", "")
        assert "/dev/null" in p
        assert "--no-pager" in p or "| cat" in p


class TestInteractivePrompt:
    def test_contains_observation_framing(self):
        p = build_interactive_prompt("explore logs", "shell", "shell", "Bash shell", "")
        # Should frame as observation/investigation, not "autonomous agent"
        assert "observe" in p.lower() or "screen" in p.lower() or "see" in p.lower()

    def test_contains_done_signal(self):
        p = build_interactive_prompt("do stuff", "shell", "shell", "Bash shell", "")
        assert "DONE:" in p

    def test_no_xml_in_template(self):
        # Use a nonexistent driver to isolate the template from driver content
        p = build_interactive_prompt("do stuff", "shell", "nonexistent_driver_xyz", "Bash shell", "")
        assert "<cmd" not in p
        assert "</cmd>" not in p

    def test_contains_session_dir(self):
        p = build_interactive_prompt("do stuff", "shell", "shell", "Bash shell", "",
                                    session_dir="/tmp/clive/abc")
        assert "/tmp/clive/abc" in p
