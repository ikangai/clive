"""Reviewer role — checks code quality and correctness."""

import json
import os

from llm import get_client, chat
from selfmod.constitution import load_constitution, get_tier

REVIEWER_TEMPERATURE = 0.1


def get_selfmod_model() -> str | None:
    """Get the model to use for selfmod review/audit. Returns None for default."""
    return os.environ.get("CLIVE_SELFMOD_MODEL") or None

REVIEWER_PROMPT = """You are the REVIEWER in clive's self-modification system.

Your job: evaluate a proposed modification for code quality, correctness,
and safety. You are INDEPENDENT of the Proposer — do not defer to their
rationale. Form your own judgment.

CONSTITUTION (these are the rules):
{constitution}

PROPOSED CHANGES:
{proposal}

CURRENT FILES (before modification):
{current_files}

Evaluate:
1. Does the code do what the description says?
2. Are there bugs, edge cases, or regressions?
3. Does it follow the project's coding style?
4. Are there security concerns?
5. Is the tier classification correct?
6. Is this the minimal change needed?

Respond with ONLY this JSON (no markdown fences):
{{
  "verdict": "approved" or "rejected",
  "issues": ["list of specific issues found"],
  "suggestions": ["optional improvements"],
  "risk_assessment": "low/medium/high",
  "reasoning": "your independent analysis"
}}"""


def build_review_prompt(proposal: dict, current_files: dict[str, str]) -> str:
    """Build the review prompt, stripping proposer reasoning for information barrier."""
    constitution = load_constitution()

    # Only pass the diff (files), NOT the proposer's description/rationale
    sanitized_proposal = {"files": proposal.get("files", {})}
    proposal_str = json.dumps(sanitized_proposal, indent=2)

    current_str = ""
    for path, content in current_files.items():
        tier = get_tier(path)
        current_str += f"\n--- {path} [{tier}] ---\n{content}\n"

    return REVIEWER_PROMPT.format(
        constitution=constitution,
        proposal=proposal_str,
        current_files=current_str,
    )


def review(
    proposal: dict,
    current_files: dict[str, str],
) -> tuple[dict, int, int]:
    """Review a modification proposal.

    Args:
        proposal: the Proposer's output
        current_files: current content of affected files

    Returns:
        (review_dict, prompt_tokens, completion_tokens)
    """
    prompt = build_review_prompt(proposal, current_files)

    client = get_client()
    kwargs = {"max_tokens": 2048, "temperature": REVIEWER_TEMPERATURE}
    model = get_selfmod_model()
    if model:
        kwargs["model"] = model

    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": "Review this proposal."},
    ]

    raw, pt, ct = chat(client, messages, **kwargs)

    clean = raw.strip()
    if clean.startswith("```"):
        clean = clean.split("\n", 1)[1].rsplit("```", 1)[0].strip()

    result = json.loads(clean)
    return result, pt, ct
