"""Three-tier intent routing: Agent addressing → Tier 0 (regex) → Tier 1 (classifier) → Tier 2 (planner).

Extracted from clive.py to keep the orchestrator thin. Each tier tries to produce
a Plan as cheaply as possible; unresolved tasks fall through to the next tier.

Public entry point:
    route_task(task, session_ctx, max_tokens) -> (Plan | None, str | None)
        Returns (plan, None) on success, or (None, early_return_msg) when the
        classifier produced an "answer"/"clarify"/"unavailable" response that
        should be returned to the user without executing a plan.
"""

from agents import parse_agent_addresses, resolve_agent
from config import find_config_schema, is_configured, run_setup
from models import ClassifierResult, Plan, Subtask
from output import detail, result, step
from planner import create_plan, display_plan
from session import ensure_agent_pane
from toolsets import PANES, find_category, normalize_tool_name


def _plan_from_agent_address(task, panes, tool_status):
    """Route clive@host tasks directly to an agent pane, bypassing planner."""
    agent_addresses = parse_agent_addresses(task)
    if not agent_addresses:
        return None

    agent_host, inner_task = agent_addresses[0]
    agent_config = resolve_agent(agent_host)
    first_pane = list(panes.values())[0]
    tmux_session = first_pane.pane.window.session

    ensure_agent_pane(tmux_session, panes, agent_host, agent_config)

    pane_name = f"agent-{agent_host}"
    plan = Plan(task=task, subtasks=[
        Subtask(id="1", description=inner_task, pane=pane_name, mode="interactive"),
    ])
    step("Routing")
    detail(f"Agent: clive@{agent_host}")
    display_plan(plan)

    tool_status[pane_name] = {
        "status": "ready",
        "app_type": "agent",
        "description": agent_config["description"],
    }
    return plan


def _plan_from_tier0(task, panes, is_direct_fn):
    """Tier 0: literal shell commands matched by regex — zero LLM calls."""
    if not is_direct_fn(task, len(panes)):
        return None
    first_pane = list(panes.keys())[0]
    plan = Plan(task=task, subtasks=[
        Subtask(id="1", description=task, pane=first_pane, mode="direct"),
    ])
    step("Routing")
    detail("Tier 0: direct command (regex)")
    display_plan(plan)
    return plan


def _activate_tool(tool_key, session_ctx, panes, first_pane):
    """Check if tool is ready as pane or command. Returns ClassifierResult, 'cancelled', or None."""
    if tool_key in panes:
        schema = find_config_schema(tool_key)
        if schema and not is_configured(schema):
            run_setup(tool_key, schema)
            if not is_configured(schema):
                return "cancelled"
            session_ctx["unconfigured"] = [
                t for t in session_ctx.get("unconfigured", []) if t != tool_key
            ]
        # Default to script — prefer programmatic access over TUI.
        # Executor falls back to interactive if script fails.
        return ClassifierResult(mode="script", tool=tool_key, pane=tool_key,
                                fallback_mode="interactive")
    if any(c["name"] == tool_key for c in session_ctx.get("available_cmds", [])):
        return ClassifierResult(mode="script", tool=tool_key, pane=first_pane)
    return None


def _resolve_unavailable(cr, session_ctx, panes, first_pane, expand_toolset_fn):
    """Reality-check the classifier's 'unavailable'/'unconfigured' verdict against session state.

    Returns (classifier_result, early_return_msg). early_return_msg is non-None
    when the caller should abort routing and return that message to the user.
    """
    tool_key = normalize_tool_name(cr.tool or "")

    # 1. Already in session?
    activated = _activate_tool(tool_key, session_ctx, panes, first_pane)
    if activated == "cancelled":
        return cr, f"{tool_key} setup cancelled."
    if activated:
        return activated, None

    # 2. Known category? → auto-expand, then try again
    cat = find_category(tool_key)
    if cat and cat not in session_ctx.get("categories", set()):
        step(f"Expanding toolset: +{cat}")
        expand_toolset_fn(cat, session_ctx)
        activated = _activate_tool(tool_key, session_ctx, panes, first_pane)
        if activated == "cancelled":
            return cr, f"{tool_key} setup cancelled."
        if activated:
            return activated, None

    # 3. Still unavailable after all checks
    step(f"Unavailable: {cr.tool}")
    msg = cr.message or f"{cr.tool} is not available"
    detail(msg)
    return cr, msg


