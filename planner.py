"""Planning phase: LLM decomposes a task into a subtask DAG."""

import json
import re

from models import Plan, Subtask, PaneInfo
from llm import get_client, chat
from prompts import build_planner_prompt


def create_plan(
    task: str,
    panes: dict[str, PaneInfo],
    tool_status: dict[str, dict],
) -> Plan:
    """Call LLM to decompose task into subtask DAG. Returns validated Plan."""
    client = get_client()

    lines = []
    for name, info in tool_status.items():
        if info["status"] == "ready":
            lines.append(f"  - {name} [{info['app_type']}]: {info['description']}")
    tools_summary = "\n".join(lines)

    system_prompt = build_planner_prompt(tools_summary)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"Task: {task}"},
    ]

    MAX_RETRIES = 3
    content = ""
    for attempt in range(1, MAX_RETRIES + 1):
        content, pt, ct = chat(client, messages, max_tokens=2048)
        print(f"  Planning (attempt {attempt}): {pt} prompt + {ct} completion tokens")

        if content.strip():
            break
        print(f"  WARNING: LLM returned empty response, retrying...")
    else:
        if not content.strip():
            raise ValueError("Planner LLM returned empty response after all retries")

    json_str = _extract_json(content)
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON from planner: {e}\nRaw: {json_str[:500]}")

    plan = Plan(task=task)
    for s in data["subtasks"]:
        plan.subtasks.append(Subtask(
            id=str(s["id"]),
            description=s["description"],
            pane=s["pane"],
            depends_on=[str(d) for d in s.get("depends_on", [])],
        ))

    errors = plan.validate(valid_panes=set(panes.keys()))
    if errors:
        raise ValueError(f"Invalid plan: {'; '.join(errors)}")

    return plan


def _extract_json(text: str) -> str:
    """Extract JSON from LLM response, handling ```json blocks."""
    m = re.search(r'```(?:json)?\s*(\{[\s\S]*?\})\s*```', text)
    if m:
        return m.group(1)
    m = re.search(r'(\{[\s\S]*\})', text)
    if m:
        return m.group(1)
    raise ValueError(f"No JSON found in planner response:\n{text}")


def display_plan(plan: Plan) -> None:
    """Print the execution plan."""
    print(f"\n{'═' * 60}")
    print("EXECUTION PLAN")
    print(f"{'═' * 60}")
    print(f"Task: {plan.task}\n")

    for s in plan.subtasks:
        deps = f" (after: {', '.join(s.depends_on)})" if s.depends_on else ""
        print(f"  [{s.id}] [{s.pane}] {s.description}{deps}")

    # Show parallelism opportunities
    no_deps = [s.id for s in plan.subtasks if not s.depends_on]
    if len(no_deps) > 1:
        print(f"\n  Parallel start: subtasks {', '.join(no_deps)}")

    print(f"{'═' * 60}\n")
