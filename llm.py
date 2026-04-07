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
}

PROVIDER_NAME = os.getenv("LLM_PROVIDER", "openrouter")
_provider = PROVIDERS[PROVIDER_NAME]
MODEL = os.getenv("AGENT_MODEL", _provider["default_model"])


_client_cache = None

def get_client() -> openai.OpenAI | anthropic.Anthropic:
    global _client_cache
    if _client_cache is not None:
        return _client_cache

    api_key_env = _provider["api_key_env"]
    api_key = os.environ.get(api_key_env) if api_key_env else "not-needed"

    if PROVIDER_NAME == "anthropic":
        _client_cache = anthropic.Anthropic(api_key=api_key)
    else:
        _client_cache = openai.OpenAI(base_url=_provider["base_url"], api_key=api_key)

    return _client_cache


def chat(
    client: openai.OpenAI | anthropic.Anthropic,
    messages: list[dict],
    max_tokens: int = 1024,
    model: str | None = None,
) -> tuple[str, int, int]:
    """Send chat completion. Returns (content, prompt_tokens, completion_tokens)."""
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
        response = client.messages.create(
            model=model or MODEL,
            max_tokens=max_tokens,
            system=system_blocks,
            messages=filtered,
        )
        content = response.content[0].text if response.content else ""
        pt = response.usage.input_tokens
        ct = response.usage.output_tokens
        return content, pt, ct

    response = client.chat.completions.create(
        model=model or MODEL,
        messages=messages,
        max_tokens=max_tokens,
    )
    content = response.choices[0].message.content or ""
    pt = response.usage.prompt_tokens if response.usage else 0
    ct = response.usage.completion_tokens if response.usage else 0
    return content, pt, ct


def chat_stream(
    client: openai.OpenAI | anthropic.Anthropic,
    messages: list[dict],
    max_tokens: int = 1024,
    model: str | None = None,
    on_token: callable = None,
) -> tuple[str, int, int]:
    """Streaming chat — calls on_token(partial_text) as tokens arrive.

    Returns same (content, prompt_tokens, completion_tokens) as chat().
    Useful for early command detection: the caller can parse the stream
    and act on the command before the full response is generated.
    """
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

            # Get final message for usage
            final = stream.get_final_message()
            pt = final.usage.input_tokens
            ct = final.usage.output_tokens

        return "".join(content_parts), pt, ct

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
