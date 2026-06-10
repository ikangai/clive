"""Tests for token-budget-triggered context squashing (gh#6)."""
from unittest.mock import MagicMock, patch

from context_compress import maybe_squash
from models import Subtask, SubtaskStatus, PaneInfo


def _make_conversation(n_turns, with_system=True):
    """Build a conversation with n user/assistant pairs."""
    messages = []
    if with_system:
        messages.append({"role": "system", "content": "system prompt"})
    for i in range(n_turns):
        messages.append({"role": "user", "content": f"screen {i}"})
        messages.append({"role": "assistant", "content": f"command {i}"})
    return messages


def _fake_compress(text):
    return "squashed summary"


def test_below_threshold_unchanged():
    """No squash while token spend is under the trigger threshold."""
    msgs = _make_conversation(8)
    result, squashed = maybe_squash(
        msgs, tokens_used=10_000, token_budget=50_000, turn=8,
        squash_count=0, compress_fn=_fake_compress,
    )
    assert result is msgs
    assert squashed is False


def test_above_threshold_squashes():
    """At >=70% of budget with enough history, old turns are squashed."""
    msgs = _make_conversation(8)
    result, squashed = maybe_squash(
        msgs, tokens_used=36_000, token_budget=50_000, turn=8,
        squash_count=0, compress_fn=_fake_compress,
    )
    assert squashed is True
    assert result[0]["role"] == "system"
    assert "[Earlier conversation summary]" in result[1]["content"]
    assert "squashed summary" in result[1]["content"]
    # keep_recent=2 → last 2 user/assistant pairs verbatim
    recent = result[2:]
    assert len(recent) == 4
    assert "screen 6" in recent[0]["content"]
    assert "screen 7" in recent[2]["content"]


def test_min_turns_guard():
    """Never squash before min_turns — not enough history to compress."""
    msgs = _make_conversation(4)
    result, squashed = maybe_squash(
        msgs, tokens_used=49_000, token_budget=50_000, turn=4,
        squash_count=0, compress_fn=_fake_compress,
    )
    assert result is msgs
    assert squashed is False


def test_max_squashes_guard():
    """At the squash cap, no further squashes happen."""
    msgs = _make_conversation(8)
    result, squashed = maybe_squash(
        msgs, tokens_used=49_000, token_budget=50_000, turn=8,
        squash_count=2, compress_fn=_fake_compress,
    )
    assert result is msgs
    assert squashed is False


def test_short_history_not_squashed():
    """Above threshold but only keep_recent+1 user turns — nothing to squash."""
    msgs = _make_conversation(3)
    result, squashed = maybe_squash(
        msgs, tokens_used=49_000, token_budget=50_000, turn=6,
        squash_count=0, compress_fn=_fake_compress,
    )
    assert result is msgs
    assert squashed is False


def test_no_compressor_no_squash():
    """Without a compress_fn there is no summarizer — skip squashing."""
    msgs = _make_conversation(8)
    result, squashed = maybe_squash(
        msgs, tokens_used=49_000, token_budget=50_000, turn=8,
        squash_count=0, compress_fn=None,
    )
    assert result is msgs
    assert squashed is False


def test_zero_budget_no_squash():
    """A zero/disabled budget never triggers squashing."""
    msgs = _make_conversation(8)
    result, squashed = maybe_squash(
        msgs, tokens_used=49_000, token_budget=0, turn=8,
        squash_count=0, compress_fn=_fake_compress,
    )
    assert result is msgs
    assert squashed is False


def test_keep_recent_override():
    """keep_recent controls how many recent pairs survive verbatim."""
    msgs = _make_conversation(10)
    result, squashed = maybe_squash(
        msgs, tokens_used=49_000, token_budget=50_000, turn=10,
        squash_count=0, compress_fn=_fake_compress, keep_recent=3,
    )
    assert squashed is True
    recent = result[2:]
    assert len(recent) == 6
    assert "screen 7" in recent[0]["content"]


def test_second_squash_waits_for_new_history():
    """Right after a squash, history is short — squash #2 only fires
    once enough new turns accumulate again."""
    msgs = _make_conversation(8)
    once, squashed = maybe_squash(
        msgs, tokens_used=40_000, token_budget=50_000, turn=8,
        squash_count=0, compress_fn=_fake_compress,
    )
    assert squashed is True
    # Immediately re-squashing the compressed history is a no-op.
    twice, squashed_again = maybe_squash(
        once, tokens_used=41_000, token_budget=50_000, turn=9,
        squash_count=1, compress_fn=_fake_compress,
    )
    assert twice is once
    assert squashed_again is False


