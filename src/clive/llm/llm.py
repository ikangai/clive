"""Shared LLM client for planner and executor."""

import os
import random
import time

import anthropic
import openai
from dotenv import load_dotenv

load_dotenv()

PROVIDERS = {
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "api_key_env": "OPENAI_API_KEY",
        "default_model": "gpt-4o",
    },
    "anthropic": {
        "base_url": None,
        "api_key_env": "ANTHROPIC_API_KEY",
        "default_model": "claude-sonnet-4-20250514",
    },
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "api_key_env": "GOOGLE_API_KEY",
        "default_model": "gemini-2.0-flash",
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_env": "OPENROUTER_API_KEY",
        "default_model": "z-ai/glm-5",
    },
    "lmstudio": {
        "base_url": "http://localhost:1234/v1",
        "api_key_env": None,
        "default_model": "local",
    },
    "ollama": {
        "base_url": "http://localhost:11434/v1",
        "api_key_env": None,
        "default_model": "llama3",
    },
    "delegate": {
        # Inference routed back over the conversational channel to the
        # outer clive — no HTTP, no API key, no network. See
        # delegate_client.py and protocol.py llm_request / llm_response.
        "base_url": None,
        "api_key_env": None,
        "default_model": "delegate",
    },
    "claude-cli": {
        # Inference served by shelling out to the `claude` CLI (`claude -p`),
        # i.e. the user's Claude Code subscription — no HTTP API, no API key.
        # The call is ISOLATED (`--setting-sources "" + --tools "" + empty
        # --mcp-config`, see _build_claude_cli_argv) so it is a pure completion
        # engine, NOT a full Claude Code agent — it loads no plugins/hooks/MCP and
        # cannot reach the host. Auth stays the subscription keychain (NOT --bare,
        # which would disable it); under a sandbox HOME, CLIVE_CLAUDECLI_HOME
        # repoints HOME at the real login keychain (see _build_claude_cli_env).
        "base_url": None,
        "api_key_env": None,
        "default_model": "claude-cli",  # "use Claude Code's default model"; set AGENT_MODEL=sonnet|opus|haiku to pick
    },
}

PROVIDER_NAME = os.getenv("LLM_PROVIDER", "openrouter")
if PROVIDER_NAME not in PROVIDERS:
    _valid = ", ".join(sorted(PROVIDERS.keys()))
    raise SystemExit(f"Unknown LLM_PROVIDER={PROVIDER_NAME!r}. Valid providers: {_valid}")
_provider = PROVIDERS[PROVIDER_NAME]
MODEL = os.getenv("AGENT_MODEL", _provider["default_model"])
SCRIPT_MODEL = os.getenv("SCRIPT_MODEL", MODEL)
CLASSIFIER_MODEL = os.getenv("CLASSIFIER_MODEL", "google/gemini-3-flash-preview")


class ClaudeCliClient:
    """Marker client whose inference is served by the `claude` CLI (`claude -p`),
    i.e. the Claude Code subscription, instead of an HTTP API. Mirrors the
    DelegateClient pattern: chat()/chat_with_tools()/chat_stream() branch on
    isinstance(client, ClaudeCliClient)."""

    def __init__(self, model: str | None = None):
        self.model = model


def _render_cli_prompt(messages: list[dict]) -> str:
    """Flatten clive's (system + multi-turn) messages into a single prompt for the
    stateless `claude -p` call. The system/driver instructions lead; the
    conversation follows; the model is asked to continue as the assistant."""
    sys_parts: list[str] = []
    convo: list[str] = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content") or ""
        if isinstance(content, list):  # anthropic block form → flatten text
            content = "".join(b.get("text", "") for b in content if isinstance(b, dict))
        if role == "system":
            sys_parts.append(str(content))
        else:
            convo.append(f"{role.upper()}: {content}")
    blob = ""
    if sys_parts:
        blob += "\n\n".join(sys_parts) + "\n\n"
    blob += "--- Conversation so far ---\n" + "\n\n".join(convo)
    blob += ("\n\nRespond now as ASSISTANT with only your next message, following "
             "the instructions above. Do not add commentary outside that message.")
    return blob


# With --strict-mcp-config, this loads ZERO MCP servers — ignoring every
# user/project/global MCP configuration the operator has.
_EMPTY_MCP_CONFIG = '{"mcpServers": {}}'


