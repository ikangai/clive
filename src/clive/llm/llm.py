"""Shared LLM client for planner and executor."""

import os

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
}

PROVIDER_NAME = os.getenv("LLM_PROVIDER", "openrouter")
if PROVIDER_NAME not in PROVIDERS:
    _valid = ", ".join(sorted(PROVIDERS.keys()))
    raise SystemExit(f"Unknown LLM_PROVIDER={PROVIDER_NAME!r}. Valid providers: {_valid}")
_provider = PROVIDERS[PROVIDER_NAME]
MODEL = os.getenv("AGENT_MODEL", _provider["default_model"])
SCRIPT_MODEL = os.getenv("SCRIPT_MODEL", MODEL)
CLASSIFIER_MODEL = os.getenv("CLASSIFIER_MODEL", "google/gemini-3-flash-preview")


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
        response = client.messages.create(**kwargs)
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
    response = client.chat.completions.create(**kwargs)
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

        response = client.messages.create(
            model=model or MODEL,
            max_tokens=max_tokens,
            system=system_blocks,
            messages=filtered,
            tools=tools,
        )

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

    response = client.chat.completions.create(
        model=model or MODEL,
        messages=messages,
        max_tokens=max_tokens,
        tools=openai_tools,
    )

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

        content_parts = []
        pt, ct = 0, 0
        stopped_early = False
        with client.messages.stream(
            model=model or MODEL,
            max_tokens=max_tokens,
            system=system_blocks,
            messages=filtered,
        ) as stream:
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

        content = "".join(content_parts)
        if stopped_early:
            pt = sum(len(m.get("content", "")) // 4 for m in messages)
            ct = len(content) // 4
        return content, pt, ct

    # OpenAI-compatible: use streaming
    response = client.chat.completions.create(
        model=model or MODEL,
        messages=messages,
        max_tokens=max_tokens,
        stream=True,
    )

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
