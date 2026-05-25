"""Shared runtime primitives for the execution layer.

Leaf module — does NOT import from executor, interactive_runner,
script_runner, dag_scheduler, or completion. All those modules import
from here, breaking what was previously a fragile circular dependency.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import threading

log = logging.getLogger(__name__)

# ─── Per-pane locks: only one subtask can use a pane at a time ───────────────
_pane_locks: dict[str, threading.Lock] = {}

# ─── Global cancellation event — set by signal handler to abort all workers ──
_cancel_event = threading.Event()


def cancel():
    """Signal all workers to stop."""
    _cancel_event.set()


def is_cancelled() -> bool:
    """Check if cancellation has been requested."""
    return _cancel_event.is_set()


def reset_cancel():
    """Reset cancellation state for a new run."""
    _cancel_event.clear()


# ─── Event emission ─────────────────────────────────────────────────────────

def _emit(on_event, *args):
    """Call event callback if provided."""
    if on_event:
        try:
            on_event(*args)
        except Exception:
            log.debug("on_event callback failed for %s", args[0] if args else "?", exc_info=True)


# ─── Command Safety ─────────────────────────────────────────────────────────

# Raw-text patterns: things that don't survive shlex tokenization or that
# we want to catch even when embedded in a larger expression.
BLOCKED_COMMANDS = [
    re.compile(r':\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:'),  # fork bomb canonical form
    re.compile(r'>\s*/dev/sd[a-z]'),                          # redirect to raw disk
    re.compile(r'\beval\s+"?\$\(.*base64'),                   # eval base64 stub
    # `while true; do ...; done` and the `while :; do ...; done` variant
    # (`:` is the bash null command — equally infinite). See Bug H10.
    re.compile(r'\bwhile\s+(?:true|:)\s*;\s*do\b'),
    # Download-and-execute pipelines — the executable arm of the discovery
    # prompt-injection chain (gh#41 debug Bug 1). The pipe-to-shell shape
    # is the canonical sign; ``jq``, ``head``, ``less`` etc. don't trigger.
    re.compile(r'\b(?:curl|wget|fetch)\b[^|]*\|\s*(?:bash|sh|zsh|dash|ksh)\b'),
    # eval / source / exec of a network fetch — same threat without the pipe.
    re.compile(r'\b(?:eval|source|\.|exec)\s+["\']?\$\(\s*(?:curl|wget|fetch)\b'),
    # base64 (or xxd) decode piped into a shell — obfuscated curl|bash.
    re.compile(r'\b(?:base64|xxd|openssl\s+(?:base64|enc))\b[^|]*\|\s*(?:bash|sh|zsh|dash|ksh)\b'),
]

# argv[0] names that are dangerous regardless of args. Match the actual
# command word (first token, optionally preceded by `sudo`), not the
# substring — `echo shutdown` and `grep shutdown /var/log/...` are benign.
# See Bug H10 false-positive analysis.
_DANGEROUS_COMMANDS = frozenset({
    "shutdown", "reboot", "halt", "poweroff",
})


def _check_command_safety(command: str) -> str | None:
    """Check command against blocklist. Returns violation or None."""
    # 1) Raw-text patterns (fork bomb, while-true, dd-to-disk redirects, etc).
    for pattern in BLOCKED_COMMANDS:
        if pattern.search(command):
            return f"Blocked dangerous command: {command[:80]}"

    # 2) Structured per-command checks. Run against each segment of a shell
    #    sequence (`a && b`, `a || b`, `a; b`, `a | b`) so `cd / && rm -rf .`
    #    is inspected as `rm -rf .` after the `&&`. shlex tokenization handles
    #    quoting and trailing `# comment` correctly.
    for segment in _split_shell_segments(command):
        violation = _check_segment(segment)
        if violation:
            return violation
    return None


_SHELL_SEPARATORS = re.compile(r'\s*(?:&&|\|\||;|\|(?!\|))\s*')


def _split_shell_segments(command: str) -> list[str]:
    """Split on `&&`, `||`, `;`, `|` outside of quotes. Best-effort: shlex
    tokenizes first to respect quoting, then we reassemble around separator
    tokens. Falls back to a single segment if tokenization fails."""
    try:
        tokens = shlex.split(command, comments=True, posix=True)
    except ValueError:
        return [command]
    segments: list[list[str]] = [[]]
    for tok in tokens:
        if tok in ("&&", "||", ";", "|"):
            segments.append([])
        else:
            segments[-1].append(tok)
    # Reassemble each segment back into a string for re-tokenization in
    # _check_segment (so quoting decisions remain consistent).
    return [shlex.join(s) for s in segments if s]


# POSIX identifier — env-var names start with letter/underscore, then
# alnum+underscore. Used by ``_strip_sudo_and_env`` to recognise a real
# ``VAR=value`` prefix vs an unrelated token that happens to contain ``=``.
# Previously the stripper used ``name.replace('_','').isalnum()``, which
# returned False for all-underscore names (``_``, ``__``) — those are valid
# POSIX identifiers, so the stripper failed to remove them and the bypassed
# ``_=x <banned-cmd>`` slipped past the safety check (gh#41 debug Bug 13).
_VALID_ENV_VAR_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _strip_sudo_and_env(tokens: list[str]) -> list[str]:
    """Strip leading ``sudo`` and POSIX ``VAR=value`` env-var assignments.

    Returns the tail starting at the real command word so the safety
    blocklist evaluates the right token. Shared with
    ``discovery.explorer._check_exploration_safety``.
    """
    while tokens:
        head = tokens[0]
        if head == "sudo":
            tokens = tokens[1:]
            continue
        if "=" in head and len(tokens) > 1:
            name = head.split("=", 1)[0]
            if _VALID_ENV_VAR_NAME.match(name):
                tokens = tokens[1:]
                continue
        break
    return tokens


def _check_segment(segment: str) -> str | None:
    try:
        tokens = shlex.split(segment, comments=True)
    except ValueError:
        return None

    if not tokens:
        return None

    tokens = _strip_sudo_and_env(tokens)

    if not tokens:
        return None

    cmd, args = tokens[0], tokens[1:]

    # rm with -r/-R: any flag combo containing r or R, any arg pointing at
    # / or a home dir. Catches `rm -fr /`, `rm -rfv /`, `rm -rf / && cmd`,
    # `rm -r --no-preserve-root /` — all variants the old regex missed.
    if cmd == "rm":
        flag_chars: set[str] = set()
        positional: list[str] = []
        for arg in args:
            if arg == "--":
                continue
            if arg.startswith("--"):
                continue
            if arg.startswith("-") and len(arg) > 1:
                flag_chars.update(arg[1:])
            else:
                positional.append(arg)
        if flag_chars & {"r", "R"}:
            for p in positional:
                base = p.rstrip("/")
                if p == "/" or base in ("", "~", "$HOME", "/home", "/Users"):
                    return f"Blocked dangerous command: rm -r {p}"
                if p.startswith(("/home/", "/Users/")) and p.count("/") <= 2:
                    return f"Blocked dangerous command: rm -r {p}"

    # System-shutdown commands as the actual command word.
    if cmd in _DANGEROUS_COMMANDS:
        return f"Blocked dangerous command: {cmd}"

    # mkfs.* anywhere as the command word.
    if cmd == "mkfs" or cmd.startswith("mkfs."):
        return f"Blocked dangerous command: {cmd}"

    # dd writing to a /dev block device (sda, sdb, nvme0n1, etc).
    # /dev/null and /dev/tty* are common-and-harmless, so allow those.
    if cmd == "dd":
        for a in args:
            if a.startswith("of=/dev/"):
                target = a[len("of="):]
                if target.startswith(("/dev/null", "/dev/tty", "/dev/std")):
                    continue
                return f"Blocked dangerous command: dd {a}"

    # chmod 777 / (accept 777 or 0777 etc as mode arg).
    if cmd == "chmod":
        mode_arg = next(
            (a for a in args if not a.startswith("-") and a != "--"), None,
        )
        if mode_arg and mode_arg.lstrip("0") == "777":
            mode_idx = args.index(mode_arg)
            after = [a for a in args[mode_idx + 1:] if not a.startswith("-")]
            for a in after:
                if a == "/" or a.rstrip("/") in ("", "~", "$HOME"):
                    return f"Blocked dangerous command: chmod 777 {a}"

    return None


# ─── Sandbox Wrapping ───────────────────────────────────────────────────────

def _wrap_for_sandbox(cmd: str, session_dir: str, sandboxed: bool = False, no_network: bool = False) -> str:
    """Wrap a command through the sandbox script if sandboxing is enabled."""
    if not sandboxed and os.environ.get("CLIVE_SANDBOX") != "1":
        return cmd
    script = os.path.join(os.path.dirname(os.path.dirname(__file__)), "sandbox", "run.sh")
    parts = ["bash", shlex.quote(script), shlex.quote(session_dir)]
    if no_network:
        parts.append("--no-network")
    parts.append(shlex.quote(cmd))
    return " ".join(parts)


# ─── File Writing ───────────────────────────────────────────────────────────

def write_file(path: str, content: str) -> str:
    try:
        if os.path.dirname(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
        return f"[Written: {path}]"
    except Exception as e:
        return f"[Error writing {path}: {e}]"


# ─── Script Extraction ─────────────────────────────────────────────────────

def _extract_script(text: str) -> str:
    """Extract bash or Python script from LLM response."""
    # Try fenced code block (bash, sh, or python)
    m = re.search(r'```(?:bash|sh|python[3]?)?\s*\n([\s\S]*?)```', text)
    if m:
        return m.group(1).strip()
    # Try unfenced: everything from shebang to end (or next ```)
    m = re.search(r'(#!(?:/bin/bash|/usr/bin/env python[3]?)[\s\S]*?)(?:```|$)', text)
    if m:
        return m.group(1).strip()
    raise ValueError(f"No script found in response:\n{text[:200]}")


# ── Model-Aware Context Budget ───────────────────────────────────────────────

# Pattern → max_user_turns. First match wins.
# Expensive checked first so "o3-mini" → expensive (not cheap via "mini").
_MODEL_BUDGETS = [
    # Expensive models — 3 turns
    (re.compile(r'opus|\bo[13](-|\b)', re.I), 3),
    # Cheap / fast models — 6 turns
    (re.compile(r'flash|haiku|\bmini\b|llama|mistral|\bphi[-\d]|local|gemma', re.I), 6),
]
_DEFAULT_MAX_TURNS = 4


# ── Model Tier Resolution ───────────────────────────────────────────────────

_TIER_MAP: dict[str, dict[str, str | None]] = {
    "openai": {"fast": "gpt-4o-mini", "default": None},
    "anthropic": {"fast": "claude-haiku-4-5-20251001", "default": None},
    "gemini": {"fast": "gemini-2.0-flash", "default": None},
    "openrouter": {"fast": None, "default": None},
    "ollama": {"fast": "llama3", "default": None},
    "lmstudio": {"fast": "local", "default": None},
    "delegate": {"fast": None, "default": None},
}


def resolve_model_tier(tier: str | None, provider: str | None = None) -> str | None:
    """Resolve a tier label ('fast', 'default') to a concrete model name.

    Returns None when tier is None, 'default', or the provider has no
    mapping.  None signals the caller to fall back to the global MODEL.
    """
    if tier is None or tier == "default":
        return None
    if provider is None:
        provider = os.getenv("LLM_PROVIDER", "openrouter")
    tiers = _TIER_MAP.get(provider)
    if not tiers:
        return None
    return tiers.get(tier)


def context_budget(model: str) -> dict:
    """Return context trimming parameters based on model cost tier.

    Returns dict with 'max_user_turns' key for use with _trim_messages().
    """
    if not model or model == "delegate":
        return {"max_user_turns": _DEFAULT_MAX_TURNS}
    for pattern, turns in _MODEL_BUDGETS:
        if pattern.search(model):
            return {"max_user_turns": turns}
    return {"max_user_turns": _DEFAULT_MAX_TURNS}
