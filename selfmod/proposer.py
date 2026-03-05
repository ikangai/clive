"""Proposer role — generates code modifications."""

import json

from llm import get_client, chat
from selfmod.constitution import load_constitution, get_tier

PROPOSER_PROMPT = """You are the PROPOSER in clive's self-modification system.

Your job: generate precise, minimal code changes to achieve the user's goal.

CONSTITUTION (you must follow these rules):
{constitution}

RULES:
1. Output ONLY valid JSON — no markdown fences, no commentary.
2. Each file change must include the COMPLETE new file content.
3. Minimize the blast radius: change as few files as possible.
4. Never modify IMMUTABLE files (selfmod/gate.py, .clive/constitution.md, .clive/audit/*).
5. Follow existing code style and conventions.
6. Do not introduce security vulnerabilities.

CURRENT FILES (for context):
{file_context}

Respond with this JSON structure:
{{
  "description": "What this modification does and why",
  "files": {{
    "relative/path/to/file.py": "complete new file content here"
  }},
  "rationale": "Why this change is safe and beneficial"
}}"""


def propose(
    goal: str,
    file_context: dict[str, str],
) -> tuple[dict, int, int]:
    """Generate a modification proposal.

    Args:
        goal: what the user wants to change
        file_context: current content of relevant files

    Returns:
        (proposal_dict, prompt_tokens, completion_tokens)
    """
    constitution = load_constitution()

    context_str = ""
    for path, content in file_context.items():
        tier = get_tier(path)
        context_str += f"\n--- {path} [{tier}] ---\n{content}\n"

    client = get_client()
    messages = [
        {
            "role": "system",
            "content": PROPOSER_PROMPT.format(
                constitution=constitution,
                file_context=context_str,
            ),
        },
        {"role": "user", "content": f"Goal: {goal}"},
    ]

    raw, pt, ct = chat(client, messages, max_tokens=4096)

    # Parse JSON
    clean = raw.strip()
    if clean.startswith("```"):
        clean = clean.split("\n", 1)[1].rsplit("```", 1)[0].strip()

    proposal = json.loads(clean)
    return proposal, pt, ct