def _build_claude_cli_argv(model: str | None = None) -> list[str]:
    """Build the `claude -p` argv for a PANEL completion.

    The panel is a dumb text-completion engine that drives clive; clive
    orchestrates the shell/tools itself, so the panel must carry NO agent surface.
    An un-isolated `claude -p` reads the operator's ~/.claude and becomes a full
    Claude Code agent — loading plugins (e.g. a group-chat plugin that registers a
    teammate handle and POSTS to the live chat), running SessionStart/Stop hooks
    (incl. a multi-minute team barrier), and connecting every configured MCP
    server. These flags strip all of that while PRESERVING subscription auth:

      --setting-sources ""   drop user+project settings → `enabledPlugins` is never
                             read → plugins + their hooks never load. Unlike --bare,
                             this keeps keychain/subscription auth working (--bare
                             ignores the keychain AND CLAUDE_CODE_OAUTH_TOKEN, leaving
                             only ANTHROPIC_API_KEY = API spend, which we avoid).
      --tools ""             zero tools — assistant text only, no Bash/Edit/MCP
      --strict-mcp-config    ignore all ambient MCP config...
      --mcp-config {…}       ...and load an empty server set

    Auth is the subscription keychain (see _build_claude_cli_env for HOME)."""
    argv = [
        "claude", "-p", "--output-format", "json",
        "--setting-sources", "",
        "--tools", "",
        "--strict-mcp-config", "--mcp-config", _EMPTY_MCP_CONFIG,
    ]
    # Only forward a model the `claude` CLI understands (sonnet/opus/haiku or a
    # claude-* id). clive may pass other ids (e.g. the Gemini CLASSIFIER_MODEL
    # default) which the CLI would reject — for those, use Claude Code's default.
    _m = (model or "").lower()
    if _m in ("sonnet", "opus", "haiku") or (_m.startswith("claude-") and _m != "claude-cli"):
        argv += ["--model", model]
    return argv


def _build_claude_cli_env(base: dict[str, str]) -> dict[str, str]:
    """Process env for an isolated `claude -p`. The subscription credential lives in
    the macOS login keychain, which is reachable only under the REAL home. clive
    runs the candidate under HOME=sandbox, so repoint HOME at the real home (carried
    in CLIVE_CLAUDECLI_HOME) for the `claude -p` subprocess. SAFE now: the agent
    surface is disabled by the argv flags (--setting-sources ""/--tools ""/empty
    --mcp-config), NOT by withholding HOME — so the real home no longer drags in
    plugins, hooks, or the group chat."""
    env = dict(base)
    real_home = base.get("CLIVE_CLAUDECLI_HOME")
    if real_home:
        env["HOME"] = real_home
    return env


def _claude_cli_complete(messages: list[dict], model: str | None = None,
                         timeout: int = 300) -> tuple[str, int, int]:
    import json as _json
    import subprocess
    import tempfile

    prompt = _render_cli_prompt(messages)
    argv = _build_claude_cli_argv(model)
    env = _build_claude_cli_env(dict(os.environ))

    def _spawn():
        # Run from a throwaway cwd so no project .claude/CLAUDE.md is discovered
        # (defense in depth alongside --setting-sources "", which drops settings).
        with tempfile.TemporaryDirectory(prefix="clive-panel-") as _cwd:
            return subprocess.run(argv, input=prompt, capture_output=True, text=True,
                                  timeout=timeout, env=env, cwd=_cwd)

    try:
        # Retry ONLY transient spawn failures (OSError — e.g. EAGAIN while forking
        # under load) with bounded backoff. A clean nonzero exit is NOT an
        # exception — _spawn() returns the CompletedProcess and it's handled below
        # with no retry — so a deterministic bad-arg/auth failure fails fast. A
        # non-transient exception (timeout, etc.) also fails fast and is surfaced
        # below as a text turn so the runner still records a turn.
        p = _with_retry(_spawn)
    except Exception as e:  # noqa: BLE001 — surface as text so the runner records a turn
        est = sum(len(str(m.get("content", ""))) for m in messages) // 4
        return f"[claude-cli error: {e}]", est, 0

    out = (p.stdout or "").strip()
    content = out
    try:
        d = _json.loads(out)
        content = d.get("result", "") or d.get("text", "") or ""
        if d.get("is_error"):
            content = f"[claude-cli: {content or 'error'}]"
    except Exception:
        content = out or (p.stderr or "").strip()

    # Report clive-SIDE token estimates, not `claude -p`'s usage — the latter is
    # inflated by Claude Code's own ~15k-token system context, which would blow
    # clive's --max-tokens budget on the first turn.
    pt = sum(len(str(m.get("content", ""))) for m in messages) // 4
    ct = len(content) // 4
    return content, pt, ct


_client_cache = None

