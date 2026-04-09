# Knowing When It's Done

The glamorous parts of building a terminal agent are the planning, the reasoning, the multi-step execution. The part that actually determines whether any of it works is more mundane: knowing when a command has finished.

This sounds trivial. It isn't. A terminal is a continuous stream. There is no HTTP response boundary, no function return value, no structured "end of output" signal. The agent types `grep -r "TODO" .` and output starts scrolling. When does it stop? When the shell prompt reappears? When the screen stops changing? What if the command produces no output? What if it's still running but the network is slow?

Every agent system that operates in a terminal has to solve this problem. The solutions are worth examining because they reveal what's hard about building reliable agents on unstructured interfaces.

## Three strategies

clive uses three detection strategies, checked in priority order.

**Marker-based detection** is the most reliable. Before executing a command, the agent wraps it with a unique suffix:

```bash
grep -r "TODO" .; echo "EXIT:$? ___DONE_1_a3f2___"
```

The marker (`___DONE_1_a3f2___`) is a string that will never appear in normal output — it contains the subtask ID and a random nonce. When the marker appears on screen, the command is done. The exit code is captured in the same line.

This works for every command that runs to completion in a shell. It doesn't work for interactive applications that take over the terminal (like a text editor or a pager), but for the 90% case of shell commands, it's definitive.

**Prompt sentinel detection** is the fallback for when markers aren't in play. The shell prompt is configured with a distinctive prefix: `[AGENT_READY] $`. When this string appears on the last line of the screen, the shell is waiting for input — the previous command must have finished.

This catches cases where the marker approach doesn't apply — commands piped through programs that swallow the echo, or environments where the shell is unfamiliar.

**Idle timeout** is the final fallback. If the screen hasn't changed for two seconds and no marker is being watched, the command is assumed complete. This handles edge cases: commands that produce no output, commands whose output is identical to the previous screen, environments where neither markers nor prompt sentinels work.

The timeout is adaptive. Polling starts at 10 milliseconds — fast enough to detect rapid output changes — and backs off exponentially to 500 milliseconds when the screen is stable. When new output appears, polling resets to 10ms. This keeps detection responsive without burning CPU during long-running commands.

## Intervention detection

Some commands don't just produce output and finish. They ask questions.

```
Are you sure you want to continue? [y/N]
Password:
File already exists. Overwrite? (yes/no)
```

If the agent doesn't notice these, it waits for a completion signal that never comes — the command is blocked on input. The task times out, and the agent has no idea why.

clive scans the screen for eight intervention patterns: confirmation prompts, password requests, overwrite warnings, "press any key" messages, fatal errors, permission denials, and disk space errors. When detected, the completion function returns immediately with an intervention tag instead of the screen content. The agent sees "the command is asking for confirmation" rather than "the command hasn't finished yet."

This distinction matters for token efficiency. Without intervention detection, a stuck command consumes turns as the agent repeatedly reads an unchanged screen and tries to reason about why nothing is happening. With it, the agent can respond on the first turn — type "y" and press enter, or abort the command.

## Seeing only what changed

Completion detection solves the temporal problem — when is the output ready. Screen diffing solves the spatial problem — what part of the output matters.

After the first turn, sending the full screen to the LLM is wasteful. Most of it hasn't changed. The previous command's output is still there. The shell prompt is in the same place. Only a few lines at the bottom are new.

clive computes a diff between the previous screen capture and the current one. The logic has three branches:

If less than 50% of lines changed, a compact unified diff is sent — just the new and modified lines with one line of context. This typically reduces the screen content from 40-60 lines to 5-10.

If more than 50% changed (a major screen update, like switching applications), the full screen is sent. A diff would be harder to read than the complete state.

If nothing changed, a single line is sent: `[Screen unchanged]`. This costs three tokens instead of several hundred.

The savings compound. A ten-turn interactive subtask with full screen on every turn might consume 50,000 screen tokens. With diffing, the same subtask uses 10,000-15,000. The LLM reads less, reasons about the relevant change, and responds — without wading through a page of stale context.

## Progressive prompt thinning

The same principle applies to the system prompt. On the first turn, the agent needs the full context: the task description, the driver reference card, dependency results from upstream subtasks, the available tools. This might be 2,000 tokens of system prompt.

On turn two, the agent already knows all of that. The driver reference card hasn't changed. The dependency context is the same. Sending it again wastes tokens and — more subtly — wastes the model's attention.

After the first turn, clive strips the system prompt down to the essentials: the goal, the command format, and any new information. The driver prompt, the file context, the dependency results — all removed. The agent's conversation history carries the context forward.

For models that support prompt caching, this is less important — cached tokens are cheap. For models that don't, progressive thinning cuts the per-turn cost roughly in half.

## The pattern

Every one of these mechanisms follows the same pattern: don't send information the agent doesn't need.

Don't send the full screen when only three lines changed. Don't send the driver prompt on turn five. Don't call the LLM when the screen hasn't changed. Don't keep polling at high frequency when the command is in a quiet phase.

Terminal agents are token-intensive by nature — the observation channel is wide and noisy. The infrastructure that narrows it, that turns a firehose of screen state into a focused signal of what changed, is what makes the economics work. It's not visible in the architecture diagrams, but it's the reason a ten-step task costs dollars instead of tens of dollars.
