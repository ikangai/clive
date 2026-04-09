# tmux as Agent Runtime

The first post described the terminal as an agent interface. But "the terminal" is vague. The specific piece of infrastructure that makes clive possible is tmux — the terminal multiplexer that's been a staple of sysadmin workflows since 2007. It was built for humans managing remote servers. It turns out to be a nearly perfect runtime for autonomous agents.

This is not a coincidence. The properties that make tmux useful for humans — persistence, isolation, observability, multiplexing — are the same properties an agent runtime needs. tmux just happens to provide them all through a clean programmatic API that predates the agent era by fifteen years.

## The two primitives

An agent needs to do two things: observe the environment and act on it. In tmux, these map to two commands.

**`capture-pane`** is observation. It returns the current rendered content of a pane — what you'd see if you were looking at the terminal. Not the raw stdout stream with interleaved escape codes, not a scrollback buffer full of stale context, but the currently visible screen state. The `-J` flag joins wrapped lines, so a long path that wraps across two terminal lines becomes one string. The `-S` flag includes recent scrollback, so output that scrolled off the top of the screen is still visible.

```python
lines = pane.cmd("capture-pane", "-p", "-J", "-S-50").stdout
```

This is the agent's perception. Clean, rendered, bounded. The agent sees exactly what a human would see if they glanced at the terminal.

**`send-keys`** is action. It types keystrokes into a pane, exactly as if a human pressed those keys. The agent doesn't call an API or invoke a function — it types a command and presses enter.

```python
pane.send_keys("grep -r 'TODO' .", enter=True)
```

The symmetry matters. The agent acts the same way a human acts (keystrokes) and observes the same way a human observes (screen content). There is no translation layer between the agent's interface and the tool's interface. The tool doesn't know an agent is driving it.

## Panes as rooms

A clive session creates one tmux window per tool. The shell gets a window. The browser gets a window. The data processor gets a window. Each window has a pane, and each pane has its own independent process, its own shell state, its own working directory.

```
tmux session "clive"
  ├── window: shell     →  bash (local)
  ├── window: browser   →  lynx (local)
  ├── window: data      →  bash with jq/mlr (local)
  └── window: agent-gpu →  ssh → remote clive (remote)
```

The metaphor is rooms. The agent works in one room at a time, but all rooms are always running. A browser pane left showing a webpage keeps showing that webpage until the agent navigates away. A shell pane with a running process keeps running that process. The agent can switch rooms — read from one pane, act in another — without disturbing anything.

This maps directly to the DAG scheduler. Independent subtasks run in parallel on different panes. Each pane gets a thread, each thread holds a lock on its pane, and the agent in each thread works through its subtask independently. When a subtask finishes, the next subtask assigned to that pane inherits the environment — the working directory, the exported variables, the command history.

```python
# Per-pane locks prevent two subtasks from typing into the same pane
plan_locks: dict[str, threading.Lock] = {}
for pane_name in panes:
    plan_locks[pane_name] = threading.Lock()
```

