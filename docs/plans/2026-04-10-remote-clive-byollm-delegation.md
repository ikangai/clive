# Remote Clive BYOLLM via Inference Delegation — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make "bring your own LLM" work for remote clives when the local LLM is LMStudio or Ollama (today it silently misroutes to the remote host's localhost), by routing inference requests from the remote clive back over the existing conversational channel to the outer clive. No tunneling, no new auth, no network changes on the remote.

**Architecture:**
1. **Framed protocol foundation** (prerequisite). Replace line-prefix `TURN:`/`CONTEXT:`/`QUESTION:`/`FILE:` parsing with a self-delimiting sentinel format — `<<<CLIVE:{kind}:{base64(json(payload))}>>>` — so protocol messages cannot be spoofed by stray tool output or injected via LLM-generated content. This is the load-bearing change that makes delegation safe.
2. **Delegate LLM provider.** Add `delegate` to `llm.py`'s `PROVIDERS`. When active, every `chat()` call emits an `llm_request` frame on stdout and blocks on stdin until an `llm_response` (or `llm_error`) frame arrives. The outer clive's pane read loop detects `llm_request` frames and answers them by calling its own (local LMStudio/Ollama/anything) LLM and typing an `llm_response` frame into the pane via `send_keys`.
3. **SSH wiring + quality-of-life.** `agents.build_agent_ssh_cmd()` auto-injects `LLM_PROVIDER=delegate` when the outer is on a local provider, forwards an optional `LLM_BASE_URL`/`GOOGLE_API_KEY`, enables SSH `ControlMaster` connection reuse, and an `agents doctor` subcommand catches the silent SendEnv misconfig class of bug.

**Tech Stack:** Python 3.12, pytest, libtmux, openai/anthropic Python SDKs, SSH (OpenSSH 8+), tmux 3.2+. No new dependencies.

**Scope boundaries (explicitly out of scope for this plan — do not implement):**
- `clive remote install <host>` bootstrap command — separate plan.
- Mode A / Mode B reconciliation with `ssh_entrypoint.sh` — separate plan.
- Streaming delegation — Phase 2 ships non-streaming first; streaming is a follow-up.
- Backwards compatibility with the old line-prefix protocol — cut over hard. All conversational sessions are internal (clive ↔ clive); no external consumers.

**Verification strategy:** Every task is TDD — write the failing test first, watch it fail, implement, watch it pass, commit. Integration tests use a mock LLM HTTP server and a subprocess-piped inner clive (no real SSH required for CI). A final end-to-end manual smoke test against a real LMStudio is in the final task.

---

## Phase 1 — Framed protocol foundation

### Task 1: Create `protocol.py` with encode/decode

**Files:**
- Create: `protocol.py`
- Create: `tests/test_protocol.py`

**Why:** The existing line-prefix protocol (`line.startswith("TURN:")`) is unsafe the moment LLM-generated text or tool output contains the literal string `TURN: done` at column 0. This blocks delegation because delegated `llm_request` payloads contain arbitrary user content. Base64-wrapping the JSON payload makes the sentinel unambiguous: the frame markers `<<<CLIVE:` and `>>>` cannot appear inside base64.

**Step 1: Write the failing test**

```python
# tests/test_protocol.py
import base64
import json

from protocol import encode, decode_all, Frame


def test_encode_produces_expected_shape():
    out = encode("turn", {"state": "done"})
    assert out.startswith("<<<CLIVE:turn:")
    assert out.endswith(">>>")
    # Payload must be base64-encoded JSON
    b64 = out[len("<<<CLIVE:turn:"):-len(">>>")]
    assert json.loads(base64.b64decode(b64).decode()) == {"state": "done"}


def test_decode_single_frame():
    screen = "random output\n" + encode("turn", {"state": "thinking"}) + "\nmore output\n"
    frames = decode_all(screen)
    assert len(frames) == 1
    assert frames[0] == Frame(kind="turn", payload={"state": "thinking"})


def test_decode_multiple_frames_preserves_order():
    screen = "\n".join([
        encode("turn", {"state": "thinking"}),
        "some shell output",
        encode("context", {"result": "ok"}),
        encode("turn", {"state": "done"}),
    ])
    frames = decode_all(screen)
    assert [f.kind for f in frames] == ["turn", "context", "turn"]
    assert frames[-1].payload == {"state": "done"}


def test_decode_ignores_stray_text_that_looks_like_sentinel():
    # A tool printing the literal string <<<CLIVE:turn:done>>> (no valid base64)
    # must not be parsed as a frame.
    screen = "<<<CLIVE:turn:done>>>\n"  # 'done' is not valid base64
    frames = decode_all(screen)
    assert frames == []


def test_decode_tolerates_partial_frame_at_start():
    # Simulates tmux scrollback truncation mid-frame.
    partial = "CLIVE:turn:" + "eyJzdGF0ZSI6ImRvbmUifQ==" + ">>>"
    good = encode("turn", {"state": "done"})
    frames = decode_all(partial + "\n" + good)
    assert len(frames) == 1
    assert frames[0].payload == {"state": "done"}


def test_decode_rejects_non_dict_payload():
    import base64
    bad = "<<<CLIVE:turn:" + base64.b64encode(b'"just a string"').decode() + ">>>"
    frames = decode_all(bad)
    assert frames == []


def test_kinds_is_the_source_of_truth():
    from protocol import KINDS
    assert {"turn", "context", "question", "file", "progress",
            "llm_request", "llm_response", "llm_error", "alive"} <= set(KINDS)
```

**Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_protocol.py -v`
Expected: FAIL with `ImportError: No module named 'protocol'`.

**Step 3: Implement `protocol.py`**

```python
# protocol.py
"""Framed conversational protocol for clive-to-clive communication.

Frame format (single line, self-delimiting):

    <<<CLIVE:{kind}:{base64(json(payload))}>>>

The base64 wrapping guarantees that protocol sentinels cannot be spoofed
by stray tool output or LLM-generated text: the marker characters
('<', '>', ':') cannot appear inside a base64-encoded payload, so a
partial match on the literal sentinel string will fail to decode and
be dropped.

Replaces the legacy line-prefix parsing in remote.py.
"""
from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass

_PREFIX = "<<<CLIVE:"
_SUFFIX = ">>>"

# Source of truth for frame kinds. Anything else is rejected at decode time.
KINDS = frozenset({
    "turn",          # payload: {"state": "thinking|waiting|done|failed"}
    "context",       # payload: arbitrary dict (result, error, etc.)
    "question",      # payload: {"text": "..."}
    "file",          # payload: {"name": "..."}
    "progress",      # payload: {"text": "..."}
    "llm_request",   # payload: {"id": "...", "messages": [...], "model": "...", "max_tokens": N}
    "llm_response",  # payload: {"id": "...", "content": "...", "prompt_tokens": N, "completion_tokens": N}
    "llm_error",     # payload: {"id": "...", "error": "..."}
    "alive",         # payload: {"ts": <float>}
})

# Strict frame regex: kind is alphanumeric/underscore, payload is base64 alphabet.
_FRAME_RE = re.compile(
    r"<<<CLIVE:(?P<kind>[a-z_]+):(?P<b64>[A-Za-z0-9+/=]+)>>>"
)


@dataclass(frozen=True)
class Frame:
    kind: str
    payload: dict


