[A is sharper on locus specificity, file-level evidence, and the "forced now vs. forced later" framing — its sibling-through-shim observation (`execution/interactive_runner.py` importing `from executor`) is damning and concrete. B is sharper on tying the proposal to Clive's governing principle and to the headline 60-80% figure that justifies the entire cost stack — it identifies the *load-bearing* uncalibrated coefficient rather than a load-bearing fragility. The deeper move both point at is the same: stop treating Clive's hottest path as an opinion and make it measurable/structural before the experimental subsystems (rooms, lobby, evolution, Phase 2 speculation) compound on top of it. B's step is the one that earns the next six months of architectural work; A's step is hygiene that unblocks shipping. I take B's thesis and A's discipline.]

# Build the replay corpus under the WAIT/OBSERVE/DECIDE classifier — and version the classifier behind it.

**Thesis:** The next architectural step is to instrument `observation/observation.py` to capture every `classify()` decision with its post-hoc downstream action, ship a replay harness in `evals/observation/replay.py`, and refactor `ScreenClassifier` into a versioned strategy (`CLIVE_CLASSIFIER=v1|v2`) so the cost stack's headline 60-80% token reduction becomes a measured number rather than an asserted one. Everything else Clive's roadmap implies — Phase 2 speculation default-on, rooms, evolution, BYOLLM economics — compounds on this coefficient.

## Locus

`src/clive/observation/observation.py` (169 lines; the six-branch regex cascade where `exit != 0` and the `UNKNOWN` catch-all both unconditionally set `needs_llm=True`), `observation/byte_classifier.py` (the streaming counterpart, default-on since 2026-04-16), and three new files: `observation/capture.py`, `evals/observation/replay.py`, and a v2 classifier strategy alongside v1.

## Rationale tied to current state

1. **The headline cost number is unmeasured.** The 60-80% token reduction claim justifies per-pane model tiers, planner mode selection, and BYOLLM round-trip economics. It rests on regex branches that escalate every non-zero exit — `grep` finding nothing, `test`, `diff`, `git diff --quiet`. That is the architectural limit of a stateless regex table that cannot read intent. Until escalation precision is calibrated, every downstream "optimization" multiplies an uncalibrated coefficient.

2. **The streaming pivot has already raised the stakes.** Phase 1 (`CLIVE_STREAMING_OBS`, default-on) routes bytes continuously through the classifier; Phase 2 speculation (`speculative.py`, `CLIVE_SPECULATE=1`, default off) version-stamps and cancels in-flight LLM calls. A too-eager escalation now spawns a speculative main-model call that may be cancelled milliseconds later — pure waste. Phase 2 cannot reach default-on without an escalation-precision number, and the brief lists Phase 2 as the explicit next gate.

3. **The methodology is already proven in this repo.** `evals/harness/run_eval.py` takes `--layer`; baseline and phase1 reports exist under `evals/observation/`; the corpus shape exists in `evals/layer3/interactive_core/tasks.json`. The autoresearch finding (RESPONSE FORMAT = +37pp on shell evals) shows Clive's highest-leverage gains come from eval-driven tuning of cheap deterministic components, not new architecture. The classifier is the next such component, and it is upstream of everything that hits the wallet.

## Concrete change

1. `observation/capture.py`: every `classify()` call writes `{screen_tail, exit_code, event, needs_llm, downstream_action_taken}` to `~/.clive/captures/{session}.jsonl`. The post-hoc label is the ground truth — did the main model actually issue a new command, or did it `DONE:`/no-op?
2. `evals/observation/replay.py`: feed the JSONL corpus through any classifier version, emit a confusion matrix priced in tokens × per-tier cost (over-escalation) versus stuck-pane turns (under-escalation).
3. Refactor `ScreenClassifier` into a versioned strategy. Keep regex as `v1`. Add `v2` consuming command-class from `command_extract.py` (which already parses the bash blocks) plus a 16-bit screen-shape fingerprint (has-traceback, has-diff-output, has-prompt-marker, …) against a small learned table fit offline. Default `v1` until replay shows `v2` strictly dominates.

## Tradeoff accepted

I am accepting **persistent screen capture by default** — privacy and disk cost, mitigated by a tail-only window and per-session GC — in exchange for a labelled, replayable substrate underneath the entire cost stack. I am explicitly *not* swapping the regex for an LLM-judge classifier: that inverts the governing principle ("LLM where judgment is required, shell everywhere else") and reintroduces the cost the classifier was built to avoid. The v2 classifier stays deterministic; the *fitness function* becomes empirical.

This is not "add more evals" — evals are always-on. This is the single artifact that turns every future change to drivers, modes, planner heuristics, and Phase 2 speculation from an opinion into a measurement. The flat-import shim cleanup is real hygiene and will be forced eventually, but it is forced by packaging, not by the architecture; this is forced by the cost stack right now.
