"""Each COMMANDS entry must expose a compact 'card' suitable for Tier 2."""
import pytest
from toolsets import COMMANDS

def test_every_command_has_a_card():
    missing = [name for name, defn in COMMANDS.items()
               if not defn.get("card")]
    assert missing == [], f"commands without 'card' field: {missing}"

def test_card_is_compact():
    """A card is a reference snippet, not a paragraph. ≤200 chars."""
    too_long = {name: len(defn["card"])
                for name, defn in COMMANDS.items()
                if len(defn["card"]) > 200}
    assert too_long == {}, f"cards too long: {too_long}"

def test_card_starts_with_tool_name():
    """Convention: '[name] one-line synopsis\n  usage examples...'"""
    bad = [name for name, defn in COMMANDS.items()
           if not defn["card"].startswith(f"[{name}]")]
    assert bad == [], f"cards missing [name] prefix: {bad}"
