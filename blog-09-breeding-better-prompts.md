# Breeding Better Prompts

Prompt engineering is a craft practiced by hand. A developer writes a prompt, tests it against a few examples, tweaks a phrase, tests again. The feedback loop is human intuition. The optimization function is "does this feel better."

This works up to a point. It doesn't scale to prompts that operate across dozens of task types in environments the developer hasn't personally tested. And it has a fundamental limit: the developer's intuition about what makes a good prompt may not match what actually makes a good prompt — the mapping between prompt text and agent behavior is complex enough that human intuition is unreliable.

clive has an evolution system that treats prompts as organisms and evals as natural selection. The results have been surprising.

## The setup

clive's driver prompts are markdown reference cards — one per tool type. The shell driver tells the agent how to use bash effectively: exit code conventions, common patterns, pitfalls to avoid. The browser driver covers lynx and curl. The data driver covers jq and awk.

These drivers are the primary lever on agent performance for their tool type. A better shell driver means fewer turns, fewer failures, lower cost across every shell subtask. The question is: what does "better" mean, precisely?

The eval framework answers that question. Each driver has a corresponding set of eval tasks — deterministic, reproducible, automatically verified. Run the evals, get a number. The number is the fitness score.

## The loop

The evolution system runs in generations. Each generation:

1. **Evaluate the current best driver** to establish a baseline fitness score and pass rate.

2. **Generate variants.** An LLM mutates the current driver according to one of three strategies: make it more token-efficient (shorter, less redundant), make it more turn-efficient (add patterns for common operations, reduce unnecessary exploration), or make it more robust (improve error handling guidance, add edge case coverage).

3. **Evaluate each variant twice.** This is the conservative selection mechanism. A single eval run has variance — task execution is non-deterministic, LLM responses vary, timing matters. Running twice and taking the minimum score protects against flukes. A variant must be genuinely better, not just lucky.

4. **Select.** If the best variant of this generation beats the current best, it becomes the new current best. Its driver text and eval report are saved to a lineage directory. If no variant improves, the generation is discarded.

5. **Repeat** for N generations.

The baseline pass rate acts as a hard floor. If a variant's pass rate drops below the baseline — even if its turn efficiency or token efficiency improved — its fitness is zero. You cannot trade reliability for speed.

## The fitness function

The score is a weighted combination of three metrics:

```
fitness = 0.5 * pass_rate + 0.3 * turn_efficiency + 0.2 * token_efficiency
```

**Pass rate** (50% weight) is the fraction of eval tasks the agent completes successfully. This is the dominant term — a driver that passes fewer tasks cannot win regardless of efficiency gains.

**Turn efficiency** (30% weight) measures how many turns the agent used relative to the minimum possible. A task that needs two turns but takes six has a turn efficiency of 0.33. This rewards drivers that give the agent clearer instructions, reducing unnecessary exploration and retry loops.

**Token efficiency** (20% weight) measures cost. It's computed against a per-task budget of 10,000 tokens — if the agent averages 5,000 tokens per task, token efficiency is 0.5. This rewards concise drivers that don't bloat the context with information the agent doesn't use.

The weights reflect a priority order: correctness first, speed second, cost third. A driver that's 10% cheaper but 5% less reliable will not be selected.

## The mutation strategies

The three strategies cycle through variants, ensuring diversity in each generation:

**Token optimizer** — "Make instructions more concise. Remove redundant examples. Compress verbose descriptions into terse reference format." This strategy produces shorter drivers. Sometimes the compression helps — the model processes less noise. Sometimes it removes a crucial example and performance drops.

**Turn optimizer** — "Add patterns for common operations that the agent currently takes multiple turns to accomplish. Reduce exploration by being more prescriptive." This strategy produces more directive drivers. It tends to add command templates and explicit "do this, not that" instructions.

**Robustness optimizer** — "Improve guidance for error handling and edge cases. Add patterns for when commands fail unexpectedly." This strategy produces more defensive drivers. It adds error recovery patterns and alternative approaches for when the primary method fails.

Each strategy is a different theory about what will improve the driver. The eval framework is the arbiter.

## What emerges

The mutations that survive are not always what a human prompt engineer would choose.

In early experiments, the token optimizer sometimes removed sections that seemed important to a human reader — detailed explanations of how a tool works — and performance improved. The model didn't need the explanation. It already knew how the tool worked. The extra context was noise, not signal.

The turn optimizer sometimes added very specific command patterns — exact flag combinations for common operations — that a human would consider too narrow. But the agent used them, and the tasks completed in fewer turns.

The robustness optimizer sometimes added error patterns that seemed redundant — "if you see 'permission denied', try with a different path" — that caught real failure modes in the eval suite.

The point is not that the evolution system produces prompts that are unrecognizable or alien. They're still readable markdown reference cards. The point is that it makes changes a human wouldn't think to make, validates them against a rigorous fitness function, and keeps only the ones that actually help.

## The lineage

Every improved generation is saved: the driver text, the eval report, the fitness score. This creates a fossil record — you can trace how the driver evolved, which changes stuck, and which strategies produced the most improvement.

The lineage also serves as a rollback mechanism. If a later generation introduces a subtle regression that the eval suite doesn't catch (perhaps in a task type not covered by evals), you can revert to any previous generation.

## The broader point

The evolution system is a small piece of infrastructure — a few hundred lines of Python. But it represents a shift in how prompt development works.

The traditional model: a developer writes a prompt, tests it manually, ships it when it feels right. The prompt is a static artifact. Improvements require human attention.

The evolutionary model: a prompt is an organism. The eval framework is the environment. The fitness function is the selection pressure. The mutation strategies are the variation mechanism. Improvements happen automatically, validated by measurement, tracked through lineage.

The developer's role shifts from writing the prompt to writing the fitness function and the eval tasks. That's a better use of human judgment — defining what "good" means rather than guessing how to achieve it.

The code is at [github.com/ikangai/clive](https://github.com/ikangai/clive).