def encode(kind: str, payload: dict) -> str:
    """Encode a payload as a framed protocol message.

    Returns a single line (no trailing newline) suitable for print() with flush.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown frame kind: {kind!r}")
    if not isinstance(payload, dict):
        raise TypeError(f"payload must be dict, got {type(payload).__name__}")
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    b64 = base64.b64encode(raw).decode("ascii")
    return f"{_PREFIX}{kind}:{b64}{_SUFFIX}"


def decode_all(screen: str) -> list[Frame]:
    """Extract all valid frames from a screen blob, in order of appearance.

    Silently drops:
      - frames with unknown kinds
      - frames whose payload is not valid base64
      - frames whose decoded payload is not valid JSON
      - frames whose payload is not a JSON object
    """
    frames: list[Frame] = []
    for m in _FRAME_RE.finditer(screen):
        kind = m.group("kind")
        if kind not in KINDS:
            continue
        try:
            raw = base64.b64decode(m.group("b64"), validate=True)
        except (binascii.Error, ValueError):
            continue
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        frames.append(Frame(kind=kind, payload=payload))
    return frames


def latest(frames: list[Frame], kind: str) -> Frame | None:
    """Return the most recent frame of a given kind, or None."""
    for f in reversed(frames):
        if f.kind == kind:
            return f
    return None
```

**Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_protocol.py -v`
Expected: PASS, 7 tests.

**Step 5: Commit**

```bash
git add protocol.py tests/test_protocol.py
git commit -m "feat(protocol): add framed sentinel protocol for clive-to-clive messages"
```

---

### Task 2: Migrate emitters in `output.py` to framed protocol

**Files:**
- Modify: `output.py:185-201` (the `# --- Conversational protocol ---` section)
- Modify: `tests/test_output_conversational.py`

**Step 1: Update the existing emit-test to expect frames**

Read `tests/test_output_conversational.py` first to see its current shape, then update assertions:

```python
# tests/test_output_conversational.py  (full replacement body for existing tests)
import base64
import json

from output import emit_turn, emit_context, emit_question, emit_file, emit_progress, emit_alive


def _parse_frame(captured: str) -> tuple[str, dict]:
    line = captured.strip().splitlines()[-1]
    assert line.startswith("<<<CLIVE:") and line.endswith(">>>")
    body = line[len("<<<CLIVE:"):-len(">>>")]
    kind, b64 = body.split(":", 1)
    payload = json.loads(base64.b64decode(b64).decode())
    return kind, payload


def test_emit_turn(capsys):
    emit_turn("done")
    kind, payload = _parse_frame(capsys.readouterr().out)
    assert kind == "turn"
    assert payload == {"state": "done"}


def test_emit_context(capsys):
    emit_context({"result": "42"})
    kind, payload = _parse_frame(capsys.readouterr().out)
    assert kind == "context"
    assert payload == {"result": "42"}


def test_emit_question(capsys):
    emit_question("which one?")
    kind, payload = _parse_frame(capsys.readouterr().out)
    assert kind == "question"
    assert payload == {"text": "which one?"}


def test_emit_file(capsys):
    emit_file("out.txt")
    kind, payload = _parse_frame(capsys.readouterr().out)
    assert kind == "file"
    assert payload == {"name": "out.txt"}


def test_emit_progress(capsys):
    emit_progress("step 1 of 3")
    kind, payload = _parse_frame(capsys.readouterr().out)
    assert kind == "progress"
    assert payload == {"text": "step 1 of 3"}


def test_emit_alive_includes_timestamp(capsys):
    emit_alive()
    kind, payload = _parse_frame(capsys.readouterr().out)
    assert kind == "alive"
    assert isinstance(payload["ts"], float)
```

**Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_output_conversational.py -v`
Expected: FAIL — emitters still produce `TURN: done` instead of framed output; some functions (`emit_file`, `emit_progress`, `emit_alive`) don't exist yet.

**Step 3: Rewrite the conversational section of `output.py`**

Replace lines 185–201 of `output.py` with:

```python
# --- Conversational protocol ---

def emit_turn(state: str):
    """Emit a framed turn-state message. States: thinking, waiting, done, failed."""
    from protocol import encode
    print(encode("turn", {"state": state}), flush=True)


def emit_context(data: dict):
    """Emit a framed context message with an arbitrary JSON-serializable dict."""
    from protocol import encode
    print(encode("context", data), flush=True)


def emit_question(question: str):
    """Emit a framed question message."""
    from protocol import encode
    print(encode("question", {"text": question}), flush=True)


def emit_file(name: str):
    """Emit a framed file-available message."""
    from protocol import encode
    print(encode("file", {"name": name}), flush=True)


def emit_progress(text: str):
    """Emit a framed progress message."""
    from protocol import encode
    print(encode("progress", {"text": text}), flush=True)


def emit_alive():
    """Emit a framed keepalive message with current wall-clock timestamp."""
    import time
    from protocol import encode
    print(encode("alive", {"ts": time.time()}), flush=True)
```

**Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_output_conversational.py -v`
Expected: PASS, 6 tests.

**Step 5: Commit**

```bash
git add output.py tests/test_output_conversational.py
git commit -m "feat(output): emit framed conversational protocol messages"
```

---

### Task 3: Migrate parsers in `remote.py` to framed protocol

**Files:**
- Modify: `remote.py:27-105` (delete `parse_remote_result`, rewrite the four `parse_*` functions)
- Modify: `tests/test_remote.py` (replace line-prefix fixtures with framed fixtures)
- Modify: `tests/test_agent_conversation.py`
- Modify: `tests/test_agent_file_transfer.py`

**Step 1: Rewrite `tests/test_remote.py` to use framed inputs**

Keep the test names, swap the `screen` fixtures. Example replacements:

```python
# tests/test_remote.py  (relevant portions)
from protocol import encode
from remote import (
    parse_remote_progress, parse_remote_files,
    build_remote_command, parse_turn_state, parse_context,
    parse_question,
)


def test_parse_turn_state_thinking():
    screen = "some shell output\n" + encode("turn", {"state": "thinking"}) + "\n"
    assert parse_turn_state(screen) == "thinking"


def test_parse_turn_state_last_wins():
    screen = "\n".join([
        encode("turn", {"state": "thinking"}),
        encode("turn", {"state": "waiting"}),
    ])
    assert parse_turn_state(screen) == "waiting"


def test_parse_turn_state_none():
    assert parse_turn_state("just shell output\n") is None


def test_parse_context_json():
    screen = encode("context", {"result": "42"})
    assert parse_context(screen) == {"result": "42"}


def test_parse_context_last_wins():
    screen = "\n".join([
        encode("context", {"result": "old"}),
        encode("context", {"result": "new"}),
    ])
    assert parse_context(screen) == {"result": "new"}


def test_parse_context_none():
    assert parse_context("no frame here") is None


def test_parse_remote_files():
    screen = "\n".join([
        encode("file", {"name": "a.txt"}),
        encode("file", {"name": "b.png"}),
    ])
    assert parse_remote_files(screen) == ["a.txt", "b.png"]


def test_parse_remote_progress():
    screen = "\n".join([
        encode("progress", {"text": "step 1"}),
        encode("progress", {"text": "step 2"}),
    ])
    assert parse_remote_progress(screen) == ["step 1", "step 2"]


def test_stray_sentinel_does_not_parse():
    # LLM output containing the literal string <<<CLIVE:turn:done>>> must be ignored.
    screen = "<<<CLIVE:turn:done>>>\n"
    assert parse_turn_state(screen) is None
```

Delete the tests that referenced `parse_remote_result` (legacy `DONE:` JSON path). That function is going away in Task 4 but removing its tests first keeps the commit clean.

Update `tests/test_agent_conversation.py` the same way for `parse_question`:

```python
# tests/test_agent_conversation.py
from protocol import encode
from remote import parse_question


def test_parse_question():
    screen = encode("question", {"text": "which format?"})
    assert parse_question(screen) == "which format?"


def test_parse_question_none_when_no_question():
    assert parse_question("nothing here") is None


def test_parse_question_last_wins():
    screen = "\n".join([
        encode("question", {"text": "old"}),
        encode("question", {"text": "new"}),
    ])
    assert parse_question(screen) == "new"


def test_parse_question_empty_question():
    screen = encode("question", {"text": ""})
    assert parse_question(screen) is None
```

And `tests/test_agent_file_transfer.py`:

```python
# tests/test_agent_file_transfer.py (relevant test)
from protocol import encode
from remote import parse_remote_files


def test_parse_remote_files():
    screen = encode("file", {"name": "out.csv"})
    assert parse_remote_files(screen) == ["out.csv"]
```

**Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_remote.py tests/test_agent_conversation.py tests/test_agent_file_transfer.py -v`
Expected: FAIL — the old parsers look for `line.startswith("TURN:")`, which never matches a framed input. Also an ImportError if tests reference deleted `parse_remote_result`.

**Step 3: Rewrite `remote.py` parsers**

Replace lines 27–105 of `remote.py` with:

```python
# remote.py  (lines 27-105 replacement)

from protocol import decode_all, latest


def parse_turn_state(screen: str) -> str | None:
    """Parse the latest turn state from framed screen content."""
    frames = decode_all(screen)
    frame = latest(frames, "turn")
    if frame is None:
        return None
    state = frame.payload.get("state")
    return state.lower() if isinstance(state, str) else None


def parse_question(screen: str) -> str | None:
    """Parse the latest question from framed screen content.

    Returns None for missing or empty question text.
    """
    frames = decode_all(screen)
    frame = latest(frames, "question")
    if frame is None:
        return None
    text = frame.payload.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    return text


def parse_context(screen: str) -> dict | None:
    """Parse the latest context payload from framed screen content."""
    frames = decode_all(screen)
    frame = latest(frames, "context")
    return frame.payload if frame is not None else None


def parse_remote_files(screen: str) -> list[str]:
    """Parse all file declarations in order of appearance."""
    frames = decode_all(screen)
    out = []
    for f in frames:
        if f.kind == "file":
            name = f.payload.get("name")
            if isinstance(name, str):
                out.append(name)
    return out


def parse_remote_progress(screen: str) -> list[str]:
    """Parse all progress declarations in order of appearance."""
    frames = decode_all(screen)
    out = []
    for f in frames:
        if f.kind == "progress":
            text = f.payload.get("text")
            if isinstance(text, str):
                out.append(text)
    return out
```

Keep `scp_file`, `scp_files_from_result`, `check_remote_clive`, and `build_remote_command` unchanged — they're not protocol parsers.

**Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_remote.py tests/test_agent_conversation.py tests/test_agent_file_transfer.py -v`
Expected: PASS for all framed tests. If `tests/test_remote.py` still imports `parse_remote_result` at module top, that import will break — remove it.

**Step 5: Commit**

```bash
git add remote.py tests/test_remote.py tests/test_agent_conversation.py tests/test_agent_file_transfer.py
git commit -m "feat(remote): parse framed protocol frames instead of line prefixes"
```

---

### Task 4: Delete the legacy `DONE:` JSON path

**Files:**
- Modify: `remote.py` (delete `parse_remote_result`, lines 27-37)
- Modify: `clive.py:161` (the `from remote import build_remote_command, check_remote_clive` block — audit the surrounding code for `DONE:` emission and parse_remote_result usage and replace)
- Grep first: `grep -rn parse_remote_result .`

**Step 1: Find all call sites**

Run: `grep -rn 'parse_remote_result\|DONE:' --include='*.py' --include='*.sh'`

Expected: matches in `remote.py` (the def), possibly one call site inside `clive.py` for `--quiet --json` mode, and fixture references in old tests. If any live call site remains, it must be rewritten to parse a framed `context` instead.

**Step 2: Write the failing test**

```python
# tests/test_remote.py  (new test, append)
def test_parse_remote_result_no_longer_exported():
    import remote
    assert not hasattr(remote, "parse_remote_result")
```

Run: `pytest tests/test_remote.py::test_parse_remote_result_no_longer_exported -v`
Expected: FAIL (function still exists).

**Step 3: Delete `parse_remote_result` from `remote.py`**

Remove the function body, docstring, and any imports it used (`json` is still used by `scp_files_from_result`? No — `json` is only used by the deleted function. Grep confirms. Remove the top-level `import json` if unused.)

**Step 4: Rewrite `clive.py` `--quiet --json` mode to emit a framed `context`**

If `clive.py` has a code path that prints `DONE: {...}` when `--json` is set (legacy), change it to call `emit_context({...})` followed by `emit_turn("done")`. Grep `clive.py` for `DONE:` to confirm there's nothing left. The `--json` flag's external contract becomes: "emits a framed `context` message, then a framed `turn=done`." Document this in the `--help` text.

**Step 5: Run the full relevant test suite**

Run: `pytest tests/test_remote.py tests/test_agent_conversation.py tests/test_agent_file_transfer.py tests/test_output_conversational.py tests/test_protocol.py -v`
Expected: PASS.

**Step 6: Commit**

```bash
git add remote.py clive.py tests/test_remote.py
git commit -m "refactor(remote): remove legacy DONE: JSON parser, framed protocol is canonical"
```

---

## Phase 2 — Delegate LLM provider (the crown jewel)

### Task 5: Add `delegate` to the LLM provider registry (no client yet)

**Files:**
- Modify: `llm.py:11-42` (the `PROVIDERS` dict)
- Create: `tests/test_llm_providers.py`

**Step 1: Write the failing test**

```python
# tests/test_llm_providers.py
def test_delegate_provider_registered():
    from llm import PROVIDERS
    assert "delegate" in PROVIDERS
    cfg = PROVIDERS["delegate"]
    assert cfg["base_url"] is None         # no HTTP
    assert cfg["api_key_env"] is None      # no key needed — outer pays
    assert cfg["default_model"] == "delegate"


def test_delegate_provider_selectable(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "delegate")
    import importlib, llm
    importlib.reload(llm)
    assert llm.PROVIDER_NAME == "delegate"
```

Run: `pytest tests/test_llm_providers.py -v`
Expected: FAIL — `delegate` not in PROVIDERS.

**Step 2: Add the provider entry**

In `llm.py`, extend `PROVIDERS`:

```python
    "delegate": {
        "base_url": None,
        "api_key_env": None,
        "default_model": "delegate",
    },
```

**Step 3: Run tests**

Run: `pytest tests/test_llm_providers.py -v`
Expected: PASS.

**Step 4: Commit**

```bash
git add llm.py tests/test_llm_providers.py
git commit -m "feat(llm): register delegate provider placeholder"
```

---

### Task 6: Implement `DelegateClient` — the remote-side inference stub

**Files:**
- Create: `delegate_client.py`
- Create: `tests/test_delegate_client.py`
- Modify: `llm.py` (`get_client()` to return DelegateClient when provider is `delegate`)

**Why separate file:** `llm.py` already has two client branches (openai, anthropic). Adding a third with a completely different transport (stdio instead of HTTP) would bloat `llm.py` and make it harder to test in isolation. Keep delegate concerns in one file.

**Step 1: Write the failing test**

The DelegateClient has to be deterministic and injectable — don't couple it to real stdin/stdout in tests. Constructor accepts file-like read/write objects.

```python
# tests/test_delegate_client.py
import io
import threading

from protocol import encode, decode_all
from delegate_client import DelegateClient


def test_chat_completion_round_trip():
    """DelegateClient writes an llm_request frame and reads an llm_response."""
    out_buf = io.StringIO()
    in_buf = io.StringIO()

    # Pre-seed the response the caller will "send back" before the client reads.
    # (Real usage: outer types this via send_keys; in test we just write to the
    #  buffer so the reader finds it on next poll.)
    def feed_response(rid: str):
        in_buf.write(encode("llm_response", {
            "id": rid,
            "content": "42",
            "prompt_tokens": 10,
            "completion_tokens": 2,
        }) + "\n")
        in_buf.seek(0)  # rewind for the reader

    client = DelegateClient(stdout=out_buf, stdin=in_buf, poll_interval=0.01)

    # Client generates an ID before sending; we have to intercept it.
    # The cleanest way: monkeypatch _new_id to return a known value.
    client._new_id = lambda: "test-001"
    feed_response("test-001")

    resp = client.chat_completions_create(
        model="delegate",
        messages=[{"role": "user", "content": "What is 6*7?"}],
        max_tokens=16,
    )

    # Outgoing frame should be an llm_request with id=test-001
    frames = decode_all(out_buf.getvalue())
    req = [f for f in frames if f.kind == "llm_request"]
    assert len(req) == 1
    assert req[0].payload["id"] == "test-001"
    assert req[0].payload["messages"] == [{"role": "user", "content": "What is 6*7?"}]
    assert req[0].payload["max_tokens"] == 16

    # Response shape mirrors openai.ChatCompletion enough for llm.chat() to consume
    assert resp.choices[0].message.content == "42"
    assert resp.usage.prompt_tokens == 10
    assert resp.usage.completion_tokens == 2


def test_error_frame_raises():
    import pytest
    out_buf = io.StringIO()
    in_buf = io.StringIO()
    in_buf.write(encode("llm_error", {"id": "test-002", "error": "outer LLM unreachable"}) + "\n")
    in_buf.seek(0)

    client = DelegateClient(stdout=out_buf, stdin=in_buf, poll_interval=0.01)
    client._new_id = lambda: "test-002"

    with pytest.raises(RuntimeError, match="outer LLM unreachable"):
        client.chat_completions_create(
            model="delegate",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=16,
        )


def test_ignores_mismatched_response_id():
    """Stale response from a previous request must not be consumed."""
    out_buf = io.StringIO()
    in_buf = io.StringIO()

    # Seed a stale response with the WRONG id, then the real one.
    in_buf.write(encode("llm_response", {
        "id": "stale-000", "content": "old", "prompt_tokens": 0, "completion_tokens": 0,
    }) + "\n")
    in_buf.write(encode("llm_response", {
        "id": "test-003", "content": "new", "prompt_tokens": 0, "completion_tokens": 0,
    }) + "\n")
    in_buf.seek(0)

    client = DelegateClient(stdout=out_buf, stdin=in_buf, poll_interval=0.01)
    client._new_id = lambda: "test-003"
    resp = client.chat_completions_create(model="delegate", messages=[], max_tokens=1)
    assert resp.choices[0].message.content == "new"
```

**Step 2: Run tests to confirm failure**

Run: `pytest tests/test_delegate_client.py -v`
Expected: FAIL (`delegate_client` module missing).

**Step 3: Implement `delegate_client.py`**

```python
# delegate_client.py
"""Delegate LLM client — routes inference requests over stdio to the outer clive.

