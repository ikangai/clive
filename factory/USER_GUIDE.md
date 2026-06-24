# clive-harness-factory — Operator's Guide

A hands-on guide to running the factory and reading the board. If you want the
internals, see `ARCHITECTURE.md`; this is the "how do I drive it" doc.

---

## 1. The idea in 60 seconds

clive is a harness: you give it a goal, and an LLM drives a real shell to achieve
it. The **factory** tries to make that harness *better* — it proposes small changes
to clive's configuration, runs clive with each change against a battery of tasks,
and measures the result by **looking at the real shell afterwards** (did the file
get created? is the content right?) — never by asking the model "did it work?".

Three things happen, and you (the **operator**) own the last one:

1. **Generate** — an AI proposes one small change to clive's spec.
2. **Measure** — the change is run against tasks and graded on the real end-state.
3. **Decide** — *you* look at the board and choose whether to promote it.

Nothing promotes itself. Nothing touches a real system. The factory only ever runs
clive inside throwaway sandboxes.

---

## 2. Vocabulary

| Term | What it means |
|------|---------------|
| **champion** | The clive spec currently considered "best". Starts as `specs/champion.yaml`. |
| **candidate** | A proposed spec — the champion with **one** field changed. |
| **spec (`open` / `frozen`)** | clive's config, split in two. `open` is what may change (system prompt, toolset, observation/recovery policy, skills). `frozen` is the safety rules — **never** changed by the machine. |
| **scenario** | One test: a goal + seed files + a deterministic check. Lives in `scenarios/working/` or `scenarios/held-out/`. |
| **working set** | Scenarios the optimiser can see and tune against. |
| **held-out** | Reserved scenarios the proposer **never** sees — used to catch a candidate that only looks good on the working set. |
| **panel** | The model(s) that drive clive during a run (configured in `panel.yaml`). Yours is `claude-cli` = your Claude subscription via `claude -p`. |
| **the gate** | The single human decision point: promote a cleared candidate, or don't. |
| **run** | One (candidate, scenario, model) evaluation: provision a sandbox → run clive → grade the real shell → record → tear down. |

---

## 3. The loop

```
        you seed/grow the corpus              the world hands you failures
                   │                                      │
                   ▼                                      ▼
   ┌──────────┐  measure   ┌───────────┐  enough fails?  ┌──────────┐
   │ champion │──────────▶ │  failures │ ──────────────▶ │ propose  │  (AI, claude -p)
   └──────────┘  baseline  └───────────┘   (gain gov.)   └────┬─────┘
        ▲                                                     │ one bounded change
        │ you promote                                         ▼
   ┌────┴─────┐   gate    ┌──────────────┐   grade      ┌──────────┐
   │  BOARD   │◀───────── │ awaiting_gate │◀──────────── │ evaluate │
   │ (human)  │  promote  └──────────────┘  real shell  │  (round) │
   └──────────┘                                          └──────────┘
```

Read it as: **measure the champion → when reality surfaces enough failures, the AI
proposes one fix → the fix is graded on the real shell → if it beats the champion
with no regressions, it lands at the gate → you promote it.**

---

## 4. The commands

Run everything from the clive repo root. `F=factory/bin/factory` is a handy alias.

### Setup & inspect
| Command | What it does |
|---------|--------------|
| `$F init` | Create the database, register the champion + all scenarios. |
| `$F reset` | **Wipe** the database, run logs, and generated candidates, then re-init — a clean slate. |
| `$F status` | Text summary: champion, candidates by stage, scenarios, budget, any safety flags. |
| `$F board` | Start the operator's board at `http://127.0.0.1:8787` (Ctrl-C to stop). |

### The loop (drive it by hand)
| Command | What it does |
|---------|--------------|
| `$F baseline` | Run the **champion** across the working set (+ a held-out sample). This is the measurement that surfaces gaps. |
| `$F propose` | The AI proposer invents **one** candidate — **but only if the gain governor allows** (see below). |
| `$F evaluate <cid>` | Run a candidate across the working set × panel, score it. |
| `$F round <cid>` | Evaluate (+ held-out sample) → write a Reporter digest → decide eligibility → `awaiting_gate` or `rejected`. |
| `$F holdout-check <cid>` | Run a candidate under the **held-out model** to check it didn't just overfit the panel. |

Scope any of `baseline` / `evaluate` / `round` with `--scenario <id>` (repeatable)
and `--model <name>` so you don't run the whole battery.

### Build the corpus from real sessions
| Command | What it does |
|---------|--------------|
| `$F mine` | Read your real clive logs (`~/.clive_session_log.jsonl`) → propose candidate scenarios into `staging/`. |
| `$F staging` | List staged candidate scenarios + whether each has a runnable check yet. |
| `$F show-scenario <id>` | Print a staged scenario (goal, seed files, check spec). |
| `$F synth-check <id>` | AI writes a runnable acceptance check for it — **you review the code**. |
| `$F promote-scenario <id> --partition working\|held-out` | Move a vetted scenario into the corpus. |

### One-shot demos
| Command | What it does |
|---------|--------------|
| `$F smoke --reset` | The end-to-end Phase-0 test: champion baseline → seeded candidate → gate. |
| `$F demo` | The "watch the gate light up" walkthrough: champion fails a gap, a one-change candidate fixes it → lands in the queue for you to promote. |

### Reading the output

A run prints a progress line:

```
[1/1] ✓ gate-demo @ claude-cli → pass (678 tok)
 │      │            │            │       └ tokens clive used (estimate on claude-cli)
 │      │            │            └ outcome: pass | fail | error | budget_exceeded | blocked
 │      │            └ which panel model drove clive
 │      └ ✓ = passed the deterministic check, ✗ = failed it
 └ [done / total runs]
```

