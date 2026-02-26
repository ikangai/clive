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


def get_client() -> openai.OpenAI | anthropic.Anthropic:
    api_key_env = _provider["api_key_env"]
    api_key = os.environ.get(api_key_env) if api_key_env else "not-needed"

    if PROVIDER_NAME == "anthropic":
        return anthropic.Anthropic(api_key=api_key)

    return openai.OpenAI(base_url=_provider["base_url"], api_key=api_key)


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

        response = client.messages.create(
            model=model or MODEL,
            max_tokens=max_tokens,
            system=system,
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
