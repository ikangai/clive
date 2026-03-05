"""Proposer role — generates code modifications."""

import json
import logging
import re

from llm import get_client, chat
from selfmod.constitution import load_constitution, get_tier

log = logging.getLogger(__name__)

PROPOSER_PROMPT = """You are the PROPOSER in clive's self-modification system.

Your job: generate precise, minimal code changes to achieve the user's goal.

CONSTITUTION (you must follow these rules):
{constitution}

RULES:
1. Minimize the blast radius: change as few files as possible.
2. Never modify IMMUTABLE files (selfmod/gate.py, .clive/constitution.md, .clive/audit/*).
3. Follow existing code style and conventions.
4. Do not introduce security vulnerabilities.

CURRENT FILES (for context):
{file_context}

OUTPUT FORMAT — use this exact delimited format (NOT JSON, because file contents break JSON):

===PROPOSAL===
DESCRIPTION: What this modification does and why
RATIONALE: Why this change is safe and beneficial
===FILE: relative/path/to/file.py===
complete new file content here
===FILE: another/file.py===
complete new file content here
===END===

CRITICAL: Include the COMPLETE new file content for each changed file.
Only include files you are actually changing."""


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

    total_pt, total_ct = 0, 0

    # Try up to 2 times (initial + 1 retry with error feedback)
    for attempt in range(2):
        raw, pt, ct = chat(client, messages, max_tokens=16384)
        total_pt += pt
        total_ct += ct

        try:
            proposal = _parse_proposal(raw)
            if proposal.get("files"):
                return proposal, total_pt, total_ct
            raise ValueError("Proposal contains no file changes")
        except (ValueError, KeyError) as e:
            log.debug(f"Proposal parse attempt {attempt + 1} failed: {e}")
            if attempt == 0:
                messages.append({"role": "assistant", "content": raw})
                messages.append({
                    "role": "user",
                    "content": (
                        f"Your response could not be parsed: {e}\n\n"
                        "Please respond again using the exact delimited format:\n"
                        "===PROPOSAL===\n"
                        "DESCRIPTION: ...\n"
                        "RATIONALE: ...\n"
                        "===FILE: path===\n"
                        "content\n"
                        "===END==="
                    ),
                })

    raise ValueError(f"Failed to parse proposal after 2 attempts")


def _parse_proposal(raw: str) -> dict:
    """Parse the delimited proposal format. Falls back to JSON."""
    text = raw.strip()

    # Strip markdown fences if present
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()

    # Try delimited format first
    if "===PROPOSAL===" in text:
        return _parse_delimited(text)

    # Fallback: try JSON
    return json.loads(text)


def _parse_delimited(text: str) -> dict:
    """Parse the ===PROPOSAL=== / ===FILE=== / ===END=== format."""
    # Extract description
    desc_match = re.search(r"DESCRIPTION:\s*(.+?)(?:\n|RATIONALE:)", text, re.DOTALL)
    description = desc_match.group(1).strip() if desc_match else ""

    # Extract rationale
    rat_match = re.search(r"RATIONALE:\s*(.+?)(?:\n===FILE:|\n===END===)", text, re.DOTALL)
    rationale = rat_match.group(1).strip() if rat_match else ""

    # Extract files
    files = {}
    file_pattern = re.compile(r"===FILE:\s*(.+?)===\n(.*?)(?=\n===FILE:|\n===END===|$)", re.DOTALL)
    for match in file_pattern.finditer(text):
        filepath = match.group(1).strip()
        content = match.group(2)
        # Remove trailing whitespace but preserve internal structure
        files[filepath] = content.rstrip() + "\n"

    if not files:
        raise ValueError("No ===FILE: sections found in proposal")

    return {
        "description": description,
        "files": files,
        "rationale": rationale,
    }