Then a scores block:

```
"working_set": 0.33   ← fraction of working-set runs that passed
"held_out": 1.0       ← same for the held-out sample
"n_working": 3        ← how many working runs are counted
"panel_rates": {...}  ← pass-rate per panel model
"panel_spread": 0.0   ← gap between the best and worst panel model (overfit signal)
"safety_tripped": false  ← did any high/critical safety flag fire?
```

**The gain governor.** If `propose` prints `trigger not met (2/3 new failures)`, that
is *by design*. The optimiser only fires after **N new champion failures** have
accumulated since the last proposal (default N=3, in `config.yaml`). It is the brake
that keeps the loop from running faster than reality hands you problems. For a quick
manual test, lower `triggers.new_failures_to_propose` to `1`, or run another failing
`baseline` to feed it.

---

## 5. The board, panel by panel

Open it with `$F board` → `http://127.0.0.1:8787`. It refreshes itself every few
seconds. It is **read-only except for one button**.

- **Header banner** — reminds you: *Phase 0 — promotion is a human action; nothing
  promotes automatically; no real credentials.* If that ever stops being true, you
  have a bug.

- **Divergence alarm** (loud red, only when firing) — the **Goodhart** warning. It
  lights up when a candidate's working-set score went **up** while the held-out
  score stayed flat (it may be gaming the proxy), or when the model panel disagrees
  too much. If it's red, be suspicious of whatever's in the queue.

- **Kanban** — every candidate as a card, in one of six columns:
  `proposed → evaluating → scored → awaiting gate → promoted / rejected`.
  A card shows the one change it makes and its scores (`ws` = working set, `ho` =
  held-out).

- **Promotion queue · the human gate** — candidates that **cleared the rule** and are
  waiting for you. Each card shows four green/red chips — **beats champion**,
  **held-out ok**, **panel ok**, **safety ok** — the Reporter's digest, and the one
  write action on the whole board: **Promote to champion**. Clicking it asks for
  your name + a rationale, records the decision, and makes that spec the champion.

- **Scoreboard** — the champion vs each challenger, broken out per panel model, with
  the working/held-out rates and the panel spread. This is where you see "0% → 100%".

- **Held-out leakage meter** — each held-out scenario has a shelf life: every time it
  influences a promotion it gets "used up" a little. The bar shows how depleted it
  is; past the threshold it retires and should be replaced with a freshly-mined one.

- **Cost burn** — tokens/$ spent this round against the configured ceiling.

- **Safety flags** — anything the negative safety checks tripped (out-of-scope write,
  grader access, unrequested port, destructive op, budget). High/critical ones block
  promotion.

The promotion rule the board enforces: a candidate may be promoted only if it **beats
the champion on the working set, doesn't regress on held-out, doesn't regress on any
panel model, and trips no safety flag** — and even then, only when *you* click.

---

## 6. A worked example (the loop you just ran)

```bash
$F reset                                  # clean slate; champion = champion.yaml
$F baseline --scenario gate-demo          # ✗ fail — the champion writes status.txt but
                                          #   leaves no .done receipt (a real gap)
$F baseline --scenario gate-demo          # ✗ — 2nd failure
$F baseline --scenario gate-demo          # ✗ — 3rd failure: the gain governor is now armed
$F propose                                # the blind proposer reads the failure detail and
                                          #   invents a fix → cand-xxxx (one system_prompt change)
$F round cand-xxxx --scenario gate-demo   # the fix is graded on the real shell → ✓ pass,
                                          #   beats the champion → "awaiting_gate"
$F board                                  # cand-xxxx sits in the Promotion queue → you Promote
$F baseline --scenario gate-demo          # the NEW champion now ✓ passes what it used to fail
```

That is the whole point demonstrated: a failure surfaced → an AI proposed a bounded
fix → the grader confirmed it on the actual files → you approved it → the harness
improved.

---

## 7. What it will and won't do

- It runs clive **only** inside throwaway sandboxes (default under `/tmp`), with
  `--safe-mode` and a scrubbed environment, so a run can't reach your real machine or
  your credentials. For untrusted candidates, switch `env.provider: docker` in
  `config.yaml` for hard, network-free isolation.
- It **never** promotes on its own and **never** changes the `frozen` safety block.
- Everything is files + one SQLite database, so you can inspect anything:
  `sqlite3 factory/store/blackboard.db .tables`.

---

## 8. Gotchas & FAQ

- **`propose` says "trigger not met (2/3)".** Not an error — the gain governor wants
  ≥3 new champion failures. Feed it more failing baselines, or lower
  `triggers.new_failures_to_propose` for a test.

- **I promoted on the board, but the next `baseline` still failed the gap.** Promotion
  is a **runtime** change: it repoints the champion at the promoted candidate's spec in
  the database. The seed file `specs/champion.yaml` is unchanged. So a `reset` (clean
  slate) reverts to the seed. To make a promotion **durable**, fold the change into
  `specs/champion.yaml` and commit it (that's how champion v2 got the `RESULT=`
  convention permanently).

- **`baseline` prints `[champion.yaml]` or `[cand-xxxx.yaml]`.** That's which spec the
  champion is currently running — handy for confirming a promotion took effect.

- **A run says `0 tok` but passed.** clive solved it in `direct` mode — a single shell
  command, no model-in-the-loop execution. That's clive being efficient, not a bug.

- **The candidate "ties" the champion and gets rejected.** Promotion requires *beating*
  the champion. If both pass, there's no improvement to promote — which is the correct,
  conservative outcome.

- **`reset` vs `init`.** `init` is safe/idempotent (registers without wiping). `reset`
  **destroys** the store, run logs, and generated candidates for a clean run.
