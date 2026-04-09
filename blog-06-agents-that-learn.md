# Agents That Learn Their Tools

Most agents start from zero on every task. They receive a prompt, do the work, return a result, and forget everything. The next task begins with the same blank slate — no memory of what worked, what failed, or what was discovered along the way.

This is fine for isolated tasks. It's wasteful for sustained work on the same machine, with the same tools, in the same environment. A human who has SSH-ed into a server and spent thirty minutes figuring out its directory structure, its quirky Python path, its non-standard log locations — that human doesn't forget all of it before the next task. The knowledge accumulates.

clive's pane agents are the mechanism for this accumulation.

## One agent per pane

Each tmux pane in a clive session gets a persistent agent. Not a fresh LLM call, but a stateful object that lives for the duration of the session and accumulates experience across every subtask it executes.

The pane agent maintains three things:

**Memory** — a rolling list of what happened. Successes and failures, summarized in one line each. The last ten entries survive. When the agent starts a new subtask, these memories are injected into its context: "Previously on this pane, you extracted CSV data successfully using `mlr --csv filter`. You failed to parse the JSON log because the file used single quotes."

**Shortcuts** — learned command patterns. When a script-mode subtask succeeds on the first attempt, the agent extracts a reusable pattern and files it. The next time a similar task arrives, the shortcut is available in context. This isn't retrieval-augmented generation — it's simpler. A dictionary of task patterns to notes about what worked.

**Health metrics** — success rate, average turns, total tokens. These aren't just for monitoring. They feed the self-adaptation mechanism.

## Self-adaptation

The interesting behavior emerges from the health metrics. When a pane agent notices it's failing too often, it changes strategy.

The first trigger is consecutive failures. If the agent fails two or more subtasks in a row, it escalates from script mode to interactive mode. The reasoning: if scripts keep breaking on this pane, the environment is probably unusual enough that the agent needs to observe and adapt rather than generate blind scripts.

The second trigger is sustained low performance. If the success rate drops below 50%, the agent boosts its turn budget — from the default maximum to at least twelve turns per subtask. More turns means more opportunity to recover from mistakes, try alternative approaches, and work through unexpected obstacles.

These adaptations are sticky for the session. Once escalated, the agent stays in the more cautious mode. The cost goes up, but so does reliability.

## The shared brain

Pane agents work in parallel. While one agent processes data in a shell pane, another browses a website, and a third queries an API. They can't see each other's screens. But they can share knowledge.

The shared brain is a thread-safe message board that all pane agents can read and write. It supports three channels:

**Facts** — broadcast knowledge. When an agent discovers something useful ("the API requires an auth header," "the CSV uses semicolons as delimiters"), it posts a fact. All other agents see it in their next context refresh.

**Messages** — directed communication. One agent can send a message to another: "the file you need is at /tmp/clive/output.json." The recipient sees it before its next turn.

**Delegation** — work requests. An agent that discovers a sub-problem outside its domain can request that another agent's pane handle it.

The implementation is simple — lists protected by a lock, persisted to a JSONL scratchpad on disk. The executor reads the last five scratchpad entries during each turn loop and injects them as context. No vector database. No embedding search. Just recent shared notes.

## What this looks like in practice

Consider a task: "Download the quarterly report from the finance portal, extract the revenue figures, and create a chart."

The planner decomposes this into three subtasks across two panes. The browser agent navigates the portal and downloads the PDF. It posts a fact: "Report downloaded to /tmp/clive/q4_report.pdf, 15 pages, tables on pages 3-7." The shell agent, starting its extraction subtask, sees this fact in context before its first turn. It knows where the file is and which pages matter without having to search.

If the shell agent fails to parse the PDF — maybe `pdftotext` isn't installed — it remembers the failure. The next subtask on the same pane gets context: "Previous attempt to parse PDF failed because pdftotext is not available. Try python3 with PyPDF2 or tabula."

If the browser agent has handled this portal before in this session, it has a shortcut: "Finance portal login uses SSO redirect — wait for the callback before navigating." That knowledge persists across tasks without being re-learned.

## The limits

Pane agent memory is session-scoped. When the tmux session ends, the accumulated knowledge is gone. This is deliberate — the environment changes between sessions (files move, services restart, access changes), and stale memory is worse than no memory.

The shortcuts are pattern matches, not semantic understanding. They help with recurring mechanical tasks, not with novel problems. And the shared brain is a broadcast channel, not a conversation — agents don't negotiate or argue, they share observations.

These are the right limits for the current design. The pane agent is not a long-term knowledge base. It's working memory — the kind of context a developer holds in their head during a debugging session and releases when they close the terminal.

The difference is that clive's working memory is explicit, structured, and shared across parallel workers. A human debugging alone holds context in one head. A clive session holds it across every pane simultaneously, and new subtasks inherit what the previous ones learned.
