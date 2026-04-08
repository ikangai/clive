# Agent Driver (clive-to-clive)

ENVIRONMENT: connected to a remote clive instance via SSH.
The remote clive runs in --quiet --json mode.

PROTOCOL (text-based, read from screen):
  You type → remote executes → screen shows result

  PROGRESS: step N of M — description   ← optional progress updates
  FILE: filename                          ← file available for transfer
  DONE: {"status": "success", "result": "...", "files": [...]}  ← completion
  DONE: {"status": "error", "reason": "..."}                    ← failure

USAGE:
- Type the task description as a single line, press Enter
- WAIT. Remote tasks may take 30-120 seconds. Use <cmd type="wait">30</cmd>
- Look for DONE: line — that's the result
- If DONE has "files", they can be transferred via scp

SENDING A TASK:
  <cmd type="shell" pane="agent">check disk usage on this server</cmd>
  <cmd type="wait">30</cmd>

READING THE RESULT:
  After DONE: appears, the executor parses it automatically.
  You can also read the screen for additional context.

CHAINING TASKS:
  Send first task → wait for DONE → send second task referencing result
  The remote shell maintains state between tasks (same session).

FILE TRANSFER:
  If remote writes files, they appear in FILE: lines.
  Use scp from a LOCAL shell pane to fetch them:
  <cmd type="shell" pane="shell">scp remote:/tmp/clive/result.csv /tmp/clive/</cmd>

PITFALLS:
- DONT send a new task before DONE: appears — remote is still working
- SSH timeout: remote tmux persists — reconnect if dropped
- Long output: use head/tail on remote, or transfer the file
- The remote agent has its OWN tools — you dont know what's installed

COMPLETION: Use <cmd type="task_complete">summary</cmd> when done.
Include key results from the remote agent's DONE: output.
