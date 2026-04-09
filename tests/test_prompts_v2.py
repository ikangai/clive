# tests/test_prompts_v2.py
"""Tests for the refocused prompts."""
from prompts import build_script_prompt_v2, build_interactive_prompt_v2


class TestScriptPromptV2:
    def test_contains_professional_framing(self):
        p = build_script_prompt_v2("count files", "shell", "shell", "Bash shell", "")
        assert "professional" in p.lower() or "skilled" in p.lower() or "engineer" in p.lower()

    def test_contains_task(self):
        p = build_script_prompt_v2("count .py files", "shell", "shell", "Bash shell", "")
        assert "count .py files" in p

    def test_contains_driver(self):
        p = build_script_prompt_v2("do stuff", "shell", "shell", "Bash shell", "")
        assert "bash" in p.lower() or "shell" in p.lower()

    def test_no_xml_in_template(self):
        # Use a nonexistent driver to isolate the template from driver content
        p = build_script_prompt_v2("do stuff", "shell", "nonexistent_driver_xyz", "Bash shell", "")
        assert "<cmd" not in p

    def test_contains_session_dir(self):
        p = build_script_prompt_v2("do stuff", "shell", "shell", "Bash shell", "",
                                   session_dir="/tmp/clive/abc")
        assert "/tmp/clive/abc" in p

    def test_shorter_than_old(self):
        from prompts import build_script_prompt
        old = build_script_prompt("do stuff", "shell", "shell", "Bash shell", "")
        new = build_script_prompt_v2("do stuff", "shell", "shell", "Bash shell", "")
        # v2 should be comparable or shorter -- not bloated
        assert len(new) < len(old) * 1.2

    def test_dep_context_included(self):
        p = build_script_prompt_v2("do stuff", "shell", "shell", "Bash shell",
                                   "Dep [1] DONE: got 3 files")
        assert "got 3 files" in p


class TestInteractivePromptV2:
    def test_contains_observation_framing(self):
        p = build_interactive_prompt_v2("explore logs", "shell", "shell", "Bash shell", "")
        # Should frame as observation/investigation, not "autonomous agent"
        assert "observe" in p.lower() or "screen" in p.lower() or "see" in p.lower()

    def test_contains_done_signal(self):
        p = build_interactive_prompt_v2("do stuff", "shell", "shell", "Bash shell", "")
        assert "DONE:" in p

    def test_no_xml_in_template(self):
        # Use a nonexistent driver to isolate the template from driver content
        p = build_interactive_prompt_v2("do stuff", "shell", "nonexistent_driver_xyz", "Bash shell", "")
        assert "<cmd" not in p
        assert "</cmd>" not in p

    def test_contains_session_dir(self):
        p = build_interactive_prompt_v2("do stuff", "shell", "shell", "Bash shell", "",
                                       session_dir="/tmp/clive/abc")
        assert "/tmp/clive/abc" in p

    def test_much_shorter_than_old_worker(self):
        from prompts import build_worker_prompt
        old = build_worker_prompt("do stuff", "shell", "shell", "Bash shell", "")
        new = build_interactive_prompt_v2("do stuff", "shell", "shell", "Bash shell", "")
        assert len(new) < len(old) * 0.7  # at least 30% shorter
