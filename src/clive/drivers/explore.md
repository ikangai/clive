---
preferred_mode: interactive
use_interactive_when: always — exploration is inherently iterative
agent_model: fast
observation_model: fast
---
# Tool Exploration Driver

ENVIRONMENT: bash shell with PS1="[AGENT_READY] $ ". You are meeting a CLI tool you have never used.
WORKING DIR: /tmp/clive

GOAL: Learn what the tool does and how to use it by running safe, read-only probes. The pane history IS the curriculum. A driver will be synthesized from this session.

PROBE ORDER (try in sequence, skip what fails):
1. `<tool> --help`
2. `<tool> -h`
3. `man <tool> 2>&1 | head -80`
4. `tldr <tool>` (if installed)
5. `<tool> --version`
6. One or two read-only example invocations derived from the help text — only if clearly safe.

DO NOT:
- Run destructive probes (rm, dd, chmod, mv, mkfs, fdisk, format, init).
- Modify files outside /tmp/clive.
- Connect to networks unless the tool requires it AND it's read-only.
- Try sudo.
- Probe more than 8 commands total — exploration is bounded.
- Run a tool that prompts for credentials (aws, gh, gcloud, kubectl, psql, mysql, ssh) WITHOUT `--help` or `--version` — these will trap on a password prompt.
- Run a TUI tool (vim, less, top, lazygit, k9s) without `--help` — they will trap the terminal.

PATTERNS:
- If `--help` is unrecognized, try `-h`. If both fail, try `man <tool>`.
- If the tool drops into a TUI, you have miscalculated — DONE: immediately.
- If the tool wants config or credentials, STOP and DONE: report it. Don't set up credentials.

RESPONSE FORMAT:
- ALWAYS respond with a ```bash code block containing your next probe.
- After 5-8 probes or when you have enough to summarize, DONE: <one-line summary describing what kind of tool this is and its 2-3 most useful invocations>.

COMPLETION: DONE: <summary>. The summary line ends exploration and goes into the synthesized driver.
