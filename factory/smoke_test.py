"""End-to-end smoke test (spec §13.10).

Runs the full pipeline on the SINGLE example scenario across the configured panel
(the smoke model), ending at the HUMAN gate. No autonomous promotion. No real
credentials are wired by the factory — the candidate clive uses whatever provider
keys clive's own .env already holds.

  provision -> apply candidate spec -> run candidate clive under the panel model
  -> grade the real end-state -> record -> teardown -> reporter digest -> GATE.

By default it seeds a deterministic one-change candidate (cheap, hermetic). Use
--propose to exercise the real claude -p Proposer instead.

  python3 -m factory.smoke_test [--reset] [--propose] [--scenario ID] [--held-out ID]
"""
from __future__ import annotations

import argparse
import os
import sys

from .common import config, paths, specs
from .common.store import Blackboard
from .orchestrator import orchestrator as orch


def _seed_candidate(store: Blackboard) -> str:
    """A deterministic, valid one-bounded-change candidate: toolset minimal->standard."""
    champ = specs.load_spec(paths.CHAMPION_YAML)
    cand = {"meta": {"version": champ["meta"].get("version", 1) + 1, "parent": "champion"},
            "open": dict(champ["open"]), "frozen": champ["frozen"]}
    aff = dict(cand["open"].get("command_affordances", {}))
    aff["toolset"] = "standard"
    cand["open"]["command_affordances"] = aff
    cand["meta"]["hash"] = specs.compute_hash(cand["open"], cand["frozen"])

    res = specs.validate_candidate(cand, champ,
                                   max_changed_open_keys=config.load_config()
                                   .get("spec", {}).get("max_changed_open_keys", 1))
    assert res.ok, f"seeded candidate failed validation: {res.errors}"
    cid = "cand-smoke-0001"
    spec_path = os.path.join(paths.CANDIDATES_DIR, f"{cid}.yaml")
    os.makedirs(paths.CANDIDATES_DIR, exist_ok=True)
    specs.dump_spec(cand, spec_path)
    if not store.get_candidate(cid):
        store.add_candidate(cid, "champion", spec_path,
                            change_summary="command_affordances.toolset: minimal -> standard",
                            diff=res.diff, stage="proposed")
    return cid


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="factory.smoke_test")
    ap.add_argument("--reset", action="store_true", help="rebuild the blackboard from scratch")
    ap.add_argument("--propose", action="store_true", help="use the real claude -p Proposer")
    ap.add_argument("--scenario", default="hello-artifact")
    ap.add_argument("--held-out", dest="held_out", default="heldout-artifact")
    ap.add_argument("--with-held-out", action="store_true",
                    help="also sample the held-out scenario in the gate check "
                         "(default: single example scenario only, per §13.10)")
    a = ap.parse_args(argv)

    if a.reset and os.path.exists(paths.DB_PATH):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(paths.DB_PATH + suffix)
            except OSError:
                pass

    model = config.smoke_model()
    only = [a.scenario] + ([a.held_out] if a.with_held_out else [])
    print("=" * 70)
    print("clive-harness-factory — Phase 0 smoke test")
    print(f"  scenario      : {a.scenario}" +
          (f"  (+ held-out {a.held_out})" if a.with_held_out else "  (single example scenario)"))
    print(f"  smoke model   : {model['name']} ({model['provider']}/{model.get('model')})")
    print(f"  env provider  : {config.load_config().get('env', {}).get('provider')}")
    print(f"  candidate     : {'real Proposer (claude -p)' if a.propose else 'seeded one-change'}")
    print("=" * 70)

    with Blackboard() as store:
        # 1. init / register (idempotent: IF NOT EXISTS + upserts)
        orch.cmd_init(store)

        # 2. champion baseline on the single scenario + held-out sample
        print("\n[1/4] champion baseline …")
        orch.cmd_baseline(store, sample=1, scenario_ids=only, models=[model])

        # 3. candidate
        print("\n[2/4] producing a candidate …")
        if a.propose:
            from .roles.common import propose
            cid = propose(store)
            if not cid:
                print("  proposer produced no valid candidate; falling back to seed")
                cid = _seed_candidate(store)
        else:
            cid = _seed_candidate(store)
        print("  candidate:", cid)

        # 4. evaluation round -> reporter -> GATE (no autonomous promotion)
        print(f"\n[3/4] evaluation round for {cid} (concurrency + budget capped) …")
        result = orch.cmd_round(store, cid, run_judge=False, scenario_ids=only,
                                models=[model])

        print("\n[4/4] status:")
        orch.cmd_status(store)

        cand = store.get_candidate(cid)
        stage = cand["stage"] if cand else "?"
        print("\n" + "=" * 70)
        print(f"SMOKE RESULT: candidate {cid} ended at stage '{stage}'.")
        assert stage in ("awaiting_gate", "rejected"), \
            f"candidate must end at the gate, not '{stage}' (nothing auto-promotes)"
        assert stage != "promoted", "INVARIANT VIOLATED: autonomous promotion occurred"
        if stage == "awaiting_gate":
            print("It CLEARED the rule and is queued for the HUMAN gate.")
        else:
            print("It did NOT clear the rule and was rejected (still ended at the gate).")
        print("Nothing was promoted automatically. No real credentials were wired.")
        print("Open the board to act:  python3 -m factory.dashboard.server")
        print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
