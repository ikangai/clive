# Move the WAIT/OBSERVE/DECIDE classifier from a hand-rolled regex table to a learned, replay-driven escalation policy.

**Thesis:** The next architectural step is to replace `ScreenClassifier` in `src/clive/observation/observation.py` with a versioned, evidence-driven policy whose escalation decision (`needs_llm`) is calibrated against a captured corpus of real pane screens, and to ship that corpus + replay harness as the source of truth for the cost-optimization stack. The locus is `observation/observation.py` (169 lines), `observation/byte_classifier.py`, and a new `evals/observation/replay.py` driving them.

## Why this, why now

The 60-80% token reduction claim — the headline figure justifying every downstream choice in Clive (per-pane tiers, planner mode selection, BYOLLM economics) — currently rests on a six-branch regex cascade where two branches (`exit != 0` and the catch-all `UNKNOWN`) unconditionally set `needs_llm=True` (`observation.py:92-100, 123-130`). Every shell command that exits non-zero — including `grep` finding nothing, `test`, `diff`, `[ -f x ]`, `git diff --quiet` — escalates to the main model. That is not a bug in the rules; it is the architectural limit of a stateless regex table that cannot read intent. Until this is fixed, every other "cost optimization" downstream is multiplied by an uncalibrated coefficient.

Three forces make this load-bearing right now:

1. **The streaming-observation pivot landed (Phase 1 default-on, 2026-04-16).** Bytes now flow through `byte_classifier.py` continuously, not just at quiesce. Phase 2 speculation (`speculative.py`, `CLIVE_SPECULATE=1`) version-stamps and cancels in-flight LLM calls. Both subsystems amplify whatever the classifier decides: a too-eager escalation now triggers a speculative main-model call that may immediately get cancelled — pure waste. Phase 2 cannot be turned on by default until escalation precision is measured.
2. **The eval harness is already structured for this.** `evals/observation/baseline-report.json` and `phase1-report.json` exist; `evals/harness/run_eval.py` already takes a `--layer` flag. There is a corpus shape (`evals/layer3/interactive_core/tasks.json`, `interactive_repair/tasks.json`) for capturing real pane scrollback. What's missing is a replay tool that feeds those captures through `ScreenClassifier.classify()` and reports precision/recall of `needs_llm` against a labelled ground truth — i.e., "for which screens did escalation actually change the action taken?"
3. **Driver-quality evals already proved this methodology works.** The 2026-04-09 driver-quality-evals plan plus the autoresearch finding (RESPONSE FORMAT section = +37pp on shell evals, per `memory/project_autoresearch_driver_findings.md`) demonstrates that Clive's highest-leverage gains come from measured, eval-driven tuning of cheap deterministic components, not from new architecture. The classifier is the next such component, and it is downstream of everything.

## The concrete change

1. Add `observation/capture.py`: every `classify()` call in interactive/streaming runs writes `{screen_tail, exit_code, event, needs_llm, downstream_action_taken}` to `~/.clive/captures/{session}.jsonl`. `downstream_action_taken` is the post-hoc label — did the main model actually issue a new command, or did it `DONE:`/no-op? That is the ground truth for whether escalation was warranted.
2. Build `evals/observation/replay.py` that runs the JSONL corpus through any classifier version and emits a confusion matrix (over-escalation cost in tokens × tier price; under-escalation cost in stuck panes).
3. Refactor `ScreenClassifier` into a versioned strategy: keep the regex table as `v1`, add `v2` that consults a small learned table (exit-code × command-class × screen-shape → escalate?) trained offline against the corpus. Command class comes from `command_extract.py` (which already parses the bash blocks); screen shape is a 16-bit fingerprint (has-traceback, has-diff-output, has-prompt-marker, …).
4. Wire `CLIVE_CLASSIFIER=v1|v2` and default to `v1` until replay shows `v2` strictly dominates on the corpus.

## Tradeoff accepted

I am accepting **persistent screen capture by default** (privacy/disk cost, mitigated by a tail-only window and per-session GC) in exchange for a measurable, replayable substrate under the entire cost stack. I am explicitly *not* doing what's tempting: I am not replacing the regex with an LLM-based classifier — that would invert the governing principle ("LLM where judgment is required, shell everywhere else") and re-introduce the cost it was built to avoid. The v2 classifier stays deterministic; the *fitness function* for tuning it becomes empirical.

What this is not: it is not "add more evals." Evals are an always-on activity. This is creating the single artifact — a labelled replay corpus of real pane screens — that turns every future change to drivers, modes, planner heuristics, and Phase 2 speculation from an opinion into a measurement. Without it, the next six months of Clive optimization compound on an unmeasured coefficient.
