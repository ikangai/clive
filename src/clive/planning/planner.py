"""Planning phase: LLM decomposes a task into a subtask DAG."""

import json
import re

from output import progress, detail
from models import Plan, Subtask, PaneInfo
from llm import get_client, chat
from prompts import build_planner_prompt


def create_plan(
    task: str,
    panes: dict[str, PaneInfo],
    tool_status: dict[str, dict],
    tools_summary: str | None = None,
    session_files: str | None = None,
    recent_history: str | None = None,
) -> Plan:
    """Call LLM to decompose task into subtask DAG. Returns validated Plan."""
    client = get_client()

    if tools_summary is None:
        # Legacy fallback: build from pane status only
        lines = []
        for name, info in tool_status.items():
            if info["status"] == "ready":
                lines.append(f"  - {name} [{info['app_type']}]: {info['description']}")
        tools_summary = "\n".join(lines)

    system_prompt = build_planner_prompt(
        tools_summary,
        session_files=session_files,
        recent_history=recent_history,
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"Task: {task}"},
    ]

    # Bounded validate-and-correct loop. An empty response, unparseable JSON, or
    # a plan that fails plan.validate() (unknown pane, cyclic depends_on, bad
    # mode/shape) does NOT abort the task: we feed the specific error back to the
    # planner and re-ask, up to MAX_RETRIES total attempts. The dominant failure
    # mode here is a near-miss that one corrective turn fixes.
    MAX_RETRIES = 3
    valid_panes = set(panes.keys())
    last_error = "no response"
    for attempt in range(1, MAX_RETRIES + 1):
        content, pt, ct = chat(client, messages, max_tokens=2048)
        progress(f"  Planning (attempt {attempt}): {pt} prompt + {ct} completion tokens")

        if not content.strip():
            last_error = "LLM returned an empty response"
            progress(f"  WARNING: {last_error}, retrying...")
            continue

        try:
            return _parse_plan(content, task, valid_panes)
        except (json.JSONDecodeError, ValueError, KeyError) as e:
            last_error = _describe_plan_error(e)
            progress(f"  WARNING: planner produced an invalid plan ({last_error}); requesting correction...")
            # Standard structured-output repair: show the model its own output
            # and the precise failure, then ask for corrected JSON only. Rebind
            # (don't mutate) so each chat() call records the messages it saw.
            messages = messages + [
                {"role": "assistant", "content": content},
                {"role": "user", "content": (
                    f"Your previous response was not a valid plan: {last_error}\n"
                    "Return ONLY the corrected JSON plan, with no commentary."
                )},
            ]

    raise ValueError(
        f"Planner failed to produce a valid plan after {MAX_RETRIES} attempts. "
        f"Last error: {last_error}"
    )


def _describe_plan_error(e: Exception) -> str:
    """Render a parse/validation error as concise feedback for the planner."""
    if isinstance(e, json.JSONDecodeError):
        return f"invalid JSON: {e}"
    if isinstance(e, KeyError):
        return f"missing required field {e}"
    return str(e)


def _parse_plan(content: str, task: str, valid_panes: set[str]) -> Plan:
    """Parse an LLM response into a validated Plan.

    Raises json.JSONDecodeError (unparseable), KeyError (missing required
    field), or ValueError (no JSON found, bad subtask id, or failed
    plan.validate) — all of which the caller treats as repairable.
    """
    json_str = _extract_json(content)
    data = json.loads(json_str)

    plan = Plan(task=task)
    for s in data["subtasks"]:
        desc = s["description"]
        # If planner assigned a skill, inject [skill:name] into description
        if s.get("skill") and f"[skill:" not in desc:
            desc = f"{desc} [skill:{s['skill']}]"
        # Defensive: normalize "tools" to a list of strings. Planners may emit
        # null, a string, or omit the field entirely — coerce all to [] rather
        # than letting (e.g.) list("yt-dlp") explode into single characters.
        raw_tools = s.get("tools")
        if isinstance(raw_tools, list):
            tools = [str(t) for t in raw_tools]
        else:
            tools = []
        plan.subtasks.append(Subtask(
            id=str(s["id"]),
            description=desc,
            pane=s["pane"],
            depends_on=[str(d) for d in s.get("depends_on", [])],
            mode=s.get("mode", "interactive"),
            tools=tools,
        ))

    errors = plan.validate(valid_panes=valid_panes)
    if errors:
        raise ValueError(f"Invalid plan: {'; '.join(errors)}")

    return plan


def _extract_json(text: str) -> str:
    """Extract JSON from LLM response, handling ```json blocks."""
    # Try fenced code block first (most reliable)
    m = re.search(r'```(?:json)?\s*(\{[\s\S]*?\})\s*```', text)
    if m:
        return m.group(1)
    # Find the outermost balanced JSON object by tracking braces
    start = text.find("{")
    if start == -1:
        raise ValueError(f"No JSON found in planner response:\n{text}")
    depth = 0
    in_string = False
    escape = False
    for i, ch in enumerate(text[start:], start):
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    # Fallback: greedy match (better than nothing)
    m = re.search(r'(\{[\s\S]*\})', text)
    if m:
        return m.group(1)
    raise ValueError(f"No JSON found in planner response:\n{text}")


def display_plan(plan: Plan) -> None:
    """Print the execution plan as a compact one-liner."""
    n = len(plan.subtasks)
    modes = sorted(set(s.mode for s in plan.subtasks))
    no_deps = [s.id for s in plan.subtasks if not s.depends_on]
    parallel = f" ({len(no_deps)} parallel)" if len(no_deps) > 1 else ""
    detail(f"{n} subtask{'s' if n != 1 else ''}{parallel}, {'+'.join(modes)}")