def get_client():
    global _client_cache
    if _client_cache is not None:
        return _client_cache

    # Delegate provider uses stdio, not HTTP — bail out of the api_key
    # path entirely. The outer clive pays for inference; the inner
    # just serializes requests and reads responses.
    if PROVIDER_NAME == "delegate":
        from delegate_client import DelegateClient
        _client_cache = DelegateClient()
        return _client_cache

    # claude-cli provider: no HTTP client — inference shells to `claude -p`.
    if PROVIDER_NAME == "claude-cli":
        _client_cache = ClaudeCliClient(model=None if MODEL == "claude-cli" else MODEL)
        return _client_cache

    api_key_env = _provider["api_key_env"]
    api_key = os.environ.get(api_key_env) if api_key_env else "not-needed"

    # LLM_BASE_URL, when set, overrides the provider's default base URL.
    # Lets users point at a self-hosted proxy (LiteLLM, self-hosted
    # Claude gateway, etc.) without editing the PROVIDERS dict. Both
    # SDKs (openai and anthropic) accept a base_url constructor
    # parameter — we thread the override through to whichever one
    # the active provider uses.
    base_url_override = os.environ.get("LLM_BASE_URL")

    if PROVIDER_NAME == "anthropic":
        kwargs = {"api_key": api_key}
        if base_url_override:
            kwargs["base_url"] = base_url_override
        _client_cache = anthropic.Anthropic(**kwargs)
    else:
        base_url = base_url_override or _provider["base_url"]
        _client_cache = openai.OpenAI(base_url=base_url, api_key=api_key)

    return _client_cache


# ─── Bounded retry for transient provider errors ────────────────────────────
# A single bare provider call fails the whole subtask the moment a transient
# 429/5xx/connection-reset hits (interactive_runner's lone fallback then also
# fails). _with_retry() wraps the NON-STREAMING completion call sites with a
# small, capped exponential backoff + jitter. It retries ONLY transient errors
# and fails fast on 4xx auth/validation. Streaming (chat_stream) and the
# delegate/claude-cli subprocess paths need different reconnection semantics and
# are deliberately left for a follow-up.

RETRY_MAX_ATTEMPTS = 3   # total tries: 1 initial + up to 2 retries
RETRY_BASE_DELAY = 0.5   # seconds; doubled each retry
RETRY_MAX_DELAY = 8.0    # ceiling on the (pre-jitter) backoff per sleep


def _is_transient(exc: Exception) -> bool:
    """True for errors worth retrying: connection/timeout failures, HTTP 429
    (rate limit), and 5xx server errors. False for everything else — notably
    4xx auth/validation (401/403/400/404/422), which must fail fast."""
    # Connection-level failures (DNS, reset, read timeout) — anthropic and
    # openai both expose APIConnectionError (APITimeoutError is a subclass).
    if isinstance(exc, (anthropic.APIConnectionError, openai.APIConnectionError)):
        return True
    # Server-side 5xx — transient by definition (503/502/500/504).
    if isinstance(exc, (anthropic.InternalServerError, openai.InternalServerError)):
        return True
    # Rate limiting (429).
    if isinstance(exc, (anthropic.RateLimitError, openai.RateLimitError)):
        return True
    # Catch-all by HTTP status for any other status-carrying error (e.g. a proxy
    # in front of the provider): retry 429 + 5xx, never other 4xx.
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int):
        return status == 429 or status >= 500
    # Subprocess spawn / stdio-transport failures (delegate + claude-cli paths):
    # a fork/exec that fails to even start the child, or a broken/reset pipe, is
    # an OSError (e.g. EAGAIN "resource temporarily unavailable" under load,
    # ECONNRESET, EPIPE, or a transiently-missing binary) and is worth a bounded
    # retry. A request TIMEOUT is NOT — re-running a multi-minute call that
    # already ran out the clock is wasteful — so exclude TimeoutError (which
    # subclasses OSError). A clean nonzero EXIT is not an exception at all (it
    # returns a CompletedProcess) so it never reaches here and fails fast.
    if isinstance(exc, OSError) and not isinstance(exc, TimeoutError):
        return True
    return False


def _with_retry(call):
    """Invoke *call*() with bounded exponential backoff + jitter, retrying ONLY
    transient errors (connection/timeout/429/5xx) up to RETRY_MAX_ATTEMPTS total
    attempts. Permanent errors (4xx auth/validation) propagate immediately with
    no retry. Once the cap is hit, the last transient error is re-raised."""
    attempt = 0
    while True:
        try:
            return call()
        except Exception as exc:  # noqa: BLE001 — re-raised below unless retried
            attempt += 1
            if attempt >= RETRY_MAX_ATTEMPTS or not _is_transient(exc):
                raise
            delay = min(RETRY_BASE_DELAY * (2 ** (attempt - 1)), RETRY_MAX_DELAY)
            delay += random.uniform(0, delay)  # full jitter to de-correlate retries
            time.sleep(delay)