def test_compressor_failure_falls_back_gracefully():
    """A failing compress_fn must not crash the turn loop."""
    msgs = _make_conversation(8)

    def boom(text):
        raise RuntimeError("llm down")

    result, squashed = maybe_squash(
        msgs, tokens_used=49_000, token_budget=50_000, turn=8,
        squash_count=0, compress_fn=boom,
    )
    # compress_context falls back to trim on compressor failure; either
    # way the call returns a usable message list and reports the outcome
    # honestly.
    assert isinstance(result, list)
    assert all("role" in m for m in result)


class TestRunnerIntegration:
    """The interactive runner squashes under token pressure and reports it."""

    @patch("interactive_runner.chat_stream", side_effect=Exception("force fallback"))
    @patch("interactive_runner.chat")
    @patch("interactive_runner.capture_pane")
    @patch("interactive_runner.wait_for_ready")
    @patch("interactive_runner.make_llm_compressor")
    def test_squash_fires_under_token_pressure(
        self, mock_mk_compressor, mock_wait, mock_capture, mock_chat, mock_stream
    ):
        mock_mk_compressor.return_value = lambda text: "squashed summary"
        mock_capture.return_value = "[AGENT_READY] $ "
        mock_wait.return_value = ("[AGENT_READY] $ ", "marker")
        # 10k tokens/turn against a 50k budget -> 70% threshold crossed
        # before the min_turns=5 guard releases at turn 5.
        mock_chat.return_value = ("```bash\nls\n```", 9_000, 1_000)

        pane = MagicMock()
        pane.cmd.return_value = MagicMock(stdout=["[AGENT_READY] $ "])
        pane_info = PaneInfo(
            pane=pane, app_type="shell", description="Bash", name="shell",
            observation_model="cheap-model",
        )
        subtask = Subtask(
            id="1", description="long task", pane="shell",
            mode="interactive", max_turns=8,
        )
        events = []

        from executor import run_subtask_interactive
        result = run_subtask_interactive(
            subtask=subtask,
            pane_info=pane_info,
            dep_context="",
            on_event=lambda *a: events.append(a),
            token_budget=50_000,
        )

        squash_events = [e for e in events if e[0] == "squash"]
        assert squash_events, "expected at least one squash event"
        assert len(squash_events) <= 2, "squash cap is 2 per subtask"
        assert all(e[2] >= 5 for e in squash_events), "no squash before turn 5"
        # Exhausted-turns summary carries the squash metadata.
        assert result.status == SubtaskStatus.FAILED
        assert "squashed at turn" in result.summary

    @patch("interactive_runner.chat_stream", side_effect=Exception("force fallback"))
    @patch("interactive_runner.chat")
    @patch("interactive_runner.capture_pane")
    @patch("interactive_runner.wait_for_ready")
    @patch("interactive_runner.make_llm_compressor")
    def test_no_squash_under_budget(
        self, mock_mk_compressor, mock_wait, mock_capture, mock_chat, mock_stream
    ):
        mock_mk_compressor.return_value = lambda text: "squashed summary"
        mock_capture.return_value = "[AGENT_READY] $ "
        mock_wait.return_value = ("[AGENT_READY] $ ", "marker")
        # 150 tokens/turn never approaches the 50k budget.
        mock_chat.return_value = ("```bash\nls\n```", 100, 50)

        pane = MagicMock()
        pane.cmd.return_value = MagicMock(stdout=["[AGENT_READY] $ "])
        pane_info = PaneInfo(
            pane=pane, app_type="shell", description="Bash", name="shell",
            observation_model="cheap-model",
        )
        subtask = Subtask(
            id="1", description="long task", pane="shell",
            mode="interactive", max_turns=8,
        )
        events = []

        from executor import run_subtask_interactive
        result = run_subtask_interactive(
            subtask=subtask,
            pane_info=pane_info,
            dep_context="",
            on_event=lambda *a: events.append(a),
            token_budget=50_000,
        )

        assert not [e for e in events if e[0] == "squash"]
        assert "squashed" not in result.summary