def _reroute_to_pane(cr, panes, session_ctx, expand_toolset_fn):
    """If classifier says direct/script but tool is a known pane, use that pane."""
    if cr.mode not in ("direct", "script") or not cr.tool:
        return cr
    canonical = normalize_tool_name(cr.tool)
    if canonical not in PANES or canonical == "shell":
        return cr

    # Ensure pane is loaded
    if canonical not in panes:
        cat = find_category(canonical)
        if cat:
            step(f"Expanding toolset: +{cat}")
            expand_toolset_fn(cat, session_ctx)
    if canonical not in panes:
        return cr

    schema = find_config_schema(canonical)
    if schema and not is_configured(schema):
        run_setup(canonical, schema)
        if is_configured(schema):
            session_ctx["unconfigured"] = [
                t for t in session_ctx.get("unconfigured", []) if t != canonical
            ]
    if schema and not is_configured(schema):
        return cr

    # Preserve classifier mode — only reroute to the correct pane.
    # "direct" becomes "script" on non-shell panes (no raw cmd).
    rerouted_mode = cr.mode if cr.mode != "direct" else "script"
    return ClassifierResult(mode=rerouted_mode, tool=canonical, pane=canonical,
                            fallback_mode=cr.fallback_mode)


def _plan_from_tier1(task, session_ctx, panes, classify_fn, expand_toolset_fn):
    """Tier 1: fast classifier picks mode and tool. Returns (plan, early_return_msg)."""
    step("Classifying")
    cr = classify_fn(task, session_ctx)
    if cr is None:
        return None, None

    first_pane = list(panes.keys())[0]
    target_pane = cr.pane if cr.pane and cr.pane in panes else first_pane

    # Validate classifier against actual session state
    if cr.mode in ("unavailable", "unconfigured"):
        cr, early = _resolve_unavailable(cr, session_ctx, panes, first_pane, expand_toolset_fn)
        if early:
            return None, early
        target_pane = cr.pane if cr.pane and cr.pane in panes else first_pane

    if cr.mode == "answer":
        step("Answer")
        result(f"  {cr.message}" if cr.message else "  (no answer)")
        return None, cr.message or ""

    if cr.mode == "clarify":
        step("Clarification needed")
        detail(cr.message or "Could you be more specific?")
        return None, cr.message or "Could you be more specific?"

    cr = _reroute_to_pane(cr, panes, session_ctx, expand_toolset_fn)
    if cr.pane and cr.pane in panes:
        target_pane = cr.pane

    if cr.mode == "direct" and cr.cmd:
        plan = Plan(task=task, subtasks=[
            Subtask(id="1", description=cr.cmd, pane=target_pane, mode="direct"),
        ])
        detail(f"Tier 1: direct → {cr.tool}")
        display_plan(plan)
        return plan, None
    if cr.mode in ("script", "interactive", "streaming"):
        plan = Plan(task=task, subtasks=[
            Subtask(id="1", description=task, pane=target_pane, mode=cr.mode),
        ])
        detail(f"Tier 1: {cr.mode} → {cr.tool or 'shell'}")
        display_plan(plan)
        return plan, None

    # mode == "plan" falls through to Tier 2
    return None, None


def _plan_from_tier2(task, panes, tool_status, tools_summary, max_tokens, find_cached_fn):
    """Tier 2: full LLM planner — multi-step task decomposition."""
    step("Planning")
    cached = find_cached_fn(task, panes)
    if cached:
        detail("Tier 2: cached plan")
        display_plan(cached)
        return cached

    budget_hint = (
        f"\n\nToken budget: {max_tokens:,}. Approximate costs:"
        f"\n  - Script subtask: ~1,000 tokens (preferred)"
        f"\n  - Interactive subtask: ~5,000 tokens"
        f"\n  Plan within budget. Prefer script mode to stay within limits."
    )
    plan = create_plan(task, panes, tool_status, tools_summary=tools_summary + budget_hint)
    detail("Tier 2: full planner")
    display_plan(plan)
    return plan


def route_task(task, session_ctx, max_tokens, is_direct_fn, classify_fn,
               expand_toolset_fn, find_cached_fn):
    """Resolve a task through the three-tier routing pipeline.

    Returns (plan, early_return_msg):
        (Plan, None)   — execute this plan
        (None, str)    — stop routing and return this message to the user
        (None, None)   — should not happen; indicates a routing bug

    Dependency-injected callables keep this module free of circular imports
    with clive.py (which owns the Tier 0 regex, classifier, cache, and toolset
    expansion helpers).
    """
    panes = session_ctx["panes"]
    tool_status = session_ctx["tool_status"]
    tools_summary = session_ctx["tools_summary"]

    # Agent addressing: clive@host bypasses all tiers
    plan = _plan_from_agent_address(task, panes, tool_status)
    if plan is not None:
        return plan, None

    # Tier 0: regex-matched literal shell commands
    plan = _plan_from_tier0(task, panes, is_direct_fn)
    if plan is not None:
        return plan, None

    # Tier 1: fast classifier
    plan, early = _plan_from_tier1(task, session_ctx, panes, classify_fn, expand_toolset_fn)
    if early is not None:
        return None, early
    if plan is not None:
        return plan, None

    # Tier 2: full planner
    plan = _plan_from_tier2(task, panes, tool_status, tools_summary, max_tokens, find_cached_fn)
    return plan, None
