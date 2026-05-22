"""Subtask should carry an optional 'tools' list."""
import pytest
from models import Subtask


def test_subtask_accepts_tools():
    s = Subtask(id="1", description="x", pane="shell", mode="script",
                tools=["jq", "curl"])
    assert s.tools == ["jq", "curl"]


def test_subtask_tools_defaults_to_empty():
    s = Subtask(id="1", description="x", pane="shell", mode="script")
    assert s.tools == []


def test_subtask_serializes_round_trip():
    """JSON round-trip preserves the tools field."""
    import json
    from dataclasses import asdict
    s = Subtask(id="1", description="x", pane="shell", mode="script",
                tools=["jq"])
    js = json.dumps(asdict(s), default=str)
    data = json.loads(js)
    # Strip non-init / enum fields that don't round-trip via Subtask(**...)
    data.pop("status", None)
    data.pop("_retried", None)
    revived = Subtask(**data)
    assert revived.tools == ["jq"]
