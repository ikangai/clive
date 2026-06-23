"""Multi-clive runner path (spec §12): integrate the EXISTING clive-to-clive
comms — the Rooms system — do not reinvent it.

It launches the real Rooms broker (`--role broker`) and two member clives that
`--join room@lobby` (the discovered CLI surface), lets them coordinate, then
grades the WORLD result (the receiver wrote the token to disk) and captures the
room transcript so the check can assert the channel actually carried the message.

Bounded by hard timeouts and process-group teardown so it can never hang.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from dataclasses import asdict
from typing import Optional

from ..common import clive_invoke, config, paths, specs, spec_applier
from ..common.store import Blackboard
from ..checks import check_base, safety
from ..envs.base import EnvProvider, EnvHandle


def _sum_session_tokens(home: str) -> int:
    """Sum tokens across every entry in the (shared) session log — members and
    broker all append to home/.clive_session_log.jsonl."""
    path = os.path.join(home, ".clive_session_log.jsonl")
    if not os.path.exists(path):
        return 0
    total = 0
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    total += int(json.loads(line).get("tokens", 0) or 0)
                except Exception:
                    continue
    except OSError:
        return 0
    return total


def _spawn(argv: list[str], env: dict, logpath: str) -> tuple[subprocess.Popen, object]:
    fh = open(logpath, "w", encoding="utf-8")
    proc = subprocess.Popen(argv, env=env, stdout=fh, stderr=subprocess.STDOUT,
                            cwd=config.clive_entry()[0], start_new_session=True)
    return proc, fh


def _terminate(proc: Optional[subprocess.Popen]) -> None:
    """Tear down the whole process GROUP, not just the parent. start_new_session=
    True makes proc the group leader (pgid == proc.pid), so we signal the group
    even when the parent has already exited but spawned children (tmux, member
    clives) still live. Always reap to avoid zombies."""
    if proc is None:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            break          # group already gone
        except Exception:
            pass
        try:
            proc.wait(timeout=5 if sig == signal.SIGTERM else 3)
            return
        except Exception:
            continue       # still alive -> escalate to SIGKILL
    try:
        proc.wait(timeout=2)   # final reap
    except Exception:
        pass


def run_multi_clive(candidate_id: str, candidate_spec_path: str, scenario: dict,
                    model_entry: dict, *, partition: str, store: Blackboard,
                    provider: EnvProvider, run_id: str, evidence_dir: str) -> dict:
    cfg = config.load_config()
    budget_cfg = cfg.get("budget", {})
    max_tokens = int(budget_cfg.get("per_run_max_tokens", 8000))
    timeout_s = int(budget_cfg.get("per_run_timeout_s", 240))
    default_toolset = cfg.get("clive", {}).get("default_toolset", "minimal")
    model_name = model_entry.get("name", "model")

    _, clive_py = config.clive_entry()
    py = config.clive_python()
    lobby = f"factoryL{run_id[-6:]}"
    room = scenario.get("room", "factory-relay")

    handle: Optional[EnvHandle] = None
    procs: list[subprocess.Popen] = []
    files: list[object] = []
    try:
        handle = provider.provision(scenario, run_id)
        sock = os.path.join(handle.home, "lobby.sock")
        spec = specs.load_spec(candidate_spec_path)
        applied = spec_applier.apply_spec(spec, evidence_dir, default_toolset)

        base_env = dict(os.environ)
        clive_invoke._scrub_env(base_env)   # drop non-LLM host creds + dangerous flags
        base_env.update(handle.clive_env)
        base_env.update(applied.env)
        base_env.update(clive_invoke.panel_env(model_entry))
        base_env["CLIVE_KEEP_SESSION"] = "1"
        base_env["CLIVE_EXPERIMENTAL_SELFMOD"] = "0"   # never reach real clive source

        # --- broker -------------------------------------------------------
        broker_log = os.path.join(evidence_dir, "broker.log")
        bproc, bfh = _spawn([py, clive_py, "--role", "broker", "--name", lobby,
                             "--lobby-socket", sock, "--safe-mode"], base_env, broker_log)
        procs.append(bproc); files.append(bfh)
        # wait for the lobby socket to appear (bounded)
        for _ in range(40):
            if os.path.exists(sock):
                break
            if bproc.poll() is not None:
                break
            time.sleep(0.25)

        # --- members ------------------------------------------------------
        for member in scenario.get("members", []):
            mgoal = (member.get("goal") or "").format(workdir=handle.workdir,
                                                       home=handle.home)
            mlog = os.path.join(evidence_dir, f"member_{member['name']}.log")
            argv = [py, clive_py, "--name", member["name"], "--conversational",
                    "--join", f"{room}@{lobby}", "--lobby-socket", sock, "--safe-mode",
                    "--max-tokens", str(max_tokens)] + list(applied.flags) + [mgoal]
            mproc, mfh = _spawn(argv, base_env, mlog)
            procs.append(mproc); files.append(mfh)

        # --- wait for the WORLD result (relayed.txt) ----------------------
        deadline = time.time() + timeout_s
        produced = False
        while time.time() < deadline:
            if provider.read_file(handle, "relayed.txt") is not None:
                produced = True
                break
            time.sleep(1.0)
        timed_out = not produced
    finally:
        for p in procs:
            _terminate(p)
        for f in files:
            try:
                f.close()
            except Exception:
                pass

    # If provisioning failed, `handle` is None: record an error run and bail
    # (nothing to grade, nothing to tear down).
    if handle is None:
        store.add_run(run_id, candidate_id, scenario["id"], model_name, "error",
                      evidence_path=evidence_dir, partition=partition,
                      clive_claim="provision failed", duration_s=0.0)
        return {"run_id": run_id, "outcome": "error", "passed": False,
                "detail": "environment provisioning failed", "safety_flags": [],
                "evidence": evidence_dir}

    # --- assemble room transcript + grade --------------------------------
    room_transcript = ""
    for member in scenario.get("members", []):
        mlog = os.path.join(evidence_dir, f"member_{member['name']}.log")
        if os.path.exists(mlog):
            with open(mlog, "r", encoding="utf-8", errors="replace") as fh:
                room_transcript += f"\n# {member['name']}\n" + fh.read()
    blog = os.path.join(evidence_dir, "broker.log")
    if os.path.exists(blog):
        with open(blog, "r", encoding="utf-8", errors="replace") as fh:
            room_transcript += "\n# broker\n" + fh.read()

    # Fold the member clives' kept session artifacts (_log_*/_script_*) into the
    # safety scan — otherwise a destructive/out-of-scope command run by a member is
    # invisible to the battery on the relay path (the single-clive runner collects
    # these; the relay path must too). Member session dirs are recovered from the
    # `Session:` lines in the member logs, attributed by this run's unique workdir.
    from .runner import _collect_session_artifacts, _read_workdir_files
    session_dirs = clive_invoke.parse_session_dirs(room_transcript)
    art_text, _ = _collect_session_artifacts(session_dirs, 0.0, handle.workdir, evidence_dir)
    scan_text = room_transcript + art_text + _read_workdir_files(handle.workdir)

    goal = (scenario.get("goal") or "").format(workdir=handle.workdir, home=handle.home)
    ctx = check_base.CheckContext(
        provider=provider, handle=handle, scenario=scenario, goal=goal,
        workdir=handle.workdir, transcript_text=scan_text, timed_out=timed_out,
        max_tokens=max_tokens, extra={"room_transcript": room_transcript})

    check_abs = os.path.join(paths.FACTORY_ROOT, scenario["check"])
    try:
        acc = check_base.load_acceptance(check_abs)(ctx)
    except Exception as e:
        acc = check_base.CheckResult(False, f"acceptance check raised: {e}")
    flags = safety.run_safety_battery(ctx)
    outcome = ("blocked" if check_base.blocks_promotion(flags)
               else "pass" if acc.passed else "budget_exceeded" if timed_out else "fail")

    # Token accounting: all members share HOME=handle.home, so they append to one
    # ~/.clive_session_log.jsonl. Sum every entry's tokens (broker + N members) so
    # multi-clive spend reaches the BudgetGuard and the cost meter (it is the most
    # expensive run shape — silently counting it as 0 defeats the gain governor).
    tokens_used = _sum_session_tokens(handle.home)

    with open(os.path.join(evidence_dir, "check.json"), "w", encoding="utf-8") as fh:
        json.dump({"acceptance": asdict(acc),
                   "safety_flags": [asdict(f) for f in flags], "outcome": outcome}, fh,
                  indent=2)

    # check_json mirrors run_one's shape (top-level 'detail'), which the Proposer
    # reads for recent_failures.
    store.add_run(run_id, candidate_id, scenario["id"], model_name, outcome,
                  evidence_path=evidence_dir, budget_used=tokens_used, partition=partition,
                  clive_claim="multi-clive coordination",
                  check_json={"acceptance": asdict(acc), "detail": acc.detail},
                  duration_s=0.0)
    for f in flags:
        store.add_safety_flag(run_id, f.kind, f.detail, f.severity)
    from ..common import budget as budgetmod
    store.add_budget(f"run:{run_id}", tokens_used,
                     cost=budgetmod.price(model_entry.get("model", ""), tokens_used),
                     notes=f"{scenario['id']} (multi-clive) @ {model_name} -> {outcome}")

    try:
        provider.teardown(handle)
    except Exception:
        pass
    return {"run_id": run_id, "outcome": outcome, "passed": acc.passed,
            "detail": acc.detail, "tokens": tokens_used, "claim": "multi-clive coordination",
            "safety_flags": [asdict(f) for f in flags], "evidence": evidence_dir}
