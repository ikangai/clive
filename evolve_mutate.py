"""Mutation strategies for driver prompt evolution.

Each strategy targets a different optimization goal.
The LLM sees the current driver + eval results and produces an improved version.
"""
import os
import tempfile

from llm import get_client, chat

STRATEGIES = [
    {
        "name": "token_optimizer",
        "goal": "Minimize total token usage across all tasks. Make instructions more concise. Remove redundant examples. Use terse, high-signal phrasing.",
    },
    {
        "name": "turn_optimizer",
        "goal": "Minimize the number of turns needed to complete tasks. Help the agent get things right on the first try. Add patterns for common operations so the agent doesn't need to explore.",
    },
    {
        "name": "robustness_optimizer",
        "goal": "Minimize failures and repair loops. Add error prevention patterns. Warn about common pitfalls more prominently. Improve script-mode compatibility.",
    },
]


def build_mutation_prompt(
    current_driver: str,
    eval_summary: str,
    strategy: dict,
) -> str:
    return f"""You are optimizing a driver prompt for a terminal agent.

The agent reads the terminal screen and types commands. The driver prompt is a compact reference card that gives the agent tool-specific knowledge. Better driver prompts = fewer turns, fewer tokens, fewer failures.

Current driver prompt:
---
{current_driver}
---

Last eval results:
{eval_summary}

Optimization goal: {strategy["goal"]}

Constraints:
- Must remain a compact reference card (under 80 lines)
- Keep the same markdown structure (# heading, SECTION: content)
- Do not remove information categories, only restructure or clarify
- Do not add conversational text or explanations — terse reference format only

Write the improved driver prompt. Output ONLY the driver prompt content, no explanation."""


def generate_variants(
    driver_path: str,
    eval_summary: str,
    num_variants: int = 3,
) -> list[str]:
    """Generate N variant driver prompts. Returns list of temp file paths."""
    with open(driver_path, "r") as f:
        current_driver = f.read().strip()

    client = get_client()
    variants = []

    for i in range(num_variants):
        strategy = STRATEGIES[i % len(STRATEGIES)]

        prompt = build_mutation_prompt(current_driver, eval_summary, strategy)
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": "Generate the improved driver prompt."},
        ]

        reply, _, _ = chat(client, messages, max_tokens=4096)

        # Strip markdown fences if present
        content = reply.strip()
        if content.startswith("```"):
            lines = content.split("\n")
            lines = lines[1:]  # remove opening fence
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            content = "\n".join(lines)

        # Write to temp file
        fd, path = tempfile.mkstemp(suffix=".md", prefix=f"driver_variant_{i}_")
        with os.fdopen(fd, "w") as f:
            f.write(content)
        variants.append(path)

    return variants


def format_eval_summary(report_dict: dict) -> str:
    """Format an eval report dict into a summary string for the mutation prompt."""
    lines = []
    lines.append(f"{report_dict['passed']}/{report_dict['total_tasks']} passed "
                 f"({report_dict['completion_rate']:.0%})")
    lines.append(f"Turn efficiency: {report_dict['avg_turn_efficiency']:.0%}")
    lines.append(f"Total tokens: {report_dict['total_tokens']:,}")
    lines.append("")
    for r in report_dict.get("results", []):
        status = "PASS" if r["passed"] else "FAIL"
        lines.append(f"  [{status}] {r['task_id']}: {r['turns']} turns, {r['tokens']} tokens")
    return "\n".join(lines)
