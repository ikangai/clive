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
import shutil
import sys
from typing import Optional

import yaml

from ..common import config, paths, scoring, specs
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
    progress = {"done": 0, "total": len(plan)}
    print(f"  {progress['total']} runs: {len(working)} working + {len(held)} held-out "
          f"× {len(models)} model(s) [{', '.join(m.get('name','?') for m in models)}]",
          file=sys.stderr)

    def make_task(scenario_row, model_entry, partition):
        scen = _scenario_dict(scenario_row)
        return lambda: {**run_one(candidate_id, spec_path, scen, model_entry,
                                  partition=partition),
                        "partition": partition,
                        "scenario": scenario_row["id"],
                        "model": model_entry.get("name", "?")}

    def on_done(res: dict) -> bool:
        progress["done"] += 1
        mark = "✓" if res.get("outcome") == "pass" else "✗"
        flags = res.get("safety_flags") or []
        sfx = f" ⚠{len(flags)}" if flags else ""
        print(f"  [{progress['done']}/{progress['total']}] {mark} "
              f"{res.get('scenario','?')} @ {res.get('model','?')} → "
              f"{res.get('outcome','?')} ({res.get('tokens',0)} tok){sfx}", file=sys.stderr)
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
    # Run the STORE's current champion spec, not the hardcoded seed — so a board
    # promotion (which repoints the champion at the promoted candidate's spec)
    # actually takes effect for the loop. The champion id stays CHAMPION_ID.
    champ = store.get_champion()
    champ_path = champ["spec_path"] if champ else paths.CHAMPION_YAML
    print(f"baseline: evaluating champion [{os.path.basename(champ_path)}] across "
          f"working set + {sample} held-out …")
    out = _evaluate(store, CHAMPION_ID, champ_path,
                    held_out_sample=sample, run_judge=False,
                    scenario_ids=scenario_ids, models=models)
    store.set_champion(CHAMPION_ID, champ_path, scores=out["scores"])
    print("baseline scores:", json.dumps(out["scores"], indent=2, default=str))


def _resolve_models(names: Optional[list[str]]) -> Optional[list[dict]]:
    """Map model NAMES (from --model) to their panel/held-out config entries."""
    if not names:
        return None
    pool = {m["name"]: m for m in (config.panel_models() + config.held_out_models())}
    out = [pool[n] for n in names if n in pool]
    return out or None


def cmd_evaluate(store: Blackboard, candidate_id: str, run_judge: bool = True,
                 scenario_ids: Optional[list[str]] = None,
                 models: Optional[list[dict]] = None) -> None:
    cand = store.get_candidate(candidate_id)
    if not cand:
        print(f"no such candidate: {candidate_id}", file=sys.stderr)
        return
    out = _evaluate(store, candidate_id, cand["spec_path"], run_judge=run_judge,
                    scenario_ids=scenario_ids, models=models)
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


def cmd_reset(keep_logs: bool = False) -> None:
    """Wipe the blackboard, generated candidate specs, and run logs, then re-init
    to a clean slate. Run OUTSIDE an open store connection (it deletes the db)."""
    removed = 0
    for suffix in ("", "-wal", "-shm"):
        p = paths.DB_PATH + suffix
        if os.path.exists(p):
            os.remove(p); removed += 1
    for f in glob.glob(os.path.join(paths.CANDIDATES_DIR, "*.yaml")):
        os.remove(f); removed += 1
    if not keep_logs and os.path.isdir(paths.RUNS_DIR):
        shutil.rmtree(paths.RUNS_DIR, ignore_errors=True)
        os.makedirs(paths.RUNS_DIR, exist_ok=True)
    print(f"reset: cleared {removed} store/spec files"
          + ("" if keep_logs else " + run logs") + " — re-initialising…")
    with Blackboard() as store:
        cmd_init(store)


DEMO_CONVENTION = (
    "\n\nOUTPUT CONVENTION: After you finish writing any report or result file for "
    "a task, also create an empty sibling completion-receipt file named "
    "<that filename>.done (for example, `touch status.txt.done`), so a task's "
    "completion is verifiable on disk.")


def _seed_demo_candidate(store: Blackboard) -> str:
    """A deterministic one-change candidate: champion + a completion-receipt
    convention in the system_prompt. Fixes the `gate-demo` scenario the current
    champion fails."""
    champ = specs.load_spec(paths.CHAMPION_YAML)
    cand = {"meta": {"version": champ["meta"].get("version", 1) + 1, "parent": "champion"},
            "open": dict(champ["open"]), "frozen": champ["frozen"]}
    cand["open"]["system_prompt"] = champ["open"].get("system_prompt", "") + DEMO_CONVENTION
    cand["meta"]["hash"] = specs.compute_hash(cand["open"], cand["frozen"])
    res = specs.validate_candidate(cand, champ, max_changed_open_keys=config.load_config()
                                   .get("spec", {}).get("max_changed_open_keys", 1))
    assert res.ok, f"demo candidate failed validation: {res.errors}"
    cid = "cand-demo"
    spec_path = os.path.join(paths.CANDIDATES_DIR, f"{cid}.yaml")
    os.makedirs(paths.CANDIDATES_DIR, exist_ok=True)
    specs.dump_spec(cand, spec_path)
    if not store.get_candidate(cid):
        store.add_candidate(cid, "champion", spec_path,
                            change_summary="system_prompt: teach clive to leave a "
                            "<file>.done completion receipt", diff=res.diff, stage="proposed")
    return cid


