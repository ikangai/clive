"""Auditor role — checks governance compliance."""

import json
import os

from llm import get_client, chat
from selfmod.constitution import load_constitution, get_tier, highest_tier
from selfmod.reviewer import get_selfmod_model

AUDITOR_TEMPERATURE = 0.0

AUDITOR_PROMPT = """You are the AUDITOR in clive's self-modification system.

Your job: verify that a proposed modification complies with the constitution
and governance rules. You are the FINAL CHECK before the deterministic gate.

You are INDEPENDENT of both the Proposer and Reviewer. Focus ONLY on
governance compliance, not code quality.

CONSTITUTION:
{constitution}

PROPOSED CHANGES:
{proposal}

REVIEWER'S ASSESSMENT:
{review}

AFFECTED FILE TIERS:
{tier_info}

Verify:
1. Does this modification comply with all constitutional rules?
2. Is the tier classification correct for all affected files?
3. Are the required approvals present for this tier?
4. Does the change attempt to weaken any safety mechanism?
5. Could this change create a path to bypass governance?

Respond with ONLY this JSON (no markdown fences):
{{
  "verdict": "approved" or "rejected",
  "governance_issues": ["list of governance violations"],
  "tier_correct": true or false,
  "safety_concerns": ["any safety concerns"],
  "reasoning": "your governance analysis"
}}"""


def build_audit_prompt(proposal: dict, review_result: dict) -> str:
    """Build the audit prompt, stripping reviewer reasoning for information barrier."""
    constitution = load_constitution()

    files = proposal.get("files", {})
    tier_info = ""
    for path in files:
        tier = get_tier(path)
        tier_info += f"  {path}: {tier}\n"
    overall_tier = highest_tier(list(files.keys()))
    tier_info += f"\n  Highest tier: {overall_tier}"

    # Only pass verdict + issues, NOT the reviewer's reasoning
    sanitized_review = {
        "verdict": review_result.get("verdict", ""),
        "issues": review_result.get("issues", []),
        "risk_assessment": review_result.get("risk_assessment", ""),
    }

    return AUDITOR_PROMPT.format(
        constitution=constitution,
        proposal=json.dumps({"files": proposal.get("files", {})}, indent=2),
        review=json.dumps(sanitized_review, indent=2),
        tier_info=tier_info,
    )


def audit(
    proposal: dict,
    review_result: dict,
) -> tuple[dict, int, int]:
    """Audit a modification proposal for governance compliance.

    Args:
        proposal: the Proposer's output
        review_result: the Reviewer's output

    Returns:
        (audit_dict, prompt_tokens, completion_tokens)
    """
    prompt = build_audit_prompt(proposal, review_result)

    client = get_client()
    kwargs = {"max_tokens": 2048, "temperature": AUDITOR_TEMPERATURE}
    model = get_selfmod_model()
    if model:
        kwargs["model"] = model

    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": "Audit this proposal for governance compliance."},
    ]

    raw, pt, ct = chat(client, messages, **kwargs)

    clean = raw.strip()
    if clean.startswith("```"):
        clean = clean.split("\n", 1)[1].rsplit("```", 1)[0].strip()

    result = json.loads(clean)
    return result, pt, ct
