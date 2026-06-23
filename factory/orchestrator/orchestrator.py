"""Orchestrator (spec §9): triggers, sequencing, concurrency + budget control.

The orchestrator sequences the stateless roles and the deterministic runner over
the blackboard. It NEVER promotes — promotion is a human action at the board. The
loops are deliberately gain-limited: the optimisation loop fires only on new
failure data, and evaluation is concurrency- and budget-capped.

CLI:  python3 -m factory.orchestrator.orchestrator <command> [args]
  init                 apply schema; register champion + scenarios
  baseline             evaluate the champion (working + held-out) for comparison
  propose              fire the optimisation trigger -> one candidate (claude -p)
  evaluate <cid>       run a candidate across working set x panel (concurrency-capped)
  round <cid>          evaluate (+held-out sample) -> reporter -> gate to awaiting_gate/rejected
  mine [--limit N]     scenario-miner -> staging (operator vetting)
  status               print a store summary
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from typing import Optional

import yaml

from ..common import config, paths, scoring
from ..common.budget import BudgetGuard
from ..common.store import Blackboard
from ..runner.runner import run_one
from . import triggers
from .concurrency import run_capped

CHAMPION_ID = "champion"


# ---------------------------------------------------------------------------
# init / registration
# ---------------------------------------------------------------------------
def _load_scenarios_from_disk() -> list[dict]:
    out = []
    for part, d in (("working", paths.WORKING_DIR), ("held-out", paths.HELD_OUT_DIR)):
        for path in sorted(glob.glob(os.path.join(d, "*.yaml"))):
            with open(path, "r", encoding="utf-8") as fh:
                sc = yaml.safe_load(fh) or {}
            sc["_path"] = path
            sc["partition"] = part
            out.append(sc)
    return out


def cmd_init(store: Blackboard) -> None:
    store.init_db()
    # Champion exists both as the reigning spec and as a pseudo-candidate so its
    # baseline runs satisfy the runs->candidates foreign key and feed scoring.
    if not store.get_candidate(CHAMPION_ID):
        store.add_candidate(CHAMPION_ID, "champion", paths.CHAMPION_YAML,
                            change_summary="(champion baseline)", stage="promoted")
    # Only seed the champion on a fresh store. Re-running init must NOT stamp a
    # fresh promoted_at on the baseline row, or get_champion() (ORDER BY
    # promoted_at DESC) would silently revert a human-promoted champion.
    if not store.get_champion():
        store.set_champion(CHAMPION_ID, paths.CHAMPION_YAML, scores={})
    n = 0
    for sc in _load_scenarios_from_disk():
        store.upsert_scenario(sc["id"], cls=sc.get("class", "single"),
                              partition=sc["partition"], source=sc.get("source", "seed"),
                              spec_path=sc["_path"], goal=sc.get("goal", ""),
                              snapshot=sc.get("snapshot", ""), check_path=sc.get("check", ""))
        n += 1
    print(f"init: schema applied, champion registered, {n} scenarios registered")


def _scenario_dict(store_row: dict) -> dict:
    """Reload the authoritative YAML for a scenario row (carries members/token/etc)."""
    with open(store_row["spec_path"], "r", encoding="utf-8") as fh:
        sc = yaml.safe_load(fh) or {}
    sc["id"] = store_row["id"]
    sc["partition"] = store_row["partition"]
    return sc


# ---------------------------------------------------------------------------
# evaluation loop
# ---------------------------------------------------------------------------
def _evaluate(store: Blackboard, candidate_id: str, spec_path: str, *,
              held_out_sample: int = 0, run_judge: bool = True,
              models: Optional[list[dict]] = None,
              scenario_ids: Optional[list[str]] = None,
              work_partition: str = "working",
              update_candidate: bool = True) -> dict:
    cfg = config.load_config()
    cap = int(cfg.get("concurrency", {}).get("cap", 2))
    guard = BudgetGuard()
    models = models if models is not None else config.panel_models()

    def _filter(rows):
        return [s for s in rows if (scenario_ids is None or s["id"] in scenario_ids)]

    working = _filter(store.list_scenarios(partition="working"))
    held = _filter(store.list_scenarios(partition="held-out"))[:held_out_sample]
    plan: list[tuple[dict, dict, str]] = []
    for s in working:
        for m in models:
            plan.append((s, m, work_partition))   # label working runs (e.g. 'holdout-model')
    for s in held:
        for m in models:
            plan.append((s, m, "held-out"))

    if candidate_id != CHAMPION_ID and update_candidate:
        store.set_stage(candidate_id, "evaluating")

    consecutive_errors = {"n": 0}

    def make_task(scenario_row, model_entry, partition):
        scen = _scenario_dict(scenario_row)
        return lambda: {**run_one(candidate_id, spec_path, scen, model_entry,
                                  partition=partition),
                        "partition": partition}

    def on_done(res: dict) -> bool:
        guard.add(int(res.get("tokens", 0)))
        if res.get("outcome") == "error":
            consecutive_errors["n"] += 1
        else:
            consecutive_errors["n"] = 0
        if guard.exceeded():
            print(f"[circuit-breaker] round token ceiling {guard.round_max_tokens} "
                  f"reached; halting evaluation", file=sys.stderr)
            return False
        if consecutive_errors["n"] >= 4:
            print("[circuit-breaker] 4 consecutive run errors; halting evaluation",
                  file=sys.stderr)
            return False
        return True

    tasks = [make_task(s, m, p) for (s, m, p) in plan]
    results = run_capped(tasks, cap, on_done=on_done)

    if run_judge:
        from ..roles.common import judge
        for r in results:
            rid = r.get("run_id")
            if rid:
                try:
                    judge(store, rid)
                except Exception as e:
                    print(f"[judge] {rid}: {e}", file=sys.stderr)

    scores = scoring.candidate_scores(store, candidate_id)
    if candidate_id != CHAMPION_ID and update_candidate:
        store.set_candidate_scores(candidate_id, scores)
        store.set_stage(candidate_id, "scored")
    return {"results": results, "scores": scores,
            "halted": guard.exceeded() or consecutive_errors["n"] >= 4}


def cmd_baseline(store: Blackboard, sample: Optional[int] = None,
                 scenario_ids: Optional[list[str]] = None,
                 models: Optional[list[dict]] = None) -> None:
    cfg = config.load_config()
    sample = cfg.get("held_out", {}).get("sample_size", 1) if sample is None else sample
    print(f"baseline: evaluating champion across working set + {sample} held-out …")
    out = _evaluate(store, CHAMPION_ID, paths.CHAMPION_YAML,
                    held_out_sample=sample, run_judge=False,
                    scenario_ids=scenario_ids, models=models)
    store.set_champion(CHAMPION_ID, paths.CHAMPION_YAML, scores=out["scores"])
    print("baseline scores:", json.dumps(out["scores"], indent=2, default=str))


def cmd_evaluate(store: Blackboard, candidate_id: str, run_judge: bool = True) -> None:
    cand = store.get_candidate(candidate_id)
    if not cand:
        print(f"no such candidate: {candidate_id}", file=sys.stderr)
        return
    out = _evaluate(store, candidate_id, cand["spec_path"], run_judge=run_judge)
    print(f"evaluate {candidate_id}:", json.dumps(out["scores"], indent=2, default=str))


# ---------------------------------------------------------------------------
# optimisation trigger
# ---------------------------------------------------------------------------
def cmd_propose(store: Blackboard) -> Optional[str]:
    cfg = config.load_config()
    fire, n, threshold = triggers.should_propose(store, cfg)
    if not fire:
        print(f"propose: trigger not met ({n}/{threshold} new failures since last "
              f"proposal). The gain governor holds.")
        return None
    print(f"propose: {n} >= {threshold} new failures — firing optimisation loop")
    from ..roles.common import propose
    cid = propose(store)
    print("proposed candidate:", cid or "(none — proposer produced no valid candidate)")
    return cid


# ---------------------------------------------------------------------------
# round = evaluate (+held-out) -> reporter -> gate (human queue)
# ---------------------------------------------------------------------------
def cmd_round(store: Blackboard, candidate_id: str, run_judge: bool = True,
              scenario_ids: Optional[list[str]] = None,
              models: Optional[list[dict]] = None) -> dict:
    cfg = config.load_config()
    sample = int(cfg.get("held_out", {}).get("sample_size", 1))
    leak_threshold = int(cfg.get("held_out", {}).get("leakage_threshold", 5))
    cand = store.get_candidate(candidate_id)
    if not cand:
        print(f"no such candidate: {candidate_id}", file=sys.stderr)
        return {}

    out = _evaluate(store, candidate_id, cand["spec_path"],
                    held_out_sample=sample, run_judge=run_judge,
                    scenario_ids=scenario_ids, models=models)
    promo = scoring.evaluate_promotion(store, candidate_id, CHAMPION_ID, cfg)

    # The held-out scenarios just influenced a promotion decision -> leakage++.
    _held = [s for s in store.list_scenarios(partition="held-out")
             if scenario_ids is None or s["id"] in scenario_ids]
    for s in _held[:sample]:
        store.increment_leakage(s["id"])
        row = store.get_scenario(s["id"])
        if row and row["leakage_count"] >= leak_threshold:
            store.retire_scenario(s["id"])
            print(f"[held-out] {s['id']} retired (leakage {row['leakage_count']} "
                  f">= {leak_threshold}); replace from vetted mined scenarios",
                  file=sys.stderr)

    from ..roles.common import report
    digest_path = report(store, candidate_id)

    if promo["eligible"]:
        store.set_stage(candidate_id, "awaiting_gate")
        print(f"round {candidate_id}: CLEARED the rule -> queued for the HUMAN gate. "
              f"Nothing promotes automatically (Phase 0).")
    else:
        store.set_stage(candidate_id, "rejected")
        reasons = [k for k in ("beats_working", "held_out_ok", "panel_ok", "safety_ok")
                   if not promo[k]]
        print(f"round {candidate_id}: did NOT clear ({', '.join(reasons)}) -> rejected")
    print("digest:", digest_path)
    return {"promotion": promo, "digest": digest_path}


def cmd_holdout_check(store: Blackboard, candidate_id: str) -> None:
    """Arbitration-cadence overfit probe (§5, §9): run the candidate across the
    working set under the HELD-OUT model(s) — never used during optimisation — and
    report the panel-vs-held-out-model gap. Runs are recorded with
    partition='holdout-model' so they never contaminate the panel scoreboard."""
    cand = store.get_candidate(candidate_id)
    if not cand:
        print(f"no such candidate: {candidate_id}", file=sys.stderr)
        return
    models = config.held_out_models()
    if not models:
        print("no held-out model configured in panel.yaml (held_out:)", file=sys.stderr)
        return
    # Record these runs as the held-out-MODEL partition from the start, and do NOT
    # touch the candidate's authoritative working scores or its stage (a queued
    # candidate must stay queued).
    _evaluate(store, candidate_id, cand["spec_path"], held_out_sample=0,
              run_judge=False, models=models, work_partition="holdout-model",
              update_candidate=False)
    sig = scoring.holdout_model_signal(store, candidate_id)
    print(f"holdout-check {candidate_id}:", json.dumps(sig, indent=2, default=str))
    if sig and sig.get("overfit_gap", 0) >= 0.34:
        print("[DIVERGENCE] panel >> held-out model — likely overfit to the panel",
              file=sys.stderr)


def cmd_mine(store: Blackboard, limit: int = 10) -> None:
    from ..roles.common import mine_scenarios
    paths_written = mine_scenarios(store, limit)
    print(f"mined {len(paths_written)} candidate scenarios into staging "
          f"(operator vetting required):")
    for p in paths_written:
        print(" ", p)


def cmd_status(store: Blackboard) -> None:
    champ = store.get_champion()
    print("=== clive-harness-factory status ===")
    print("champion:", champ["id"] if champ else "(none)",
          "scores:", champ["scores_json"] if champ else "{}")
    print("\ncandidates by stage:")
    for stage in ("proposed", "evaluating", "scored", "awaiting_gate", "promoted", "rejected"):
        rows = store.list_candidates(stage)
        if rows:
            print(f"  {stage}: {[r['id'] for r in rows]}")
    print("\nscenarios:")
    for s in store.list_scenarios():
        print(f"  {s['id']} [{s['partition']}/{s['class']}] leakage={s['leakage_count']}")
    bt = store.budget_totals()
    print(f"\nbudget: {bt['tokens']} tokens, ${bt['cost']:.4f}")
    flags = store.all_safety_flags()
    if flags:
        print(f"\nsafety flags: {len(flags)}")
        for f in flags[:10]:
            print(f"  [{f['severity']}] {f['kind']} ({f['candidate_id']}): {f['detail'][:80]}")
    # divergence for scored/queued candidates
    for c in store.list_candidates():
        if c["stage"] in ("scored", "awaiting_gate"):
            d = scoring.divergence_signal(store, c["id"], champ["id"] if champ else None)
            if d["alarm"]:
                print(f"\n[DIVERGENCE ALARM] {c['id']}: {', '.join(d['reasons'])}")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="factory.orchestrator")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    bp = sub.add_parser("baseline"); bp.add_argument("--sample", type=int, default=None)
    sub.add_parser("propose")
    ep = sub.add_parser("evaluate"); ep.add_argument("cid"); ep.add_argument("--no-judge", action="store_true")
    rp = sub.add_parser("round"); rp.add_argument("cid"); rp.add_argument("--no-judge", action="store_true")
    hp = sub.add_parser("holdout-check"); hp.add_argument("cid")
    mp = sub.add_parser("mine"); mp.add_argument("--limit", type=int, default=10)
    sub.add_parser("status")
    a = ap.parse_args(argv)

    with Blackboard() as store:
        if a.cmd == "init":
            cmd_init(store)
        elif a.cmd == "baseline":
            cmd_baseline(store, a.sample)
        elif a.cmd == "propose":
            cmd_propose(store)
        elif a.cmd == "evaluate":
            cmd_evaluate(store, a.cid, run_judge=not a.no_judge)
        elif a.cmd == "round":
            cmd_round(store, a.cid, run_judge=not a.no_judge)
        elif a.cmd == "holdout-check":
            cmd_holdout_check(store, a.cid)
        elif a.cmd == "mine":
            cmd_mine(store, a.limit)
        elif a.cmd == "status":
            cmd_status(store)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