def cmd_demo(store: Blackboard) -> None:
    """Promotion-gate demo: the champion FAILS `gate-demo` (writes the report but
    leaves no completion receipt), a one-change candidate that teaches a <file>.done
    receipt convention PASSES, so it clears the rule and lands in the queue for a
    human Promote click."""
    only = ["gate-demo"]
    model = config.smoke_model()
    print("demo: champion fails 'gate-demo' (writes status.txt but no .done receipt); "
          "a one-change candidate that teaches a receipt convention fixes it.\n")
    print("[1/3] champion baseline on gate-demo …")
    cmd_baseline(store, sample=0, scenario_ids=only, models=[model])
    cid = _seed_demo_candidate(store)
    print(f"\n[2/3] candidate {cid}: + a <file>.done completion-receipt convention "
          f"(one system_prompt change)")
    print(f"\n[3/3] evaluation round for {cid} …")
    cmd_round(store, cid, run_judge=False, scenario_ids=only, models=[model])

    cand = store.get_candidate(cid)
    stage = cand["stage"] if cand else "?"
    print("\n" + "=" * 64)
    if stage == "awaiting_gate":
        print(f"✅ {cid} CLEARED the rule and is AWAITING YOUR PROMOTION at the gate.")
        print("   It beat the champion on the working set, no regressions, no safety flag.")
        print("   Open the board and click Promote (the one human lever):")
        print("     factory/bin/factory board   →  http://127.0.0.1:8787")
    else:
        print(f"{cid} ended at stage '{stage}' (expected awaiting_gate).")
    print("Nothing was promoted automatically. Promotion is your action at the board.")
    print("=" * 64)


def _validate_check(abs_path: str) -> tuple[bool, str]:
    try:
        with open(abs_path, "r", encoding="utf-8") as fh:
            src = fh.read()
        compile(src, abs_path, "exec")
    except SyntaxError as e:
        return False, f"syntax error: {e}"
    except OSError as e:
        return False, str(e)
    if "def acceptance" not in src:
        return False, "no acceptance(ctx) function"
    return True, ""


def _staged_check_ready(sc: dict) -> tuple[bool, str]:
    chk = sc.get("check", "") or ""
    if not (chk.endswith(".py")):
        return False, "needs synth-check"
    abs_path = os.path.join(paths.FACTORY_ROOT, chk)
    if not os.path.exists(abs_path):
        return False, "check file missing"
    ok, msg = _validate_check(abs_path)
    return ok, ("ready" if ok else msg)


def cmd_staging() -> None:
    """List mined candidate scenarios awaiting operator vetting."""
    files = sorted(glob.glob(os.path.join(paths.STAGING_DIR, "*.yaml")))
    if not files:
        print("staging is empty — run `factory mine` to propose candidate scenarios.")
        return
    print(f"staged candidate scenarios ({len(files)}) — vet -> synth-check -> promote:")
    for f in files:
        with open(f, "r", encoding="utf-8") as fh:
            sc = yaml.safe_load(fh) or {}
        ready, status = _staged_check_ready(sc)
        seeds = len(sc.get("seed_files") or {})
        print(f"  {sc.get('id','?'):30} [{sc.get('class','single')}] seeds={seeds}  "
              f"check: {'✓ ' + sc['check'] if ready else '✗ ' + status}")
        print(f"      goal: {(sc.get('goal','') or '')[:96]}")


def cmd_show_scenario(staged_id: str) -> None:
    p = os.path.join(paths.STAGING_DIR, f"{staged_id}.yaml")
    if not os.path.exists(p):
        print(f"no staged scenario {staged_id!r}", file=sys.stderr)
        return
    with open(p, "r", encoding="utf-8") as fh:
        print(fh.read())


def cmd_synth_check(store: Blackboard, staged_id: str) -> None:
    """Synthesize a runnable acceptance check for a staged scenario (operator reviews)."""
    from ..roles.common import synth_check
    path = synth_check(store, staged_id)
    if not path:
        print(f"synth-check failed for {staged_id!r} (no staged scenario, or the model "
              f"returned no acceptance()).", file=sys.stderr)
        return
    ok, msg = _validate_check(path)
    rel = os.path.relpath(path, paths.FACTORY_ROOT)
    print(f"synthesized check: {rel}  [{'compiles OK' if ok else 'NEEDS FIX: ' + msg}]")
    print("REVIEW it before promoting — the check is the product, never trusted unread. Then:")
    print(f"  factory/bin/factory promote-scenario {staged_id} --partition working")