def chat(
    client,
    messages: list[dict],
    max_tokens: int = 1024,
    model: str | None = None,
    temperature: float | None = None,
) -> tuple[str, int, int]:
    """Send chat completion. Returns (content, prompt_tokens, completion_tokens)."""
    # Delegate branch first — its transport is stdio, not HTTP, and
    # the openai SDK duck-type is minimal enough that the shared code
    # below would work, but splitting it out keeps the control flow
    # obvious for future maintainers.
    if isinstance(client, ClaudeCliClient):
        return _claude_cli_complete(messages, model=model or client.model)

    from delegate_client import DelegateClient
    if isinstance(client, DelegateClient):
        # Wrap the stdio invoke in the same bounded retry: a transient transport
        # failure (broken/reset pipe) is retried, while a deterministic
        # delegate-side error (RuntimeError from an llm_error frame) or a timeout
        # propagates immediately with no retry.
        resp = _with_retry(lambda: client.chat.completions.create(
            model=model or MODEL,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        ))
        content = resp.choices[0].message.content or ""
        return content, resp.usage.prompt_tokens, resp.usage.completion_tokens

    if isinstance(client, anthropic.Anthropic):
        # Anthropic takes system as a top-level param, not in messages
        system = ""
        filtered = []
        for msg in messages:
            if msg["role"] == "system":
                system = msg["content"]
            else:
                filtered.append(msg)

        # Use cache_control for system prompt (cached after first call, 90% cheaper)
        system_blocks = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}] if system else []
        kwargs = dict(
            model=model or MODEL,
            max_tokens=max_tokens,
            system=system_blocks,
            messages=filtered,
        )
        if temperature is not None:
            kwargs["temperature"] = temperature
        response = _with_retry(lambda: client.messages.create(**kwargs))
        content = response.content[0].text if response.content else ""
        pt = response.usage.input_tokens if response.usage else 0
        ct = response.usage.output_tokens if response.usage else 0
        return content, pt, ct

    kwargs = dict(
        model=model or MODEL,
        messages=messages,
        max_tokens=max_tokens,
    )
    if temperature is not None:
        kwargs["temperature"] = temperature
    response = _with_retry(lambda: client.chat.completions.create(**kwargs))
    content = response.choices[0].message.content or ""
    pt = response.usage.prompt_tokens if response.usage else 0
    ct = response.usage.completion_tokens if response.usage else 0
    return content, pt, ct


