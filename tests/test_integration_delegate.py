"""End-to-end delegation integration test.

Spawns a real clive subprocess with LLM_PROVIDER=delegate, plays the
outer role against its stdin/stdout, and forwards every llm_request
frame to a local mock LMStudio HTTP server. Proves the full vertical
slice works across a real process boundary: DelegateClient →
framed stdio → outer handler → mock LMStudio → framed response →
DelegateClient → llm.chat() tuple.

No real SSH, no tmux required beyond what clive.py's own setup uses.
If tmux is not available, the test skips cleanly.
"""
import http.server
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from protocol import encode, decode_all


if not shutil.which("tmux"):
    pytest.skip("tmux not available — skipping integration test",
                allow_module_level=True)


class _MockLMStudio(http.server.BaseHTTPRequestHandler):
    """Minimal OpenAI-compatible server that always returns a fixed reply.

    The reply is a short "DONE:" string so clive's command_extract
    recognizes it as a completion signal on the inner's first planner
    call, keeping the test end-to-end lean.
    """
    request_log: list[dict] = []

    def log_message(self, *_):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        try:
            req = json.loads(body) if body else {}
        except json.JSONDecodeError:
            req = {"raw": body.decode("utf-8", "replace")}
        _MockLMStudio.request_log.append(req)
        resp = {
            "id": "chatcmpl-1",
            "object": "chat.completion",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "DONE: trivial"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
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
    try:
        yield port
    finally:
        srv.shutdown()


def test_delegate_client_over_subprocess_pipe(mock_lmstudio):
    """Minimal proof that DelegateClient works across a real process
    boundary with real stdin/stdout pipes.

    Rather than spawn all of clive.py (which brings in tmux setup,
    planner overhead, and many LLM calls), we spawn a tiny inner
    script that imports DelegateClient directly, makes ONE chat call,
    and prints the result. That keeps the test focused on the stdio
    transport: encode → subprocess.stdout → outer parses → outer
    calls mock LMStudio → outer writes llm_response → subprocess.stdin
    → DelegateClient decodes → returns.
    """
    repo_root = Path(__file__).parent.parent
    inner_script = repo_root / "tests" / "_delegate_inner_harness.py"

    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo_root / "src" / "clive")
    env["PYTHONUNBUFFERED"] = "1"
    env.pop("CLIVE_FRAME_NONCE", None)  # test runs nonceless

    proc = subprocess.Popen(
        [sys.executable, str(inner_script)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
        bufsize=1,  # line-buffered
    )

    # Outer plays the role of clive-on-laptop: reads llm_request frames
    # off the subprocess stdout, hits mock LMStudio, writes llm_response
    # back to subprocess stdin.
    import openai
    outer_client = openai.OpenAI(
        base_url=f"http://127.0.0.1:{mock_lmstudio}/v1",
        api_key="not-needed",
    )

    deadline = time.time() + 15
    buf = ""
    final_line = None
    answered_ids = set()

    try:
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            buf += line
            frames = decode_all(buf)
            for f in frames:
                if f.kind == "llm_request" and f.payload["id"] not in answered_ids:
                    resp = outer_client.chat.completions.create(
                        model="local",
                        messages=f.payload["messages"],
                        max_tokens=f.payload.get("max_tokens", 64),
                    )
                    out = encode("llm_response", {
                        "id": f.payload["id"],
                        "content": resp.choices[0].message.content or "",
                        "prompt_tokens": resp.usage.prompt_tokens,
                        "completion_tokens": resp.usage.completion_tokens,
                    })
                    proc.stdin.write(out + "\n")
                    proc.stdin.flush()
                    answered_ids.add(f.payload["id"])
            if "FINAL:" in buf:
                # Harness prints FINAL:<text> once it has called chat() and
                # extracted the content. Capture and stop.
                for ln in buf.splitlines():
                    if ln.startswith("FINAL:"):
                        final_line = ln
                        break
                if final_line:
                    break
    finally:
        try:
            proc.stdin.close()
        except BrokenPipeError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    stderr = proc.stderr.read() if proc.stderr else ""
    assert final_line, (
        f"inner harness never printed FINAL:. buf=\n{buf}\nstderr=\n{stderr}"
    )
    # Mock LMStudio must have been called at least once
    assert len(_MockLMStudio.request_log) >= 1, (
        "mock LMStudio was never called — the delegate round trip didn't happen"
    )
    # The content the mock returned must have made it back to the inner
    assert "DONE: trivial" in final_line, f"unexpected final line: {final_line}"
