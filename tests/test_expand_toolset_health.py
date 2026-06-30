"""Runtime-expanded panes must be health-checked, not assumed ready.

`_expand_toolset` adds panes via `add_pane` when a category is loaded at
runtime (`/add` REPL command or planner-triggered expansion). Historically it
marked every such pane `status='ready'` unconditionally, so a tool that fails
to launch (an SSH remote that never connects, a TUI that crashes on startup)
was reported ready and the planner routed subtasks to a dead pane.

These tests pin that the status now comes from `check_health` — which both
reports actual `[AGENT_READY]` readiness and respawns DEAD panes — so a runtime
expansion gets the same self-healing as the initial `setup_session` path.
"""
import clive_core
from models import PaneInfo


def _make_ctx():
    return {
        "session": object(),  # add_pane is monkeypatched; never really used
        "session_dir": "/tmp/clive-test",
        "panes": {},
        "tool_status": {},
        "available_cmds": [],
        "missing_cmds": [],
        "endpoints": [],
        "unconfigured": [],
        "categories": {"core"},
    }


def _patch_registry(monkeypatch):
    """A tiny, hermetic category->pane registry so the test is independent of
    the real toolset contents (no commands/endpoints/config to probe)."""
    monkeypatch.setattr(
        clive_core, "CATEGORIES",
        {"testcat": {"panes": ["testpane"], "commands": [], "endpoints": []}},
    )
    monkeypatch.setattr(
        clive_core, "PANES",
        {"testpane": {"name": "testpane", "app_type": "shell",
                      "description": "test pane"}},
    )


def test_expand_records_unavailable_when_health_reports_unavailable(monkeypatch):
    _patch_registry(monkeypatch)
    fake_pane = PaneInfo(pane=None, app_type="shell",
                         description="test pane", name="testpane")
    monkeypatch.setattr(clive_core, "add_pane",
                        lambda session, pane_def, session_dir: fake_pane)

    seen = {}

    def fake_check_health(panes):
        seen["panes"] = panes
        return {name: {"status": "unavailable",
                       "app_type": info.app_type,
                       "description": info.description}
                for name, info in panes.items()}

    monkeypatch.setattr(clive_core, "check_health", fake_check_health)

    ctx = _make_ctx()
    expanded = clive_core._expand_toolset("testcat", ctx)

    assert expanded is True
    # check_health ran on exactly the newly-added pane
    assert seen["panes"] == {"testpane": fake_pane}
    # the dead pane is reported unavailable, NOT assumed ready
    assert ctx["tool_status"]["testpane"]["status"] == "unavailable"


def test_expand_records_ready_when_health_reports_ready(monkeypatch):
    _patch_registry(monkeypatch)
    fake_pane = PaneInfo(pane=None, app_type="shell",
                         description="test pane", name="testpane")
    monkeypatch.setattr(clive_core, "add_pane",
                        lambda session, pane_def, session_dir: fake_pane)

    def fake_check_health(panes):
        return {name: {"status": "ready",
                       "app_type": info.app_type,
                       "description": info.description}
                for name, info in panes.items()}

    monkeypatch.setattr(clive_core, "check_health", fake_check_health)

    ctx = _make_ctx()
    expanded = clive_core._expand_toolset("testcat", ctx)

    assert expanded is True
    assert ctx["tool_status"]["testpane"]["status"] == "ready"
