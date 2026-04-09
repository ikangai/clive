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
