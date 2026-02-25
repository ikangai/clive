"""Shared LLM client for planner and executor."""

import os

import openai
from dotenv import load_dotenv

load_dotenv()

MODEL = os.getenv("AGENT_MODEL", "z-ai/glm-5")
OPENROUTER_BASE = "https://openrouter.ai/api/v1"


def get_client() -> openai.OpenAI:
    return openai.OpenAI(
        base_url=OPENROUTER_BASE,
        api_key=os.environ["OPENROUTER_API_KEY"],
    )


def chat(
    client: openai.OpenAI,
    messages: list[dict],
    max_tokens: int = 1024,
    model: str | None = None,
) -> tuple[str, int, int]:
    """Send chat completion. Returns (content, prompt_tokens, completion_tokens)."""
    response = client.chat.completions.create(
        model=model or MODEL,
        messages=messages,
        max_tokens=max_tokens,
    )
    content = response.choices[0].message.content or ""
    pt = response.usage.prompt_tokens if response.usage else 0
    ct = response.usage.completion_tokens if response.usage else 0
    return content, pt, ct