When a remote clive is configured with LLM_PROVIDER=delegate, every chat
completion is serialized as an llm_request frame on stdout and blocks
reading stdin until a matching llm_response (or llm_error) frame arrives.
The outer clive is responsible for detecting llm_request frames in the
pane, answering them with its own (local) LLM, and typing the response
frame into the pane via tmux send-keys.

This eliminates the "LMStudio is on the caller's localhost but the
remote clive tries to hit ITS OWN localhost" failure mode that happens
with the legacy SendEnv-only approach, without any network tunneling.
"""
from __future__ import annotations

import sys
import time
import uuid
from dataclasses import dataclass
from typing import IO

from protocol import decode_all, encode


@dataclass
class _Message:
    content: str


@dataclass
class _Choice:
    message: _Message


@dataclass
class _Usage:
    prompt_tokens: int
    completion_tokens: int


@dataclass
class _ChatCompletion:
    """Minimal duck-type of openai.types.chat.ChatCompletion — only the
    attributes llm.chat() reads are populated.
    """
    choices: list[_Choice]
    usage: _Usage


class DelegateClient:
    """Stdio-based LLM client.

    Parameters
    ----------
    stdout : file-like
        Where to write outgoing llm_request frames. Defaults to sys.stdout.
    stdin : file-like
        Where to read incoming llm_response / llm_error frames. Defaults
        to sys.stdin.
    poll_interval : float
        How often to re-read stdin when no matching response has arrived yet.
    timeout : float
        Hard cap on how long to wait for a response before raising.
    """

    def __init__(
        self,
        stdout: IO | None = None,
        stdin: IO | None = None,
        poll_interval: float = 0.2,
        timeout: float = 300.0,
    ):
        self._out = stdout if stdout is not None else sys.stdout
        self._in = stdin if stdin is not None else sys.stdin
        self._poll = poll_interval
        self._timeout = timeout
        # Mirror the openai SDK's .chat.completions.create shape for drop-in use.
        self.chat = _ChatNamespace(self)

    def _new_id(self) -> str:
        return f"req-{uuid.uuid4().hex[:12]}"

    def chat_completions_create(
        self,
        model: str,
        messages: list[dict],
        max_tokens: int = 1024,
        temperature: float | None = None,
        **kwargs,
    ) -> _ChatCompletion:
        rid = self._new_id()
        payload = {
            "id": rid,
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if temperature is not None:
            payload["temperature"] = temperature

        self._out.write(encode("llm_request", payload) + "\n")
        self._out.flush()

        deadline = time.time() + self._timeout
        buf = ""
        while time.time() < deadline:
            chunk = self._read_available()
            if chunk:
                buf += chunk
                frames = decode_all(buf)
                for f in frames:
                    if f.kind == "llm_error" and f.payload.get("id") == rid:
                        raise RuntimeError(f.payload.get("error", "delegate error"))
                    if f.kind == "llm_response" and f.payload.get("id") == rid:
                        return _ChatCompletion(
                            choices=[_Choice(message=_Message(content=f.payload.get("content", "")))],
                            usage=_Usage(
                                prompt_tokens=int(f.payload.get("prompt_tokens", 0)),
                                completion_tokens=int(f.payload.get("completion_tokens", 0)),
                            ),
                        )
            time.sleep(self._poll)

        raise TimeoutError(f"delegate LLM response timed out after {self._timeout}s (id={rid})")

    def _read_available(self) -> str:
        """Read whatever is currently available on the input stream.

        For StringIO in tests, this is everything. For real stdin, we fall
        back to readline() in a loop — the outer's send_keys produces whole
        lines, so readline() won't block past the next frame boundary.
        """
        if hasattr(self._in, "getvalue"):  # StringIO — read remainder
            remaining = self._in.read()
            return remaining
        # Real stdin: read a single line non-blockingly isn't portable;
        # use readline and trust the outer to flush full lines.
        line = self._in.readline()
        return line


class _ChatNamespace:
    """Provides client.chat.completions.create(...) to mimic the openai SDK."""

    def __init__(self, parent: DelegateClient):
        self.completions = _CompletionsNamespace(parent)


class _CompletionsNamespace:
    def __init__(self, parent: DelegateClient):
        self._parent = parent

    def create(self, **kwargs) -> _ChatCompletion:
        return self._parent.chat_completions_create(**kwargs)
```

**Step 4: Wire `get_client()` in `llm.py`**

Modify `llm.py:56-69`:

```python
def get_client():
    global _client_cache
    if _client_cache is not None:
        return _client_cache

    if PROVIDER_NAME == "delegate":
        from delegate_client import DelegateClient
        _client_cache = DelegateClient()
        return _client_cache

    api_key_env = _provider["api_key_env"]
    api_key = os.environ.get(api_key_env) if api_key_env else "not-needed"

    if PROVIDER_NAME == "anthropic":
        _client_cache = anthropic.Anthropic(api_key=api_key)
    else:
        _client_cache = openai.OpenAI(base_url=_provider["base_url"], api_key=api_key)

    return _client_cache
```

Also teach `chat()` in `llm.py` to handle DelegateClient:

```python
# llm.py, inside chat(), before the anthropic/openai branches:
    from delegate_client import DelegateClient
    if isinstance(client, DelegateClient):
        resp = client.chat.completions.create(
            model=model or MODEL,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        content = resp.choices[0].message.content or ""
        return content, resp.usage.prompt_tokens, resp.usage.completion_tokens
```

For `chat_stream()`, delegate to `chat()` non-streamingly — no streaming in v1:

```python
    from delegate_client import DelegateClient
    if isinstance(client, DelegateClient):
        content, pt, ct = chat(client, messages, max_tokens, model)
        if on_token:
            on_token(content)
        return content, pt, ct
```

**Step 5: Run all the relevant tests**

Run: `pytest tests/test_delegate_client.py tests/test_llm_providers.py -v`
Expected: PASS.

**Step 6: Commit**

```bash
git add delegate_client.py llm.py tests/test_delegate_client.py
git commit -m "feat(llm): add DelegateClient — route inference over stdio frames"
```

---

### Task 7: Outer-side — handle `llm_request` frames in the agent pane turn loop

**Files:**
- Modify: the executor code that handles agent panes. Locate it first:
  - Run: `grep -rn 'parse_turn_state' --include='*.py'`
  - The call site is the "inner agent turn loop" — this is where the outer reads the inner pane's screen and decides whether to wait, respond to a question, or consume the final `context`.
- Create: `tests/test_executor_delegate.py`

**Why this task is subtle:** The outer's main LLM reasoning loop must NOT consume a turn when answering an `llm_request`. Delegation is a side-channel round trip, not part of the inner's plan-execute flow. The outer sees `llm_request`, calls its own `llm.chat()`, types back `llm_response` via `send_keys`, and continues polling — nothing else changes in its state machine.

**Step 1: Write the failing test**

This is an integration test that does NOT require tmux or SSH. We mock the pane with a simple object that records what was sent.

```python
# tests/test_executor_delegate.py
from unittest.mock import MagicMock

from protocol import encode, decode_all


def test_outer_answers_llm_request_via_send_keys(monkeypatch):
    """When the inner pane shows an llm_request frame, the outer should:
       1. call llm.chat() with the forwarded messages,
       2. type back an llm_response frame via pane.send_keys,
       3. not consume an outer-loop turn."""
    from executor import handle_agent_pane_frame  # new function we're adding

    fake_pane = MagicMock()
    request_frame = encode("llm_request", {
        "id": "req-abc",
        "model": "delegate",
        "messages": [{"role": "user", "content": "hello"}],
        "max_tokens": 16,
    })

    # Stub llm.chat to return a known response.
    calls = {}
    def fake_chat(client, messages, max_tokens=1024, model=None, temperature=None):
        calls["messages"] = messages
        calls["max_tokens"] = max_tokens
        return "hi back", 7, 2

    monkeypatch.setattr("llm.chat", fake_chat)
    monkeypatch.setattr("llm.get_client", lambda: object())

    handled = handle_agent_pane_frame(fake_pane, request_frame)
    assert handled is True

    # Outer must have called the local LLM with the forwarded messages.
    assert calls["messages"] == [{"role": "user", "content": "hello"}]
    assert calls["max_tokens"] == 16

    # Outer must have typed a matching llm_response frame into the pane.
    assert fake_pane.send_keys.called
    typed = fake_pane.send_keys.call_args[0][0]
    frames = decode_all(typed)
    assert len(frames) == 1
    assert frames[0].kind == "llm_response"
    assert frames[0].payload["id"] == "req-abc"
    assert frames[0].payload["content"] == "hi back"
    assert frames[0].payload["prompt_tokens"] == 7
    assert frames[0].payload["completion_tokens"] == 2


def test_outer_sends_llm_error_on_chat_failure(monkeypatch):
    from executor import handle_agent_pane_frame

    fake_pane = MagicMock()
    request_frame = encode("llm_request", {
        "id": "req-err", "model": "delegate",
        "messages": [], "max_tokens": 8,
    })

    def failing_chat(*args, **kwargs):
        raise RuntimeError("LMStudio unreachable")

    monkeypatch.setattr("llm.chat", failing_chat)
    monkeypatch.setattr("llm.get_client", lambda: object())

    handle_agent_pane_frame(fake_pane, request_frame)

    typed = fake_pane.send_keys.call_args[0][0]
    frames = decode_all(typed)
    assert frames[0].kind == "llm_error"
    assert frames[0].payload["id"] == "req-err"
    assert "LMStudio unreachable" in frames[0].payload["error"]


def test_non_llm_request_frame_returns_false():
    from executor import handle_agent_pane_frame
    from unittest.mock import MagicMock

    pane = MagicMock()
    turn_frame = encode("turn", {"state": "thinking"})
    assert handle_agent_pane_frame(pane, turn_frame) is False
    pane.send_keys.assert_not_called()
```

**Step 2: Run tests to confirm failure**

Run: `pytest tests/test_executor_delegate.py -v`
Expected: FAIL (`handle_agent_pane_frame` not defined).

**Step 3: Add `handle_agent_pane_frame` to `executor.py`**

Add at an appropriate location near the other agent-pane helpers:

```python
# executor.py (new helper, import protocol at the top)

def handle_agent_pane_frame(pane, screen_blob: str) -> bool:
    """If screen_blob contains an unanswered llm_request frame, answer it
    by calling the local LLM and typing back an llm_response frame.

    Returns True iff a delegate request was handled (caller should NOT
    advance its turn state — delegation is a side-channel round trip).
    """
    from protocol import decode_all, encode, latest
    import llm

    frames = decode_all(screen_blob)
    req = latest(frames, "llm_request")
    if req is None:
        return False

    # Skip if we've already answered this id (outer may re-read the same screen)
    resp = latest(frames, "llm_response")
    if resp is not None and resp.payload.get("id") == req.payload.get("id"):
        return False

    rid = req.payload.get("id", "unknown")
    messages = req.payload.get("messages", [])
    max_tokens = int(req.payload.get("max_tokens", 1024))
    model = req.payload.get("model")

    try:
        client = llm.get_client()
        content, pt, ct = llm.chat(
            client,
            messages,
            max_tokens=max_tokens,
            model=model if model and model != "delegate" else None,
        )
        out = encode("llm_response", {
            "id": rid,
            "content": content,
            "prompt_tokens": pt,
            "completion_tokens": ct,
        })
    except Exception as e:
        out = encode("llm_error", {"id": rid, "error": str(e)})

    pane.send_keys(out, enter=True)
    return True
```

**Step 4: Wire it into the agent-pane read loop**

Find the loop that calls `parse_turn_state(screen)`. Immediately BEFORE acting on turn state, call `handle_agent_pane_frame(pane, screen)`. If it returns True, `continue` the loop without advancing turn state. Rough sketch (adjust to match the real loop):

```python
    screen = pane.capture_pane()
    if handle_agent_pane_frame(pane, screen):
        time.sleep(0.5)
        continue
    turn = parse_turn_state(screen)
    # ... existing handling
```

**Step 5: Run tests**

Run: `pytest tests/test_executor_delegate.py -v`
Expected: PASS for all three tests.

**Step 6: Commit**

```bash
git add executor.py tests/test_executor_delegate.py
git commit -m "feat(executor): answer inner llm_request frames via local LLM"
```

---

### Task 8: Auto-enable `LLM_PROVIDER=delegate` for local outer providers

**Files:**
- Modify: `agents.py:20-27, 123-157` (`_FORWARD_ENVS` and `build_agent_ssh_cmd`)
- Modify: `tests/test_agents.py` (or create if thin)

**Decision:** When the outer's `LLM_PROVIDER` is a local-only provider (`lmstudio`, `ollama`), the remote cannot possibly reach it without tunneling, so we force `delegate`. For cloud providers the default stays — `LLM_PROVIDER` and `*_API_KEY` get SendEnv'd and the remote talks to the cloud endpoint directly. This keeps the fast path fast and only introduces the extra stdio round-trip when it's the only correct behavior.

**Step 1: Write the failing test**

```python
# tests/test_agents.py (append)
import os

from agents import build_agent_ssh_cmd


def test_local_provider_forces_delegate(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "lmstudio")
    cmd = build_agent_ssh_cmd("prod.example.com", config={})
    # The remote must be told to use delegate, not lmstudio
    assert "SendEnv=LLM_PROVIDER" in cmd
    # ... and an override env must be set on the ssh invocation itself
    assert "LLM_PROVIDER=delegate" in cmd
    # sanity: the remote clive command itself has --conversational
    assert "--conversational" in cmd


def test_cloud_provider_forwards_as_is(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-fake")
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    cmd = build_agent_ssh_cmd("prod.example.com", config={})
    assert "SendEnv=LLM_PROVIDER" in cmd
    assert "SendEnv=OPENROUTER_API_KEY" in cmd
    # No override, the remote inherits openrouter
    assert "LLM_PROVIDER=delegate" not in cmd


def test_ollama_also_forces_delegate(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    cmd = build_agent_ssh_cmd("prod.example.com", config={})
    assert "LLM_PROVIDER=delegate" in cmd
```

**Step 2: Run tests to confirm failure**

Run: `pytest tests/test_agents.py -v -k delegate`
Expected: FAIL — current `build_agent_ssh_cmd` has no notion of provider overrides.

**Step 3: Update `build_agent_ssh_cmd`**

Replace the function in `agents.py`:

```python
_LOCAL_PROVIDERS = frozenset({"lmstudio", "ollama"})


def build_agent_ssh_cmd(host: str, config: dict) -> str:
    """Build SSH command for clive-to-clive connection.

    No -t flag (no TTY) → inner clive auto-detects conversational mode.
    Forwards API key env vars via SendEnv (BYOLLM).

    Local LLM providers (lmstudio, ollama) cannot be reached from the
    remote host over the network without tunneling, so we transparently
    override LLM_PROVIDER=delegate on the remote — inference round-trips
    back through the conversational channel via DelegateClient.
    """
    parts = ["ssh"]

    key = config.get("key")
    if key:
        parts.append(f"-i {key}")

    # Forward API key env vars that are actually set locally.
    for env_var in _FORWARD_ENVS:
        if os.environ.get(env_var):
            parts.append(f"-o SendEnv={env_var}")

    # Connection options
    parts.extend(["-o BatchMode=yes", "-o ConnectTimeout=10"])

    # Local providers can't be reached from the remote — force delegate.
    outer_provider = os.environ.get("LLM_PROVIDER", "").lower()
    remote_env_overrides = []
    if outer_provider in _LOCAL_PROVIDERS:
        remote_env_overrides.append("LLM_PROVIDER=delegate")
        remote_env_overrides.append("AGENT_MODEL=delegate")

    # Host
    parts.append(host)

    # Remote command — prefix with env overrides if any
    clive_path = config.get("path", DEFAULT_CLIVE_PATH)
    toolset = config.get("toolset")
    remote_parts = []
    if remote_env_overrides:
        remote_parts.extend(remote_env_overrides)
    remote_parts.extend([clive_path, "--conversational"])
    if toolset:
        remote_parts.extend(["-t", toolset])

    remote_cmd = " ".join(remote_parts)
    parts.append(f"'{remote_cmd}'")

    return " ".join(parts)
```

**Step 4: Run tests**

Run: `pytest tests/test_agents.py -v -k delegate`
Expected: PASS (3 tests).

**Step 5: Commit**

```bash
git add agents.py tests/test_agents.py
git commit -m "feat(agents): force LLM_PROVIDER=delegate on remote when outer is lmstudio/ollama"
```

---

### Task 9: End-to-end integration test — mock LMStudio → outer → inner subprocess → delegate → mock LMStudio

**Files:**
- Create: `tests/test_integration_delegate.py`

**Why:** Unit tests proved each layer in isolation. This task proves they compose. No SSH, no tmux — just a subprocess with stdin/stdout pipes playing the role of the inner clive, and an HTTP mock playing the role of LMStudio.

**Step 1: Write the integration test**

```python
# tests/test_integration_delegate.py
import http.server
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from protocol import encode, decode_all


class _MockLMStudio(http.server.BaseHTTPRequestHandler):
    """Minimal OpenAI-compatible server that always returns '42'."""
    request_log: list[dict] = []

    def log_message(self, *_): pass  # silence

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        req = json.loads(body) if body else {}
        _MockLMStudio.request_log.append(req)
        resp = {
            "id": "chatcmpl-1",
            "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "42"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
        }
        data = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def mock_lmstudio():
    _MockLMStudio.request_log = []
    srv = http.server.HTTPServer(("127.0.0.1", 0), _MockLMStudio)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield port
    srv.shutdown()


def test_delegate_round_trip_calls_outer_lmstudio(mock_lmstudio, monkeypatch, tmp_path):
    """Simulated end-to-end: spawn an inner clive with LLM_PROVIDER=delegate,
    feed it a trivial task, and verify:
      1. The inner writes an llm_request frame to stdout.
      2. We (as the outer) call mock LMStudio ourselves and write back llm_response.
      3. The inner completes and emits a framed context+turn=done.
    """
    import os
    env = os.environ.copy()
    env["LLM_PROVIDER"] = "delegate"
    env["AGENT_MODEL"] = "delegate"
    env["PYTHONUNBUFFERED"] = "1"

    clive_py = Path(__file__).parent.parent / "clive.py"

    # Start inner clive with stdin/stdout pipes.
    proc = subprocess.Popen(
        [sys.executable, str(clive_py), "--conversational", "-t", "minimal", "say hi"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
    )

    # Read lines from stdout, handling frames as the outer would.
    import openai
    outer_client = openai.OpenAI(base_url=f"http://127.0.0.1:{mock_lmstudio}/v1", api_key="not-needed")

    saw_done = False
    deadline = time.time() + 30
    buf = ""

    try:
        while time.time() < deadline and not saw_done:
            line = proc.stdout.readline()
            if not line:
                break
            buf += line
            frames = decode_all(buf)
            for f in frames:
                if f.kind == "llm_request":
                    # Outer answers with mock LMStudio
                    resp = outer_client.chat.completions.create(
                        model="local",
                        messages=f.payload["messages"],
                        max_tokens=f.payload.get("max_tokens", 256),
                    )
                    out_frame = encode("llm_response", {
                        "id": f.payload["id"],
                        "content": resp.choices[0].message.content or "",
                        "prompt_tokens": resp.usage.prompt_tokens,
                        "completion_tokens": resp.usage.completion_tokens,
                    })
                    proc.stdin.write(out_frame + "\n")
                    proc.stdin.flush()
                elif f.kind == "turn" and f.payload.get("state") == "done":
                    saw_done = True
                elif f.kind == "turn" and f.payload.get("state") == "failed":
                    pytest.fail(f"Inner clive failed; stderr:\n{proc.stderr.read()}")
            # Prevent buf from re-triggering handled frames
            if saw_done:
                break
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    assert saw_done, f"inner never reached turn=done.\nstdout buf:\n{buf}\nstderr:\n{proc.stderr.read()}"
    assert len(_MockLMStudio.request_log) >= 1, "outer LMStudio was never called"
```

**Step 2: Run the test**

Run: `pytest tests/test_integration_delegate.py -v -s`
Expected: PASS. If not, use `-s` to watch the stdout/stderr live and diagnose.

**Known risk:** if the real planner makes many LLM calls per task, the outer loop above will need to handle multiple `llm_request` frames. The `while not saw_done` loop already does this (it keeps reading). If the planner asks questions that need stateful responses, the mock LMStudio will need to be smarter. Keep the task text trivially decomposable ("say hi" should be a one-call task).

**Step 3: Commit**

```bash
git add tests/test_integration_delegate.py
git commit -m "test(integration): end-to-end delegate round-trip with mock LMStudio"
```

---

## Phase 3 — Supporting fixes

### Task 10: Add `LLM_BASE_URL` and `GOOGLE_API_KEY` to forwarding and provider lookup

**Files:**
- Modify: `llm.py:11-42` (PROVIDERS + `get_client`)
- Modify: `agents.py:21-27` (`_FORWARD_ENVS`)
- Modify: `tests/test_llm_providers.py`, `tests/test_agents.py`

**Step 1: Write the failing test**

```python
# tests/test_llm_providers.py (append)
def test_llm_base_url_overrides_provider_default(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("LLM_BASE_URL", "http://my-proxy:8080/v1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-x")
    import importlib, llm
    importlib.reload(llm)
    client = llm.get_client()
    assert str(client.base_url).startswith("http://my-proxy:8080")


# tests/test_agents.py (append)
def test_google_api_key_is_forwarded(monkeypatch):
    from agents import build_agent_ssh_cmd
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GOOGLE_API_KEY", "g-fake")
    cmd = build_agent_ssh_cmd("host", config={})
    assert "SendEnv=GOOGLE_API_KEY" in cmd


def test_llm_base_url_is_forwarded(monkeypatch):
    from agents import build_agent_ssh_cmd
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-x")
    monkeypatch.setenv("LLM_BASE_URL", "http://proxy:8080/v1")
    cmd = build_agent_ssh_cmd("host", config={})
    assert "SendEnv=LLM_BASE_URL" in cmd
```

Run: `pytest tests/test_llm_providers.py tests/test_agents.py -v -k "base_url or google"`
Expected: FAIL.

**Step 2: Add `LLM_BASE_URL` honouring in `llm.py`**

Inside `get_client()`, after selecting `_provider`:

```python
    base_url_override = os.environ.get("LLM_BASE_URL")
    base_url = base_url_override or _provider["base_url"]
    ...
    else:
        _client_cache = openai.OpenAI(base_url=base_url, api_key=api_key)
```

(`anthropic` ignores base_url; that's fine — users who want a proxy should use the openai path.)

**Step 3: Extend `_FORWARD_ENVS` in `agents.py`**

```python
_FORWARD_ENVS = [
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "GOOGLE_API_KEY",
    "LLM_PROVIDER",
    "AGENT_MODEL",
    "LLM_BASE_URL",
]
```

**Step 4: Run tests**

Run: `pytest tests/test_llm_providers.py tests/test_agents.py -v`
Expected: PASS.

**Step 5: Commit**

```bash
git add llm.py agents.py tests/test_llm_providers.py tests/test_agents.py
git commit -m "feat(llm,agents): honour LLM_BASE_URL and forward GOOGLE_API_KEY"
```

---

### Task 11: SSH `ControlMaster` connection reuse

**Files:**
- Modify: `agents.py:123-157` (build_agent_ssh_cmd)
- Modify: `clive.py` (ensure `~/.clive/ssh/` exists at startup)
- Modify: `tests/test_agents.py`

**Step 1: Write the failing test**

```python
# tests/test_agents.py (append)
def test_ssh_cmd_enables_controlmaster(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    from agents import build_agent_ssh_cmd
    cmd = build_agent_ssh_cmd("host", config={})
    assert "ControlMaster=auto" in cmd
    assert "ControlPath=" in cmd
    assert "ControlPersist=" in cmd
```

Run: expected FAIL.

**Step 2: Add to `build_agent_ssh_cmd`**

After the existing `BatchMode` / `ConnectTimeout` options, append:

```python
    # Connection pooling — reuse a single SSH channel for rapid agent traffic
    ctl_path = os.path.expanduser("~/.clive/ssh/%C")
    parts.extend([
        "-o ControlMaster=auto",
        f"-o ControlPath={ctl_path}",
        "-o ControlPersist=60s",
    ])
```

**Step 3: Ensure the ssh control dir exists**

In `clive.py`, near the top of `main()` (before any agent pane is spawned), add:

```python
    os.makedirs(os.path.expanduser("~/.clive/ssh"), exist_ok=True, mode=0o700)
```

**Step 4: Run tests**

Run: `pytest tests/test_agents.py -v -k controlmaster`
Expected: PASS.

**Step 5: Commit**

```bash
git add agents.py clive.py tests/test_agents.py
git commit -m "perf(agents): pool SSH connections via ControlMaster for clive@host"
```

---

### Task 12: `clive agents doctor` subcommand

**Files:**
- Create: `agents_doctor.py`
- Modify: `clive.py` (CLI registration — look at how other subcommands are registered, e.g. `--schedule`)
- Create: `tests/test_agents_doctor.py`

**Why:** The single biggest class of production bugs in the remote-clive subsystem is silent misconfig: AcceptEnv doesn't match SendEnv, key path is wrong, clive isn't installed on the remote. `doctor` proactively surfaces all of these in one command.

**Step 1: Define the doctor report schema**

```python
# agents_doctor.py
"""`clive agents doctor` — validate remote clive connectivity."""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field

from agents import _load_registry, _FORWARD_ENVS, build_agent_ssh_cmd


@dataclass
class AgentCheck:
    host: str
    checks: dict = field(default_factory=dict)  # name -> (ok: bool, detail: str)

    def ok(self) -> bool:
        return all(v[0] for v in self.checks.values())


def check_agent(host: str, config: dict) -> AgentCheck:
    result = AgentCheck(host=host)

    # 1. Key file exists (if specified)
    key = config.get("key")
    if key:
        expanded = os.path.expanduser(key)
        exists = os.path.exists(expanded)
        result.checks["key_exists"] = (exists, expanded if exists else f"missing: {expanded}")
    else:
        result.checks["key_exists"] = (True, "using SSH default identity")

    # 2. SSH connectivity (5s timeout, non-interactive)
    actual_host = config.get("host", host)
    ssh_cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5"]
    if key:
        ssh_cmd.extend(["-i", os.path.expanduser(key)])
    ssh_cmd.extend([actual_host, "echo clive-doctor-ok"])
    try:
        r = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=10)
        ok = r.returncode == 0 and "clive-doctor-ok" in r.stdout
        result.checks["ssh_connect"] = (ok, r.stderr.strip() or "ok")
    except (subprocess.TimeoutExpired, OSError) as e:
        result.checks["ssh_connect"] = (False, str(e))
        return result  # no point continuing

    # 3. Remote python3 + clive.py importable
    clive_path = config.get("path", "python3 clive.py")
    import_cmd = ssh_cmd[:-1] + [f"{clive_path.split()[0]} -c 'import clive; print(\"ok\")'"]
    try:
        r = subprocess.run(import_cmd, capture_output=True, text=True, timeout=10)
        result.checks["clive_installed"] = (
            r.returncode == 0 and "ok" in r.stdout,
            r.stderr.strip() or "ok",
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        result.checks["clive_installed"] = (False, str(e))

    # 4. AcceptEnv — list what the remote sshd will accept
    accept_cmd = ssh_cmd[:-1] + ["sshd -T 2>/dev/null | grep -i acceptenv || true"]
    try:
        r = subprocess.run(accept_cmd, capture_output=True, text=True, timeout=10)
        accepted = r.stdout.lower()
        missing = [v for v in _FORWARD_ENVS
                   if os.environ.get(v) and v.lower() not in accepted]
        if missing:
            result.checks["accept_env"] = (
                False,
                f"remote sshd missing AcceptEnv for: {', '.join(missing)}",
            )
        else:
            result.checks["accept_env"] = (True, "all set envs accepted")
    except Exception as e:
        # Not fatal — user may not have sudo on remote to read sshd -T
        result.checks["accept_env"] = (True, f"could not verify ({e})")

    return result


def run_doctor(registry_path: str | None = None) -> list[AgentCheck]:
    registry = _load_registry(registry_path)
    results = []
    for host, config in registry.items():
        results.append(check_agent(host, config))
    return results


def format_report(results: list[AgentCheck]) -> str:
    lines = []
    for r in results:
        status = "✓" if r.ok() else "✗"
        lines.append(f"{status} {r.host}")
        for name, (ok, detail) in r.checks.items():
            icon = "  ✓" if ok else "  ✗"
            lines.append(f"{icon} {name}: {detail}")
    return "\n".join(lines)
```

**Step 2: Write the test**

```python
# tests/test_agents_doctor.py
from unittest.mock import patch, MagicMock

from agents_doctor import check_agent, AgentCheck


def test_check_agent_with_missing_key(tmp_path):
    config = {"host": "fake.example.com", "key": "/nonexistent/key"}
    result = check_agent("fake", config)
    assert result.checks["key_exists"][0] is False
    assert "missing" in result.checks["key_exists"][1]


def test_check_agent_ssh_timeout(monkeypatch):
    import subprocess
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=10)
    monkeypatch.setattr(subprocess, "run", fake_run)
    result = check_agent("fake", {})
    assert result.checks["ssh_connect"][0] is False


def test_format_report_has_per_check_lines():
    from agents_doctor import format_report
    r = AgentCheck(host="prod")
    r.checks["key_exists"] = (True, "ok")
    r.checks["ssh_connect"] = (False, "connection refused")
    out = format_report([r])
    assert "prod" in out
    assert "key_exists" in out
    assert "ssh_connect" in out
    assert "connection refused" in out
```

Run: `pytest tests/test_agents_doctor.py -v`
Expected: PASS after `agents_doctor.py` is created per Step 1.

**Step 3: CLI wiring**

In `clive.py` add an argparse flag `--agents-doctor` (or a proper `agents` subparser if that pattern exists — grep for `add_parser`) that calls `run_doctor()` and prints `format_report(...)`.

**Step 4: Manual smoke**

Run: `python3 clive.py --agents-doctor` (assuming an `agents.yaml` is present)
Expected: a per-host report with pass/fail per check.

**Step 5: Commit**

```bash
git add agents_doctor.py clive.py tests/test_agents_doctor.py
git commit -m "feat(agents): add 'clive agents doctor' to catch silent remote misconfig"
```

---

## Phase 4 — Hardening

### Task 13: Conversational keepalive (inner side)

**Files:**
- Modify: `clive.py:260-286` (the conversational stdin loop)
- Create: `tests/test_conversational_keepalive.py`

**Why:** Inner clive blocks on `sys.stdin.readline()` after emitting `turn=waiting`. If the outer crashes, the inner hangs forever holding a tmux session and potentially a DelegateClient waiting on a response that will never come. Emitting `alive` frames every 15s while waiting lets the outer (or any supervisor) detect dead sessions.

**Step 1: Write the failing test**

```python
# tests/test_conversational_keepalive.py
import subprocess
import sys
import time
from pathlib import Path

from protocol import decode_all


def test_conversational_loop_emits_alive_frames(tmp_path):
    """Run clive in conversational keep-alive mode with no task, verify
    alive frames appear on stdout within 20 seconds."""
    clive_py = Path(__file__).parent.parent / "clive.py"
    import os
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    proc = subprocess.Popen(
        [sys.executable, str(clive_py), "--conversational", "--name", "keepalive-test"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
    )
    try:
        buf = ""
        deadline = time.time() + 20
        saw_alive = False
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                time.sleep(0.2)
                continue
            buf += line
            frames = decode_all(buf)
            if any(f.kind == "alive" for f in frames):
                saw_alive = True
                break
        assert saw_alive, f"no alive frame within 20s. stdout:\n{buf}"
    finally:
        proc.stdin.write("exit\n")
        proc.stdin.flush()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
```

Run: `pytest tests/test_conversational_keepalive.py -v -s`
Expected: FAIL (no alive frames emitted yet).

**Step 2: Emit alive frames during stdin wait**

Change the keep-alive loop at `clive.py:260-286` to use a background thread that emits `alive` every 15s:

```python
        import threading
        from output import emit_alive

        stop_alive = threading.Event()

        def _alive_ticker():
            while not stop_alive.is_set():
                emit_alive()
                stop_alive.wait(15.0)

        alive_thread = threading.Thread(target=_alive_ticker, daemon=True)
        alive_thread.start()

        try:
            while True:
                try:
                    line = sys.stdin.readline()
                except EOFError:
                    break
                if not line:
                    break
                task = line.strip()
                if not task:
                    continue
                if task.lower() in ("exit", "quit", "/stop"):
                    break
                emit_turn("thinking")
                try:
                    summary = run(
                        task,
                        toolset_spec=args.toolset,
                        output_format="default",
                        max_tokens=args.max_tokens,
                    )
                    emit_context({"result": summary})
                    emit_turn("done")
                except Exception as e:
                    emit_context({"error": str(e)})
                    emit_turn("failed")
        finally:
            stop_alive.set()

        raise SystemExit(0)
```

Note: `emit_alive` is thread-safe because `print(..., flush=True)` on stdout holds the CPython GIL around the write syscall, and each emit is a single `write()` of a complete line. Don't bother adding an explicit lock.

**Step 3: Run the test**

Run: `pytest tests/test_conversational_keepalive.py -v -s`
Expected: PASS within ~15 seconds.

**Step 4: Commit**

```bash
git add clive.py tests/test_conversational_keepalive.py
git commit -m "feat(conversational): emit alive frames every 15s in keepalive loop"
```

---

## Phase 5 — Manual end-to-end smoke against real LMStudio

### Task 14: Real LMStudio smoke test & documentation

**Files:**
- Create: `docs/byollm-delegate.md` (user-facing)

**Prerequisites:**
- LMStudio (or Ollama) running on the local machine with a model loaded
- At least one reachable remote host with clive installed
- `~/.clive/agents.yaml` configured for that host

**Step 1: Wire up & confirm the provider detection**

```bash
export LLM_PROVIDER=lmstudio
export LLM_BASE_URL=http://localhost:1234/v1
python3 clive.py --agents-doctor
```

Expected: doctor passes all checks for each configured host, no complaints about LMStudio/Ollama specifically (since the outer is what matters).

**Step 2: Invoke a remote task**

```bash
python3 clive.py "clive@prod list files in /tmp and return the count"
```

What should happen under the hood:
1. Outer starts, parses `clive@prod`.
2. Outer builds the SSH command with `LLM_PROVIDER=delegate` as a remote env override (because outer is on `lmstudio`).
3. SSH connects, inner clive starts in conversational mode with `LLM_PROVIDER=delegate`.
4. Inner tries to plan → needs LLM → DelegateClient serializes an `llm_request` frame on its stdout (which is the SSH pipe).
5. Outer sees the `llm_request` frame in the pane, calls LMStudio on the outer's localhost, types back an `llm_response` frame via `send_keys`.
6. Inner's DelegateClient reads the response, returns the `_ChatCompletion` to `llm.chat()`, planning continues.
7. Steps 4–6 repeat for every LLM call inside the inner's plan.
8. Inner emits `context={"result": ...}` + `turn=done`.
9. Outer consumes the context, prints the result.

**Step 3: Observe LMStudio's request log**

Watch LMStudio's UI or server log. Expect N requests corresponding to N LLM calls the inner needed. Confirm the prompts contain the inner's planner/executor templates — the outer LMStudio is doing the brain work for the remote.

**Step 4: Write `docs/byollm-delegate.md`**

```markdown
# Bring-Your-Own-LLM for remote clives

When you address a remote clive via `clive@host`, the remote needs an
LLM to plan and execute. Two cases:

## Cloud providers (Anthropic, OpenAI, OpenRouter, Gemini)

Your local env vars (API keys + `LLM_PROVIDER`) are forwarded via SSH
`SendEnv`. The remote uses them to call the cloud endpoint directly.
Your laptop doesn't proxy anything — the remote calls the cloud by
itself, using your keys.

Requirements:
- `AcceptEnv ANTHROPIC_API_KEY OPENAI_API_KEY OPENROUTER_API_KEY GOOGLE_API_KEY LLM_PROVIDER AGENT_MODEL LLM_BASE_URL`
  in the remote's `/etc/ssh/sshd_config` (or a drop-in under
  `/etc/ssh/sshd_config.d/`).
- Remote must have internet access to the LLM endpoint.

## Local providers (LMStudio, Ollama)

Local LLMs live on *your* laptop's localhost. The remote has no way
to reach them without tunneling. Clive handles this automatically
by switching the remote to a `delegate` LLM provider.

Under delegation, every inference the remote wants to run is serialized
as a framed `llm_request` message on stdout (i.e. back over the SSH
channel), picked up by your local clive, answered by your local
LMStudio/Ollama, and typed back into the remote pane as an
`llm_response` frame. The remote never touches the network for
inference — your laptop does all the brain work.

What you do:
1. Start LMStudio (port 1234) or Ollama (port 11434) as normal.
2. `export LLM_PROVIDER=lmstudio`  (or `ollama`).
3. `python3 clive.py "clive@prod do something"`.

That's it — no `ssh -R`, no tunnel config, no AcceptEnv worries for
API keys (there are none).

Caveats:
- Every remote LLM call makes one round-trip over your SSH channel.
  Latency adds up for tasks with many turns. Acceptable for local
  dev and testing; not recommended for high-throughput batch jobs
  where a cloud provider with a regional endpoint will be faster.
- Streaming is not yet supported in delegate mode (v1). Responses
  arrive whole.
- If you disconnect, the remote's DelegateClient will time out after
  5 minutes and fail the subtask.

## Troubleshooting

- `clive agents doctor` — run this first when something's wrong.
- Look for `<<<CLIVE:llm_request:...>>>` in the remote pane's scrollback:
  present means delegation is active and waiting; absent means the
  remote is trying to call its own LLM provider.
- If the remote hangs: check that your local LMStudio/Ollama is up
  and responding. The outer will propagate HTTP failures as
  `llm_error` frames.
```

**Step 5: Commit**

```bash
git add docs/byollm-delegate.md
git commit -m "docs: document BYOLLM delegation for remote clives"
```

---

## Verification checklist (run before declaring done)

- [ ] `pytest tests/test_protocol.py` — 7 tests pass
- [ ] `pytest tests/test_output_conversational.py` — 6 tests pass
- [ ] `pytest tests/test_remote.py tests/test_agent_conversation.py tests/test_agent_file_transfer.py` — all framed tests pass
- [ ] `pytest tests/test_llm_providers.py` — delegate provider registered + LLM_BASE_URL honoured
- [ ] `pytest tests/test_delegate_client.py` — 3 tests pass
- [ ] `pytest tests/test_executor_delegate.py` — 3 tests pass
- [ ] `pytest tests/test_agents.py` — delegate-forcing + SendEnv + ControlMaster tests pass
- [ ] `pytest tests/test_integration_delegate.py` — end-to-end mock LMStudio round-trip passes
- [ ] `pytest tests/test_agents_doctor.py` — 3 tests pass
- [ ] `pytest tests/test_conversational_keepalive.py` — alive frame seen within 20s
- [ ] `grep -rn "TURN:" --include="*.py"` — only appears in legacy docs/blogs, not in live code
- [ ] `grep -rn "DONE:" --include="*.py"` — no live code references
- [ ] `grep -rn "parse_remote_result" --include="*.py"` — zero matches
- [ ] Manual: `LLM_PROVIDER=lmstudio python3 clive.py "clive@<your-host> list files in /tmp"` succeeds and LMStudio shows incoming requests
- [ ] Manual: `python3 clive.py --agents-doctor` reports clean for a well-configured host, and clearly identifies the problem for a mis-AcceptEnv'd host

---

## Task dependency graph

```
Task 1 (protocol.py)
  ├→ Task 2 (output emitters)
  │     └→ Task 3 (remote.py parsers)
  │           └→ Task 4 (delete DONE)
  │                 └→ Task 7 (outer handles llm_request)
  │                       └→ Task 9 (integration test)
  └→ Task 6 (DelegateClient)   ← depends on Task 1 directly
        └→ Task 7
        └→ Task 8 (force delegate in ssh cmd)  ← depends on Task 6
              └→ Task 9

Task 5 (register delegate in PROVIDERS)  ← prerequisite for Task 6

Tasks 10, 11, 12, 13 are independent — can run in parallel after Task 1.

Task 14 (manual smoke) requires Tasks 1–13.
```

Tasks 10–12 are independent of the delegation crown jewel and can be worked in parallel if using subagent-driven development.

---

## Notes for the executing engineer

- **Do not introduce a compatibility shim** for the old `TURN:`/`DONE:` line-prefix protocol. It's internal; cut over hard. Tests in Task 3 will catch any stragglers.
- **Do not add streaming to DelegateClient in v1.** The non-streaming fallback (Task 6, chat_stream branch) is intentional. Streaming is a follow-up.
- **Do not amend commits.** Every task ends in a fresh commit.
- **If a test fails for reasons unrelated to your current task, STOP and ask.** Do not "fix" unrelated code.
- **The outer LLM is whatever the outer's `LLM_PROVIDER` says.** Delegation doesn't care — it calls `llm.chat()` with the outer's client. The remote is always `delegate` when triggered; the outer is the variable.
- **`send_keys(text, enter=True)` is the libtmux method.** Pass the full framed string as `text`; tmux will add the trailing newline via `enter=True`. This matters because inner's `sys.stdin.readline()` needs a newline to unblock.
- **Base64 alphabet caveat.** Standard base64 uses `+` and `/`. Both are allowed inside the `[A-Za-z0-9+/=]+` regex class in `protocol.py`, but if you're tempted to switch to URL-safe base64 (`-`/`_`), update the regex too.
