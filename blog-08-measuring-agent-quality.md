# Measuring Agent Quality

There is a common pattern in agent development: build the system, run some examples, observe that they work, ship it. When something breaks later, investigate, patch the prompt, run the examples again, observe that they work again.

This is debugging, not measurement. The examples that get re-tested are the ones that broke. The ones that used to work are assumed to still work. Regressions hide until a user finds them.

clive has an eval framework because we got tired of this pattern. If you can't measure whether a change made the system better or worse, you're navigating by feel. The eval framework is the instrument panel.

## Four layers

Agent quality isn't one thing. A system can be good at executing shell commands and bad at planning multi-step tasks. It can be reliable on deterministic pipelines and unreliable on interactive browsing. The eval framework measures along four axes.

**Layer 1: End-to-end.** Complete tasks from natural language to final result. "Count the TODO comments in this directory and report the total." The planner plans, the executor executes, the summarizer summarizes. If the final answer is correct, the task passes. These tests catch system-level failures — broken coordination between components, misrouted tasks, garbled summaries.

**Layer 2: Tool proficiency.** Each tool surface tested in isolation. Shell tasks (file operations, data extraction, text processing), browser tasks (page fetching, link extraction, API calls), data tasks (CSV aggregation, JSON transformation). These are the workhorse evaluations — the ones that run most frequently and catch the most regressions.

**Layer 3: Script quality.** Script-mode-specific tasks that test the generate-execute-repair loop. Can the agent write a correct script for renaming files? For parsing JSON? For handling missing input gracefully? For fixing a syntax error in its own output? These tasks stress the deterministic execution path and its error recovery.

**Layer 4: Planning quality.** No execution — just plan generation. Given a complex task and a set of available tools, does the planner produce a sensible DAG? Are parallelizable subtasks actually parallel? Are dependencies correct? Is the mode assignment reasonable? These tests validate the plan JSON against structural expectations without running any commands.

Each layer has a different cost. Layer 4 is cheapest (one LLM call per test, no execution). Layer 2 is the workhorse (real execution, moderate token cost). Layer 1 is the most expensive and the most informative.

## What gets measured

Each eval task produces an `EvalResult` with six fields that matter:

**Passed/failed** is the binary. Did the agent produce the correct result? This is verified either deterministically (run a shell command, check exit code zero) or by an LLM verifier (ask a model whether the output satisfies the task).

**Turns used** versus **minimum turns**. The ratio is turn efficiency — how many turns the agent needed relative to the theoretical minimum. A task with a minimum of two turns that takes eight turns works, but wastefully. Turn efficiency surfaces prompt quality problems: unclear instructions that cause the agent to explore unnecessarily.

**Token count** — prompt tokens and completion tokens, separately. These feed directly into cost estimation via a pricing table keyed by model. You know not just whether the eval suite passed, but what it cost.

**False completion** — the agent claimed it was done, but the verification failed. This is the most dangerous failure mode: the agent is confidently wrong. Tracking it separately from plain failures catches prompt formulations that encourage premature task_complete signals.

**Error recovery** — the agent hit an error and recovered. This is the flip side of false completion: a positive signal that the agent's error handling works.

## Deterministic vs. LLM verification

The choice of verifier per task is a design decision with real consequences.

Deterministic verification runs a shell command and checks the exit code. `test -f /tmp/clive/output.csv && wc -l /tmp/clive/output.csv | grep -q "^42 "` — the file exists and has 42 lines. This is fast, cheap, and reproducible. Two runs of the same eval with the same agent output produce the same verdict.

LLM verification asks a model to judge the output. "Did the agent successfully summarize the webpage? Is the summary accurate?" This handles tasks where correctness isn't binary — summaries, analyses, exploratory results. But it's non-deterministic, slower, and costs tokens.

clive's eval framework caches LLM verifier results. The cache key is a hash of the task description, the agent output, and the verification prompt. If the same output is verified twice, the second check is a cache hit. This makes repeated eval runs cheaper without sacrificing accuracy on genuinely new outputs.

The general rule: use deterministic verification when you can express correctness as a shell predicate. Use LLM verification when you can't. Most Layer 2 and Layer 3 tasks are deterministic. Most Layer 1 tasks require LLM verification.

## Baseline regression

The most valuable feature of the eval framework is the simplest: baseline comparison.

After a passing eval run, the results are saved as a baseline JSON. On the next run — after a code change, a prompt tweak, a model update — the framework compares the current completion rate to the baseline. If it dropped, a regression warning fires. In CI, it's a hard failure.

This is what turns measurement into a safety net. You can change the planner prompt, the driver card, the execution logic, and know within minutes whether you broke something. Not by re-running the examples you remember, but by running all of them and comparing against the last known good state.

The alternative — the one most agent projects use — is finding out from users. The eval framework means finding out from math.
