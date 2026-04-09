---
preferred_mode: interactive
use_interactive_when: always — this is a conversation with another agent
---
# Agent Driver (clive-to-clive peer conversation)

ENVIRONMENT: connected to a remote clive instance via SSH.
The remote clive runs in conversational mode (structured turn protocol).

PROTOCOL (read from pane screen):
  TURN: thinking    — remote is working. DO NOT type. Wait.
  TURN: waiting     — remote needs input. Read QUESTION/CONTEXT, respond.
  TURN: done        — task complete. Extract result from last CONTEXT line.
  TURN: failed      — task failed. Extract error from last CONTEXT line.

  CONTEXT: {...}    — structured JSON state from remote
  QUESTION: "..."   — question from remote (read before responding)
  PROGRESS: ...     — status update (informational only)
  FILE: filename    — file available for scp transfer

RULES:
- ONLY type when TURN: waiting appears. Never interrupt TURN: thinking.
- Read QUESTION and CONTEXT lines before composing your response.
- Keep responses concise and actionable — the remote clive parses your text.
- You are a peer, not a supervisor. The remote clive has its own judgment.
- If TURN: done result is insufficient, send a follow-up task on a new line.

SENDING THE INITIAL TASK:
  Type the task description as a single line, press Enter.
  sleep 10

RESPONDING TO QUESTIONS:
  Read the QUESTION line. Type your answer as a single line, press Enter.
  sleep 10

LEGACY PROTOCOL (backward compatibility):
  DONE: {"status": "success", "result": "..."}  — older clive instances
  DONE: {"status": "error", "reason": "..."}     — older error format

COMPLETION:
  When done: DONE: <summary from CONTEXT>
  Include key results. If FILE: lines appeared, note files for transfer.
