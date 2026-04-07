"""Tests for output format flags."""
from prompts import build_summarizer_prompt


def test_default_summarizer():
    prompt = build_summarizer_prompt()
    assert "concise" in prompt.lower()
    assert "SINGLE LINE" not in prompt


def test_oneline_summarizer():
    prompt = build_summarizer_prompt("oneline")
    assert "SINGLE LINE" in prompt


def test_json_summarizer():
    prompt = build_summarizer_prompt("json")
    assert "JSON" in prompt
    assert '"result"' in prompt


def test_bool_summarizer():
    prompt = build_summarizer_prompt("bool")
    assert "YES or NO" in prompt