def chat_with_tools(
    client,
    messages: list[dict],
    tools: list[dict],
    max_tokens: int = 1024,
    model: str | None = None,
) -> tuple[list, str, int, int]:
    """Send chat completion with tool definitions.

    Returns (tool_calls_raw, text_content, prompt_tokens, completion_tokens).

    tool_calls_raw is the provider-native list of tool-call objects; use
    ``tool_defs.parse_tool_calls(raw, format=...)`` to normalise them.
    """
    if isinstance(client, ClaudeCliClient):
        # No native tool calling over the CLI — fall back to plain-text chat, so
        # clive uses its plain-text bash-block command protocol.
        text, pt, ct = chat(client, messages, max_tokens=max_tokens, model=model)
        return [], text, pt, ct

    from delegate_client import DelegateClient
    if isinstance(client, DelegateClient):
        # DelegateClient does not support tool calling — fall back to
        # plain text chat and return no tool calls.
        text, pt, ct = chat(client, messages, max_tokens=max_tokens, model=model)
        return [], text, pt, ct

    if isinstance(client, anthropic.Anthropic):
        system = ""
        filtered = []
        for msg in messages:
            if msg["role"] == "system":
                system = msg["content"]
            else:
                filtered.append(msg)

        system_blocks = (
            [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
            if system else []
        )

        response = _with_retry(lambda: client.messages.create(
            model=model or MODEL,
            max_tokens=max_tokens,
            system=system_blocks,
            messages=filtered,
            tools=tools,
        ))

        # Separate text content and tool-use blocks
        text_parts = []
        tool_calls_raw = []
        for block in response.content:
            if getattr(block, "type", None) == "text":
                text_parts.append(block.text)
            else:
                tool_calls_raw.append(block)

        text = "\n".join(text_parts) if text_parts else ""
        pt = response.usage.input_tokens if response.usage else 0
        ct = response.usage.output_tokens if response.usage else 0
        return tool_calls_raw, text, pt, ct

    # OpenAI-compatible path
    from tool_defs import tools_for_openai
    openai_tools = tools_for_openai()

    response = _with_retry(lambda: client.chat.completions.create(
        model=model or MODEL,
        messages=messages,
        max_tokens=max_tokens,
        tools=openai_tools,
    ))

    choice = response.choices[0]
    text = choice.message.content or ""
    tool_calls_raw = choice.message.tool_calls or []
    pt = response.usage.prompt_tokens if response.usage else 0
    ct = response.usage.completion_tokens if response.usage else 0
    return tool_calls_raw, text, pt, ct


def chat_stream(
    client,
    messages: list[dict],
    max_tokens: int = 1024,
    model: str | None = None,
    on_token: callable = None,
    should_stop: callable = None,
) -> tuple[str, int, int]:
    """Streaming chat — calls on_token(partial_text) as tokens arrive.

    Returns same (content, prompt_tokens, completion_tokens) as chat().

    If *should_stop* is provided, it is called after each token.  When
    it returns True the stream is aborted and the content accumulated so
    far is returned.  Token counts are estimated when the stream is cut
    short (exact counts require the provider to finish generating).
    """
    # Delegate does not stream in v1 — fall back to non-streaming chat()
    # and fire on_token exactly once with the full content. Streaming
    # is a phase-2 follow-up.
    if isinstance(client, ClaudeCliClient):
        # No streaming over the CLI — complete and fire on_token once.
        content, pt, ct = chat(client, messages, max_tokens=max_tokens, model=model)
        if on_token:
            on_token(content)
        return content, pt, ct

    from delegate_client import DelegateClient
    if isinstance(client, DelegateClient):
        content, pt, ct = chat(client, messages, max_tokens=max_tokens, model=model)
        if on_token:
            on_token(content)
        return content, pt, ct

    if isinstance(client, anthropic.Anthropic):
        system = ""
        filtered = []
        for msg in messages:
            if msg["role"] == "system":
                system = msg["content"]
            else:
                filtered.append(msg)

        system_blocks = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}] if system else []

        # Retry the stream SETUP/connect (transient-only): the request is issued
        # on __enter__, so a connection failure there is retried. Once tokens are
        # flowing we do NOT retry — a mid-flight break propagates as a clean
        # failure rather than silently re-running a half-consumed stream (which
        # would double-fire on_token and risk truncated/duplicated content).
        def _open_stream():
            mgr = client.messages.stream(
                model=model or MODEL,
                max_tokens=max_tokens,
                system=system_blocks,
                messages=filtered,
            )
            return mgr, mgr.__enter__()

        manager, stream = _with_retry(_open_stream)

        content_parts = []
        pt, ct = 0, 0
        stopped_early = False
        try:
            for text in stream.text_stream:
                content_parts.append(text)
                if on_token:
                    on_token("".join(content_parts))
                if should_stop and should_stop():
                    stopped_early = True
                    break

            if not stopped_early:
                # Get final message for usage
                final = stream.get_final_message()
                pt = final.usage.input_tokens
                ct = final.usage.output_tokens
        finally:
            # Always close the stream — including when consumption raised, so the
            # mid-flight break surfaces cleanly with the connection released.
            manager.__exit__(None, None, None)

        content = "".join(content_parts)
        if stopped_early:
            pt = sum(len(m.get("content", "")) // 4 for m in messages)
            ct = len(content) // 4
        return content, pt, ct

    # OpenAI-compatible: use streaming. Retry the stream SETUP (the create call
    # that opens the connection) on transient errors only; do NOT retry once
    # chunks are flowing below — a mid-stream break propagates as a clean failure
    # rather than silently re-running a half-consumed stream.
    response = _with_retry(lambda: client.chat.completions.create(
        model=model or MODEL,
        messages=messages,
        max_tokens=max_tokens,
        stream=True,
    ))

    content_parts = []
    pt, ct = 0, 0
    for chunk in response:
        if chunk.choices and chunk.choices[0].delta.content:
            content_parts.append(chunk.choices[0].delta.content)
            if on_token:
                on_token("".join(content_parts))
            if should_stop and should_stop():
                break
        if hasattr(chunk, 'usage') and chunk.usage:
            pt = chunk.usage.prompt_tokens or 0
            ct = chunk.usage.completion_tokens or 0

    content = "".join(content_parts)
    # Some providers don't report usage in stream, estimate from content
    if pt == 0:
        pt = sum(len(m.get("content", "")) // 4 for m in messages)
    if ct == 0:
        ct = len(content) // 4

    return content, pt, ct