def cmd_promote_scenario(store: Blackboard, staged_id: str, partition: str) -> None:
    """Operator action: move a vetted staged scenario into the corpus + register it."""
    if partition not in ("working", "held-out"):
        print("partition must be 'working' or 'held-out'", file=sys.stderr)
        return
    p = os.path.join(paths.STAGING_DIR, f"{staged_id}.yaml")
    if not os.path.exists(p):
        print(f"no staged scenario {staged_id!r}", file=sys.stderr)
        return
    with open(p, "r", encoding="utf-8") as fh:
        sc = yaml.safe_load(fh) or {}
    ready, status = _staged_check_ready(sc)
    if not ready:
        print(f"refusing to promote {staged_id!r}: {status}. Run "
              f"`factory synth-check {staged_id}`, review the check, then promote.",
              file=sys.stderr)
        return
    sc["partition"] = partition
    sc["source"] = "mined"
    sc.setdefault("leakage_count", 0)
    dest_dir = paths.WORKING_DIR if partition == "working" else paths.HELD_OUT_DIR
    dest = os.path.join(dest_dir, f"{staged_id}.yaml")
    with open(dest, "w", encoding="utf-8") as fh:
        yaml.safe_dump(sc, fh, sort_keys=False, allow_unicode=True)
    os.remove(p)
    store.upsert_scenario(staged_id, cls=sc.get("class", "single"), partition=partition,
                          source="mined", spec_path=dest, goal=sc.get("goal", ""),
                          snapshot=sc.get("snapshot", ""), check_path=sc.get("check", ""))
    print(f"promoted {staged_id!r} -> {partition} corpus "
          f"({os.path.relpath(dest, paths.FACTORY_ROOT)}) + registered in the store.")


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
    rs = sub.add_parser("reset"); rs.add_argument("--keep-logs", action="store_true")
    bp = sub.add_parser("baseline"); bp.add_argument("--sample", type=int, default=None)
    bp.add_argument("--scenario", action="append"); bp.add_argument("--model", action="append")
    sub.add_parser("propose")
    ep = sub.add_parser("evaluate"); ep.add_argument("cid"); ep.add_argument("--no-judge", action="store_true")
    ep.add_argument("--scenario", action="append"); ep.add_argument("--model", action="append")
    rp = sub.add_parser("round"); rp.add_argument("cid"); rp.add_argument("--no-judge", action="store_true")
    rp.add_argument("--scenario", action="append"); rp.add_argument("--model", action="append")
    hp = sub.add_parser("holdout-check"); hp.add_argument("cid")
    mp = sub.add_parser("mine"); mp.add_argument("--limit", type=int, default=10)
    sub.add_parser("staging")
    sp = sub.add_parser("show-scenario"); sp.add_argument("id")
    yp = sub.add_parser("synth-check"); yp.add_argument("id")
    pp = sub.add_parser("promote-scenario"); pp.add_argument("id")
    pp.add_argument("--partition", choices=["working", "held-out"], default="working")
    sub.add_parser("status")
    sub.add_parser("demo")
    a = ap.parse_args(argv)

    # reset deletes the db file, so it must run BEFORE a store connection is opened.
    if a.cmd == "reset":
        cmd_reset(keep_logs=a.keep_logs)
        return 0
    # demo = clean slate + the champion-fails / candidate-fixes walkthrough.
    if a.cmd == "demo":
        cmd_reset(keep_logs=False)
        with Blackboard() as store:
            cmd_demo(store)
        return 0

    with Blackboard() as store:
        if a.cmd == "init":
            cmd_init(store)
        elif a.cmd == "baseline":
            cmd_baseline(store, a.sample, scenario_ids=a.scenario,
                         models=_resolve_models(a.model))
        elif a.cmd == "propose":
            cmd_propose(store)
        elif a.cmd == "evaluate":
            cmd_evaluate(store, a.cid, run_judge=not a.no_judge,
                         scenario_ids=a.scenario, models=_resolve_models(a.model))
        elif a.cmd == "round":
            cmd_round(store, a.cid, run_judge=not a.no_judge,
                      scenario_ids=a.scenario, models=_resolve_models(a.model))
        elif a.cmd == "holdout-check":
            cmd_holdout_check(store, a.cid)
        elif a.cmd == "mine":
            cmd_mine(store, a.limit)
        elif a.cmd == "staging":
            cmd_staging()
        elif a.cmd == "show-scenario":
            cmd_show_scenario(a.id)
        elif a.cmd == "synth-check":
            cmd_synth_check(store, a.id)
        elif a.cmd == "promote-scenario":
            cmd_promote_scenario(store, a.id, a.partition)
        elif a.cmd == "status":
            cmd_status(store)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
