# Agent Driver (clive-to-clive)

ENVIRONMENT: connected to a remote clive instance via SSH.
The remote clive runs in --quiet mode: telemetry on stderr, result on stdout.

PROTOCOL:
  Send task as plain text, press Enter, wait for output.
  Remote clive plans, executes, and prints the result.
  DONE: {"status": "success", "result": "..."} — structured completion
  DONE: {"status": "error", "reason": "..."} — structured failure
  Plain text output — unstructured result (still valid)

USAGE:
- Type task description and press Enter
- Wait for output to appear (may take 30-120 seconds)
- Read the result from the screen
- For structured output, look for DONE: JSON line
- For plain text, the last substantial block is the result

PATTERNS:
- Simple task: type description, wait for result
- Chained tasks: send first, read result, send second referencing result
- Check status: if output stops mid-task, the remote agent may be waiting for input

PITFALLS:
- Long tasks: remote clive may take minutes — don't send another command too soon
- SSH timeout: if connection drops, the remote tmux session persists — reconnect
- Buffering: output may arrive in chunks, wait for DONE: or screen stability
- Quiet mode: remote stderr goes to the SSH terminal — you'll see telemetry mixed in

COMPLETION: Use <cmd type="task_complete">summary</cmd> when done.
Include key results from the remote agent's output.
