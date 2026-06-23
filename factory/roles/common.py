"""Role engine (spec §5). All roles are stateless `claude -p` workers: each
invocation assembles a context slice from the store, calls `claude -p` with the
role's prompt file, and writes the result back. No role retains state.

Blindness is structural: the proposer's context slice is assembled here and
deliberately excludes grader internals (checks/) and held-out scenarios.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import uuid
from typing import Optional

import yaml

from ..common import config, paths, scoring, specs
from ..common.store import Blackboard


# ---------------------------------------------------------------------------
# claude -p transport
# ---------------------------------------------------------------------------
def claude_p(prompt: str, *, timeout: int = 180) -> tuple[str, int, float]:
    """Call `claude -p` (print mode), prompt on stdin. Returns (text, tokens, cost).
    Tries JSON output for usage; falls back to plain text."""
    try:
        p = subprocess.run(["claude", "-p", "--output-format", "json"],
                           input=prompt, capture_output=True, text=True, timeout=timeout)
        if p.returncode == 0 and p.stdout.strip():
            try:
                obj = json.loads(p.stdout)
                text = obj.get("result", "") or obj.get("text", "")
                usage = obj.get("usage", {}) or {}
                tokens = int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))
                cost = float(obj.get("total_cost_usd", 0.0) or 0.0)
                if text:
                    return text, tokens, cost
            except json.JSONDecodeError:
                return p.stdout, 0, 0.0
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return f"[claude -p unavailable: {e}]", 0, 0.0
    # fallback: plain text
    try:
        p = subprocess.run(["claude", "-p"], input=prompt, capture_output=True,
                           text=True, timeout=timeout)
        return p.stdout, 0, 0.0
    except Exception as e:
        return f"[claude -p failed: {e}]", 0, 0.0


def _load_prompt(role: str) -> str:
    with open(os.path.join(paths.ROLES_DIR, role, "prompt.md"), "r", encoding="utf-8") as fh:
        return fh.read()


def _extract(text: str, langs=("yaml", "json", "")) -> str:
    """Pull a fenced code block (yaml/json) from an LLM reply, else the whole text.
    Uses a GREEDY body match so triple-backticks INSIDE the block (e.g. a ```bash
    mention inside a system_prompt value) don't truncate the extraction at the
    inner fence."""
    for lang in langs:
        m = re.search(rf"```{lang}[^\n]*\n(.*)```", text, re.DOTALL | re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return text.strip()


def _first_json_object(text: str) -> str | None:
    """Return the first balanced {...} JSON object in `text`, tracking string state
    so braces/backticks inside string values don't confuse it. Robust to a JSON
    payload whose values contain ``` fences or { } characters."""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _parse_obj(reply: str):
    """Best-effort parse of an LLM reply into a dict: balanced-brace JSON first
    (backtick-safe), then a fenced block, then YAML."""
    cand = _first_json_object(reply)
    if cand:
        try:
            return json.loads(cand)
        except json.JSONDecodeError:
            pass
    raw = _extract(reply, ("json", "yaml", ""))
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        try:
            return yaml.safe_load(raw)
        except Exception:
            return None


# ---------------------------------------------------------------------------
# Proposer (generation) — emits ONE bounded change to `open`. Blind.
# ---------------------------------------------------------------------------
def propose(store: Blackboard) -> Optional[str]:
    champ_row = store.get_champion()
    champ_path = champ_row["spec_path"] if champ_row else paths.CHAMPION_YAML
    champ = specs.load_spec(champ_path)

    # Context slice — champion spec (so the proposer knows the rules incl. what is
    # frozen), recent WORKING-set failures, and the history of changes tried.
    # Deliberately NO grader internals, NO held-out scenarios.
    failures = store.recent_failures(limit=12)
    # Held-out-derived signal is redacted from the tried-history: the proposer is
    # blind to the held-out set (scoring.proposer_safe_scores).
    tried = [{"id": c["id"], "change": c["change_summary"], "stage": c["stage"],
              "scores": scoring.proposer_safe_scores(json.loads(c["scores_json"] or "{}"))}
             for c in store.list_candidates()]

    ctx = {
        "champion_open": champ.get("open", {}),
        "champion_frozen_keys": list((champ.get("frozen", {}) or {}).keys()),
        "open_keys_you_may_change": specs.OPEN_KEYS,
        "recent_failures": [
            {"scenario": f.get("scenario_goal", ""), "outcome": f["outcome"],
             "detail": json.loads(f.get("check_json", "{}") or "{}").get("detail", "")}
            for f in failures],
        "changes_already_tried": tried,
    }
    prompt = _load_prompt("proposer") + "\n\n## CONTEXT (JSON)\n```json\n" \
        + json.dumps(ctx, indent=2, default=str) + "\n```\n"
    reply, tokens, cost = claude_p(prompt)
    store.add_budget("proposer", tokens, cost, notes="propose")

    patch = _parse_obj(reply)
    if not isinstance(patch, dict) or "open_key" not in patch or "new_value" not in patch:
        return None
    key = patch["open_key"]
    if key not in specs.OPEN_KEYS:
        return None

    # Construct the candidate from champion + the single field change. Frozen is
    # copied verbatim, so it is structurally impossible to mutate it.
    candidate = {"meta": {"version": (champ.get("meta", {}).get("version", 1)) + 1,
                          "parent": champ_row["id"] if champ_row else "champion"},
                 "open": dict(champ.get("open", {})),
                 "frozen": champ.get("frozen", {})}
    candidate["open"][key] = patch["new_value"]
    candidate["meta"]["hash"] = specs.compute_hash(candidate["open"], candidate["frozen"])

    res = specs.validate_candidate(candidate, champ,
                                   max_changed_open_keys=config.load_config()
                                   .get("spec", {}).get("max_changed_open_keys", 1))
    if not res.ok:
        return None

    cid = f"cand-{uuid.uuid4().hex[:10]}"
    spec_path = os.path.join(paths.CANDIDATES_DIR, f"{cid}.yaml")
    os.makedirs(paths.CANDIDATES_DIR, exist_ok=True)
    specs.dump_spec(candidate, spec_path)
    summary = patch.get("summary") or specs.change_summary(res.diff)
    store.add_candidate(cid, candidate["meta"]["parent"], spec_path,
                        change_summary=summary, diff=res.diff, stage="proposed")
    return cid


# ---------------------------------------------------------------------------
# Judge (measurement, supplementary) — annotates only; never sets pass/fail.
# ---------------------------------------------------------------------------
def judge(store: Blackboard, run_id: str) -> dict:
    run = store.get_run(run_id)
    if not run:
        return {}
    scenario = store.get_scenario(run["scenario_id"]) or {}
    transcript_path = os.path.join(run["evidence_path"], "transcript.txt")
    transcript = ""
    if os.path.exists(transcript_path):
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as fh:
            transcript = fh.read()[:16000]
    prompt = _load_prompt("judge") + (
        f"\n\n## SCENARIO GOAL\n{scenario.get('goal','')}\n"
        f"\n## DETERMINISTIC OUTCOME (already decided; do not override)\n{run['outcome']}\n"
        f"\n## RUN TRANSCRIPT (truncated)\n```\n{transcript}\n```\n")
    reply, tokens, cost = claude_p(prompt)
    store.add_budget("judge", tokens, cost, notes=run_id)
    flags = _parse_obj(reply)
    if not isinstance(flags, dict):
        flags = {"note": reply[:2000]}
    store.add_judge_note(run_id, flags)
    return flags


# ---------------------------------------------------------------------------
# Reporter (arbitration support) — deterministic signals + prose digest.
# ---------------------------------------------------------------------------
def report(store: Blackboard, candidate_id: str) -> str:
    champ = store.get_champion()
    champ_id = champ["id"] if champ else None
    cfg = config.load_config()
    promo = scoring.evaluate_promotion(store, candidate_id, champ_id, cfg)
    diverg = scoring.divergence_signal(store, candidate_id, champ_id)
    cand = store.get_candidate(candidate_id) or {}
    runs = store.runs_for_candidate(candidate_id)

    facts = {
        "candidate": candidate_id, "change": cand.get("change_summary", ""),
        "promotion_eligibility": promo, "divergence": diverg,
        "run_outcomes": [{"scenario": r["scenario_id"], "model": r["model"],
                          "outcome": r["outcome"], "partition": r["partition"]}
                         for r in runs],
        "safety_flags": store.safety_flags_for_candidate(candidate_id),
    }
    prompt = _load_prompt("reporter") + "\n\n## COMPUTED FACTS (authoritative)\n```json\n" \
        + json.dumps(facts, indent=2, default=str) + "\n```\n"
    reply, tokens, cost = claude_p(prompt)
    store.add_budget("reporter", tokens, cost, notes=candidate_id)

    digest = reply.strip() or "(no digest)"
    digest_path = os.path.join(paths.RUNS_DIR, f"{candidate_id}.digest.md")
    os.makedirs(paths.RUNS_DIR, exist_ok=True)
    with open(digest_path, "w", encoding="utf-8") as fh:
        fh.write(f"# Promotion digest — {candidate_id}\n\n"
                 f"```json\n{json.dumps(facts, indent=2, default=str)}\n```\n\n{digest}\n")
    # store digest reference on the candidate
    store.set_candidate_scores(candidate_id, {**promo["candidate_scores"],
                                              "digest_path": digest_path,
                                              "divergence": diverg})
    return digest_path


# ---------------------------------------------------------------------------
# Scenario Miner (corpus intake) — writes CANDIDATE scenarios to staging only.
# ---------------------------------------------------------------------------
def mine_scenarios(store: Blackboard, limit: int = 10) -> list[str]:
    log_path = os.path.expanduser("~/.clive_session_log.jsonl")
    lines: list[str] = []
    if os.path.exists(log_path):
        with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.read().strip().splitlines()[-limit:]
    prompt = _load_prompt("scenario-miner") + (
        "\n\n## RECENT PRODUCTION SESSIONS (JSONL)\n```\n" + "\n".join(lines) + "\n```\n")
    reply, tokens, cost = claude_p(prompt)
    store.add_budget("scenario-miner", tokens, cost, notes="mine")

    raw = _extract(reply, ("yaml", "json", ""))
    try:
        proposed = yaml.safe_load(raw)
    except Exception:
        return []
    if isinstance(proposed, dict):
        proposed = proposed.get("scenarios", [])
    if not isinstance(proposed, list):
        return []

    written: list[str] = []
    os.makedirs(paths.STAGING_DIR, exist_ok=True)
    for i, sc in enumerate(proposed):
        if not isinstance(sc, dict) or not sc.get("goal"):
            continue
        # Mined scenarios are CANDIDATES for operator vetting. Never enter the
        # corpus, and never the held-out partition, without human approval.
        sc.setdefault("id", f"mined-{uuid.uuid4().hex[:6]}")
        sc["source"] = "mined"
        sc["partition"] = "staging"   # not 'working'/'held-out' — vetting required
        sc["leakage_count"] = 0
        path = os.path.join(paths.STAGING_DIR, f"{sc['id']}.yaml")
        with open(path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(sc, fh, sort_keys=False, allow_unicode=True)
        written.append(path)
    return written


def synth_check(store: Blackboard, staged_id: str) -> Optional[str]:
    """Synthesize a deterministic acceptance check (Python) for a STAGED scenario.

    claude -p writes a `def acceptance(ctx) -> CheckResult` module from the
    scenario's goal + seed_files + check description. The result is saved for
    OPERATOR REVIEW and the staged scenario's `check` is pointed at it. The human
    reviews the generated code before promoting the scenario — the check is the
    product, so it is never trusted unread.
    """
    staged = os.path.join(paths.STAGING_DIR, f"{staged_id}.yaml")
    if not os.path.exists(staged):
        return None
    with open(staged, "r", encoding="utf-8") as fh:
        sc = yaml.safe_load(fh) or {}
    ctx = {"goal": sc.get("goal", ""), "seed_files": sc.get("seed_files", {}),
           "check_description": sc.get("check", ""), "class": sc.get("class", "single")}
    prompt = _load_prompt("check-synth") + "\n\n## SCENARIO\n```json\n" \
        + json.dumps(ctx, indent=2, default=str) + "\n```\n"
    reply, tokens, cost = claude_p(prompt)
    if store:
        store.add_budget("check-synth", tokens, cost, notes=staged_id)

    code = _extract(reply, ("python", ""))
    if "def acceptance" not in code:
        return None
    rel = f"checks/scenarios/{staged_id.replace('-', '_')}_check.py"
    path = os.path.join(paths.FACTORY_ROOT, rel)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(code if code.endswith("\n") else code + "\n")
    # Point the staged scenario at the generated check (operator reviews, then promotes).
    sc["check"] = rel
    with open(staged, "w", encoding="utf-8") as fh:
        yaml.safe_dump(sc, fh, sort_keys=False, allow_unicode=True)
    return path


def run_role(name: str, store: Blackboard, **kw):
    if name == "proposer":
        return propose(store)
    if name == "judge":
        return judge(store, kw["run_id"])
    if name == "reporter":
        return report(store, kw["candidate_id"])
    if name in ("scenario-miner", "scenario_miner", "miner"):
        return mine_scenarios(store, kw.get("limit", 10))
    raise ValueError(f"unknown role {name!r}")
