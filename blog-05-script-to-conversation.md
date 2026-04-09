# The Spectrum from Script to Conversation

Most agent frameworks have one execution mode. The agent receives a task, enters a loop — observe, reason, act, repeat — and exits when the task is done. Every task, regardless of complexity, passes through the same machinery.

This is correct but wasteful. "List all Python files in this directory" does not require the same cognitive apparatus as "debug why the API is returning 500 errors." The first is a known program. The second is an open-ended investigation. Treating them identically means either overpaying for simple tasks or under-equipping complex ones.

clive treats observation frequency as a per-task parameter. The planner examines each subtask and assigns a mode — a point on the spectrum from fully deterministic to fully adaptive.

## Script mode

At the deterministic end, the agent generates a shell script in one LLM call, writes it to a file, and executes it. No turn loop. No screen reading. No reasoning between steps. The agent watches the exit code and, if it's zero, moves on.

```
LLM call → generate script → bash script.sh → check exit code → done
```

If the script fails, a repair loop engages: the agent reads the error output, generates a fixed script, and tries again. But the happy path is a single LLM call for the entire subtask.

The cost difference is substantial. Interactive mode runs 5-8 LLM turns per subtask at roughly 5,000 tokens each. Script mode averages one turn at roughly 2,000 tokens. That's a 2.5x reduction — and for tasks where the script works on the first attempt, the gap is wider.

The planner's heuristic is simple: if the task can be expressed as a pipeline — extract, transform, filter, write — it's a script. If it requires reading unknown content, navigating an interactive application, or adapting to what appears on screen, it's interactive.

## Interactive mode

At the adaptive end, the agent enters the full turn loop. Read the screen, reason about what to do, type a command, wait for output, repeat. This is the mode that handles debugging, web browsing, multi-step exploration — anything where the next action depends on what happened in the previous one.

Interactive mode has its own efficiency mechanisms. After the first turn, only the screen diff is sent to the LLM — not the full screen. This cuts token usage by 60-80% on subsequent turns. The conversation history is capped at the most recent turns with a bookend strategy that preserves the initial context. The system prompt thins progressively — after turn one, the detailed driver prompt is stripped down to just the goal and command format.

These aren't optimizations bolted on afterward. They're consequences of treating the turn loop as an expensive resource that should be invoked precisely as much as the task requires.

## The fallback chain

The two modes aren't separate execution paths — they're a cascade. When a script-mode subtask fails its repair attempts, the executor doesn't give up. It re-queues the subtask in interactive mode with a boosted turn budget. The agent gets to read the error output, investigate, and work through the problem step by step.

This means the planner can be aggressive about assigning script mode. If it's wrong, the fallback catches it. The cost of a failed script attempt (one or two wasted LLM calls) is small compared to the savings across all the tasks where the script works.

## Below the spectrum: zero-LLM execution

There are tasks that don't need the LLM at all.

At the routing level, clive's three-tier intent resolver catches literal shell commands with a regex before any model is invoked. `ls -la /tmp` goes directly to the shell. `curl -s api.example.com | jq .data` goes directly to the shell. No classification, no planning, no generation. The cost of these tasks is zero tokens.

At the execution level, executable skills define deterministic step sequences in markdown:

```yaml
STEPS:
  - cmd: curl -s {URL}
    check: exit_code 0
    on_fail: skip
  - cmd: jq '.data[]' /tmp/clive/response.json
    check: valid_json
    on_fail: abort
```

The skill runner executes these mechanically — no LLM on the happy path. Each step runs, its check is evaluated (exit code, file existence, output content, JSON validity), and the runner proceeds or aborts. Parameters are injected from the skill invocation. The LLM only engages if a step fails and the skill requests LLM-assisted repair.

## The design principle

The governing idea across all of this is one line: **LLM where judgment is required, shell everywhere else.**

A regex can detect `grep -r "TODO" .` without a language model. A small classifier can route "check the weather" to the right tool without a full planner. A shell script can process a CSV file without turn-by-turn observation. An executable skill can run a deployment checklist without reasoning about each step.

The LLM is expensive, slow, and non-deterministic. It's also the only component that can handle genuine ambiguity — unexpected errors, novel situations, tasks that require understanding context. The architecture reserves it for those moments and handles everything else mechanically.

The result is a system where simple tasks are fast and cheap, complex tasks get the full cognitive weight of the model, and the boundary between them is drawn per-task by the planner rather than fixed in the architecture.