A four-subtask plan with two panes might run subtasks 1 and 2 in parallel on separate panes, then subtask 3 on the first pane (inheriting subtask 1's environment), then subtask 4 after subtask 3 finishes. The pane is the execution environment. The lock ensures exclusive access. The DAG determines the order.

## The observation channel

The design of `capture-pane` solves several problems that agent builders often struggle with.

**Rendering is handled.** Terminal output contains ANSI escape codes — colors, cursor movement, line clearing. Raw stdout is noisy and hard to parse. `capture-pane` returns the rendered result after tmux has processed all escape codes. The agent sees "file.txt" in a directory listing, not `\033[1;34mfile.txt\033[0m`.

**Boundaries are natural.** The pane has a fixed size — typically 80x24 or whatever the terminal dimensions are. This is a natural context window. The agent reads one screenful of text, reasons about it, and acts. Long output scrolls off the top, but `-S-50` includes recent scrollback. The agent never has to deal with a megabyte of unbounded stdout.

**The snapshot is consistent.** `capture-pane` returns an atomic snapshot. There's no race between reading the first line and reading the last line — the entire screen state is captured in one call. A command that's actively producing output won't produce a garbled half-state.

**Environmental metadata is available.** Beyond screen content, tmux exposes pane dimensions, window names, and session state through `display-message`:

```python
width = pane.cmd("display-message", "-p", "#{pane_width}").stdout[0]
height = pane.cmd("display-message", "-p", "#{pane_height}").stdout[0]
```

The agent knows its terminal is 120 columns wide and can adjust output formatting accordingly.

## Persistence and recovery

tmux sessions survive disconnections. If you SSH into a server, start a tmux session, and your connection drops — the session keeps running. You reconnect and pick up where you left off.

For agents, this property is even more valuable. The clive process orchestrates execution, but the actual work happens in tmux panes. If the orchestrator crashes, the panes persist. A remote command keeps running. A browser keeps its state. The filesystem changes are already made.

This also means the workspace is inspectable at any time. While clive runs, you can open another terminal and attach to the session:

```bash
tmux attach -t clive
```

You see exactly what the agent sees. You can watch it type commands, see output scroll by, observe it switch between panes. This is the most useful debugging tool in the entire system — not logs, not traces, but watching the agent work in real time on the same terminal interface it's using.

For remote agent-to-agent connections, persistence means the inner clive's workspace survives even if the outer clive disconnects. SSH drops, the tmux session on the remote host keeps running. When the outer clive reconnects, the pane still has the latest output.

## Lazy creation

Not all panes need to exist at session start. The `clive@host` addressing system creates agent panes on demand — when the planner encounters an address, the session manager opens a new tmux window, starts the SSH connection, and registers the pane. The workspace grows organically as the task requires it.

```python
window = session.new_window(window_name="agent-devbox", attach=False)
pane = window.active_pane
pane.send_keys("ssh devbox 'python3 clive.py --conversational'", enter=True)
```

The `attach=False` parameter is the key detail. New windows are created in the background — the session's active window doesn't change, the user's view (if they're watching) isn't disrupted, and the orchestrator doesn't need to manage focus. The pane exists, the command starts, and the agent can read from it whenever it's ready.

## Hardening the runtime

Using tmux as an agent runtime raises practical problems that don't arise in casual human use.

**Socket isolation.** A developer who uses tmux daily has their own sessions — editor layouts, running servers, SSH connections. An agent creating sessions on the same tmux server would collide with these. clive runs on a dedicated tmux socket: `libtmux.Server(socket_name="clive")`. The agent's sessions are invisible to `tmux ls` and a stray `tmux kill-server` won't destroy the agent's workspace — or vice versa. The `-L clive` flag namespaces everything.

**Crash-resilient panes.** When a command in a tmux pane exits — whether it completes normally or crashes — the pane closes by default. For an agent, this is catastrophic. The exit code and final output vanish before the agent can read them. The fix is `remain-on-exit`: the pane stays open after its process exits, preserving the screen content. clive sets this per-session immediately after creation, scoped so it doesn't leak to other sessions on the same server.

**Setup without sleep.** The naive way to wait for a pane to be ready is `time.sleep(1.5)`. This is slow and unreliable — too short for remote connections, wasteful for local ones. clive uses the same marker pattern it uses for command completion:

```python
marker = f"___SETUP_{uuid.uuid4().hex[:4]}___"
pane.send_keys(f'cd {cwd} && mkdir -p {workdir}; echo {marker}', enter=True)
```

A polling loop checks all panes in parallel with exponential backoff — starting at 10ms, capping at 500ms, ceiling at 10 seconds. Local panes resolve in roughly 50 milliseconds instead of 1,500. Remote panes take as long as they need and no longer. The same technique that detects command completion also detects environment readiness.

These aren't features users see. They're the difference between a prototype and a system you can leave running overnight.

## What tmux isn't

tmux is not a container runtime. It doesn't provide filesystem isolation, resource limits, or security boundaries. A command running in a tmux pane has the full permissions of the user who started the session. The safety layers — command blocklists, restricted shells, SSH constraints — are separate concerns built on top.

tmux is not a message bus. Panes don't communicate with each other through tmux. The shared brain, the scratchpad, the file-based context passing between subtasks — these are clive constructs that use the filesystem, not tmux features.

What tmux is, precisely, is a multiplexed terminal emulator with a programmatic interface. It handles rendering, isolation between panes, persistence across disconnections, and atomic screen capture. Everything clive does — the turn loop, the DAG scheduler, the screen diffing, the completion detection, the agent-to-agent protocol — is built on those four capabilities.

The decision to build on tmux rather than building a custom agent runtime was the most consequential architectural choice in the project. It meant inheriting thirty years of terminal infrastructure — SSH compatibility, remote access, tool ecosystem, human observability — instead of reimplementing it. Every Unix CLI tool works in a tmux pane. Every SSH server is reachable from a tmux pane. Every developer already knows how to attach to a tmux session and see what's happening.

The runtime was always there. It was just waiting for an agent to inhabit it.
