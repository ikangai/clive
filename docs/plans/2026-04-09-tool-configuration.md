# Tool Configuration System — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** When a tool is installed but not configured (e.g. neomutt without SMTP settings), guide the user through interactive setup instead of falsely claiming success or saying "unavailable."

**Architecture:** New `config.py` module owns configuration state (`~/.clive/config/*.toml`). Tools declare a `config` schema in their toolset definition. The classifier learns a new `unconfigured` mode that triggers the setup flow before execution. After setup, user is asked whether to retry the task.

**Tech Stack:** Python `tomllib` (stdlib, 3.11+) for reading TOML, simple serializer for writing (flat key-value only). `getpass` for secret fields.

---

### Task 1: Create `config.py` — core config module

**Files:**
- Create: `config.py`

**Step 1: Write `config.py` with all core functions**

```python
"""Tool configuration system.

Manages ~/.clive/config/*.toml files for tools that need credentials
or account settings beyond just being installed (email, cloud sync, etc.).

Public API:
    is_configured(config_schema)     → bool
    load_config(filename)            → dict
    get_unconfigured(panes, cmds)    → list[str]
    run_setup(tool_name, config_schema) → bool  (True = retry task)
    generate_neomuttrc(config)       → None
"""

import getpass
import os
import tomllib

from output import step, detail

CONFIG_DIR = os.path.expanduser("~/.clive/config")


def _write_toml(data: dict, path: str) -> None:
    """Write a flat dict as TOML."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        for k, v in data.items():
            if isinstance(v, bool):
                f.write(f"{k} = {'true' if v else 'false'}\n")
            elif isinstance(v, int):
                f.write(f"{k} = {v}\n")
            else:
                f.write(f'{k} = "{v}"\n')


def load_config(filename: str) -> dict:
    """Read a TOML config file from CONFIG_DIR. Returns {} if missing."""
    path = os.path.join(CONFIG_DIR, filename)
    if not os.path.exists(path):
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


def is_configured(config_schema: dict) -> bool:
    """Check if a tool's config file exists with all required fields."""
    filename = config_schema.get("file", "")
    if not filename:
        return True  # no config needed
    data = load_config(filename)
    if not data:
        return False
    for field in config_schema.get("fields", []):
        if field.get("required") and not data.get(field["key"]):
            return False
    return True


def get_unconfigured(panes: list[dict], commands: list[dict]) -> list[str]:
    """Return tool names that are installed but need configuration."""
    unconfigured = []
    for pane in panes:
        schema = pane.get("config")
        if schema and not is_configured(schema):
            unconfigured.append(pane["name"])
    for cmd in commands:
        schema = cmd.get("config")
        if schema and not is_configured(schema):
            unconfigured.append(cmd["name"])
    return unconfigured


def run_setup(tool_name: str, config_schema: dict) -> bool:
    """Interactive setup: prompt for fields, write config, generate native config.

    Returns True if user wants to retry the original task.
    """
    step(f"{tool_name} not configured. Let's set it up.")
    fields = config_schema.get("fields", [])
    values = {}

    for field in fields:
        key = field["key"]
        prompt_text = field["prompt"]
        default = field.get("default")
        secret = field.get("secret", False)

        if default is not None:
            prompt_text += f" [{default}]"
        prompt_text += ": "

        try:
            if secret:
                val = getpass.getpass(f"  {prompt_text}")
            else:
                val = input(f"  {prompt_text}").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            detail("Setup cancelled.")
            return False

        if not val and default is not None:
            val = default
        if not val and field.get("required"):
            detail(f"{field['prompt']} is required. Setup cancelled.")
            return False

        # Coerce to int if default is int
        if isinstance(default, int) and isinstance(val, str):
            try:
                val = int(val)
            except ValueError:
                pass

        values[key] = val

    # Write TOML
    toml_path = os.path.join(CONFIG_DIR, config_schema["file"])
    _write_toml(values, toml_path)
    detail(f"Config saved to {toml_path}")

    # Generate native config if generator specified
    generator_name = config_schema.get("generator")
    if generator_name:
        gen_fn = GENERATORS.get(generator_name)
        if gen_fn:
            gen_fn(values, config_schema)

    # Ask to retry
    try:
        retry = input("  Try the task again? [Y/n]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return retry in ("", "y", "yes")


# ── Native config generators ────────────────────────────────────────────────

def generate_neomuttrc(config: dict, schema: dict) -> None:
    """Write ~/.config/neomutt/neomuttrc from config values."""
    target = os.path.expanduser(schema.get("generates", "~/.config/neomutt/neomuttrc"))
    os.makedirs(os.path.dirname(target), exist_ok=True)

    # Back up existing
    if os.path.exists(target):
        backup = target + ".bak"
        os.rename(target, backup)
        detail(f"Backed up existing config to {backup}")

    addr = config["address"]
    smtp = config["smtp_server"]
    smtp_port = config.get("smtp_port", 587)
    smtp_tls = config.get("smtp_tls", "starttls")
    imap = config["imap_server"]
    imap_port = config.get("imap_port", 993)
    password = config["password"]

    # SMTP URL scheme
    if smtp_tls == "starttls":
        smtp_scheme = "smtp"
        starttls = "yes"
        force_tls = "yes"
    else:
        smtp_scheme = "smtps"
        starttls = "no"
        force_tls = "yes"

    content = f"""\
# Generated by clive — {addr}
set from = "{addr}"
set realname = "{addr.split('@')[0]}"
set smtp_url = "{smtp_scheme}://{addr}@{smtp}:{smtp_port}"
set smtp_pass = "{password}"
set folder = "imaps://{addr}@{imap}:{imap_port}"
set spoolfile = "+INBOX"
set record = "+Sent"
set postponed = "+Drafts"
set trash = "+Trash"
set ssl_starttls = {starttls}
set ssl_force_tls = {force_tls}
set mail_check = 60
set sort = reverse-date
"""
    with open(target, "w") as f:
        f.write(content)
    os.chmod(target, 0o600)
    detail(f"Generated {target}")


GENERATORS = {
    "generate_neomuttrc": generate_neomuttrc,
}
```

**Step 2: Verify module imports cleanly**

Run: `cd /Users/martintreiber/Documents/Development/clive && python3 -c "import config; print('ok')"`
Expected: `ok`

**Step 3: Commit**

```bash
git add config.py
git commit -m "feat(config): add tool configuration module with email setup"
```

---

### Task 2: Add config schema to email pane in `toolsets.py`

**Files:**
- Modify: `toolsets.py:82-95` (email pane definition)

**Step 1: Add `config` block to the email pane**

Add after `"category": "comms"` in the email pane dict:

```python
    "email": {
        "name": "email",
        "cmd": None,
        "app_type": "email_cli",
        "description": (
            "Email pane. Run neomutt for interactive mail. "
            "Helper scripts: bash fetch_emails.sh, "
            "bash send_reply.sh <to> <subject> <body>."
        ),
        "host": None,
        "check": "command -v neomutt",
        "install": "brew install neomutt",
        "category": "comms",
        "config": {
            "file": "email.toml",
            "generates": "~/.config/neomutt/neomuttrc",
            "fields": [
                {"key": "address",     "prompt": "Email address",  "required": True},
                {"key": "smtp_server", "prompt": "SMTP server",    "required": True},
                {"key": "smtp_port",   "prompt": "SMTP port",      "default": 587},
                {"key": "smtp_tls",    "prompt": "SMTP security",  "default": "starttls"},
                {"key": "imap_server", "prompt": "IMAP server",    "required": True},
                {"key": "imap_port",   "prompt": "IMAP port",      "default": 993},
                {"key": "password",    "prompt": "Password",       "required": True, "secret": True},
            ],
            "generator": "generate_neomuttrc",
        },
    },
```

**Step 2: Verify toolsets still resolves**

Run: `python3 -c "from toolsets import resolve_toolset; r = resolve_toolset('full'); print([p['name'] for p in r['panes']])"`
Expected: list including `email`

**Step 3: Commit**

```bash
git add toolsets.py
git commit -m "feat(toolsets): add config schema to email pane"
```

---

### Task 3: Wire `unconfigured` into classifier prompt (`prompts.py`)

**Files:**
- Modify: `prompts.py:235-279` (`build_classifier_prompt`)

**Step 1: Add `unconfigured_tools` parameter and mode**

Add `unconfigured_tools: list[str] = None` parameter.
Add `Unconfigured tools (installed but need setup): {list}` line after Missing commands.
Add `"unconfigured"` to the mode enum and mode guide.
Add example.

The updated function signature:
```python
def build_classifier_prompt(
    available_panes: list[str],
    installed_commands: list[str],
    missing_commands: list[str],
    available_endpoints: list[str],
    unconfigured_tools: list[str] | None = None,
) -> str:
```

Add after `Missing commands:` line:
```
Unconfigured tools (installed, need setup first): {', '.join(unconfigured_tools) if unconfigured_tools else 'none'}
```

Update mode enum to include `unconfigured`:
```
"mode": "direct|script|interactive|plan|unavailable|unconfigured|answer|clarify",
```

Add to mode guide:
```
- "unconfigured": tool is installed but needs account/credential setup. Set tool name.
```

Add example:
```
- "send email to bob@x.com" (email unconfigured) -> {{"mode":"unconfigured","tool":"email","pane":"email","driver":"email_cli","cmd":null,"fallback_mode":null,"stateful":false,"message":"Email needs account setup"}}
```

**Step 2: Verify prompt builds**

Run: `python3 -c "from prompts import build_classifier_prompt; p = build_classifier_prompt(['shell'], ['jq'], [], [], ['email']); print('unconfigured' in p.lower())"`
Expected: `True`

**Step 3: Commit**

```bash
git add prompts.py
git commit -m "feat(prompts): add unconfigured mode to classifier prompt"
```

---

### Task 4: Wire config checks into `clive.py`

**Files:**
- Modify: `clive.py:47-48` (imports)
- Modify: `clive.py:86-100` (`_classify` — pass unconfigured)
- Modify: `clive.py:254-296` (`_setup_session` — compute unconfigured)
- Modify: `clive.py:370-378` (`_run_inner` — handle unconfigured mode)

**Step 1: Add imports**

At `clive.py:48`, add config imports:
```python
from config import get_unconfigured, run_setup, load_config
```

**Step 2: Compute unconfigured in `_setup_session`**

In `_setup_session()`, after `available_cmds, missing_cmds = check_commands(...)` (line 265), add:
```python
    # Check which tools need configuration
    unconfigured = get_unconfigured(resolved["panes"], available_cmds)
```

Add `"unconfigured": unconfigured` to the returned dict (after `"endpoints"` at line 296).

**Step 3: Pass unconfigured to classifier**

In `_classify()` (line 86-100), after `endpoint_names`:
```python
    unconfigured = session_ctx.get("unconfigured", [])
```

And pass to `build_classifier_prompt`:
```python
    system_prompt = build_classifier_prompt(
        available_panes=available_panes,
        installed_commands=installed,
        missing_commands=missing,
        available_endpoints=endpoint_names,
        unconfigured_tools=unconfigured,
    )
```

**Step 4: Handle `mode == "unconfigured"` in `_run_inner`**

After the `unavailable` check (line 375-378), add:
```python
        if cr.mode == "unconfigured":
            # Find config schema for this tool
            from toolsets import PANES, COMMANDS
            config_schema = None
            tool_key = cr.tool or ""
            for pane_def in PANES.values():
                if pane_def["name"] == tool_key and pane_def.get("config"):
                    config_schema = pane_def["config"]
                    break
            if not config_schema:
                for cmd_name, cmd_def in COMMANDS.items():
                    if cmd_name == tool_key and cmd_def.get("config"):
                        config_schema = cmd_def["config"]
                        break
            if config_schema:
                retry = run_setup(tool_key, config_schema)
                if retry:
                    # Refresh unconfigured list in session_ctx
                    session_ctx["unconfigured"] = [
                        t for t in session_ctx.get("unconfigured", []) if t != tool_key
                    ]
                    return _run_inner(task, toolset_spec, output_format, max_tokens,
                                      session_dir, _cleanup, _state, session_ctx)
                return "Setup completed. Re-run the task when ready."
            else:
                step(f"Unconfigured: {tool_key}")
                detail("No configuration schema found for this tool.")
                return f"{tool_key} needs configuration but no setup is available."
```

**Step 5: Update `models.py` comment**

In `models.py:107`, update the mode comment:
```python
    mode: str  # direct, script, interactive, plan, unavailable, unconfigured, answer, clarify
```

**Step 6: Test the full flow manually**

Run: `cd /Users/martintreiber/Documents/Development/clive && python3 -c "from config import is_configured; print(is_configured({'file': 'email.toml', 'fields': [{'key': 'address', 'required': True}]}))"`
Expected: `False` (no config file exists yet)

**Step 7: Commit**

```bash
git add clive.py models.py
git commit -m "feat(clive): wire unconfigured mode into classify + execute flow"
```

---

### Task 5: Update `--list-tools` to show config status

**Files:**
- Modify: `clive.py:791-817` (`--list-tools` section)

**Step 1: Show config status for panes**

After the pane name/type line (line 798-802), add a config status indicator:
```python
        for p in resolved["panes"]:
            cfg = p.get("config")
            if cfg:
                from config import is_configured
                configured = is_configured(cfg)
                icon = "✓" if configured else "⚠"
                status = "configured" if configured else "needs setup"
                print(f"  {p['name']:16s} [{p['app_type']}] {icon} {status}")
            else:
                print(f"  {p['name']:16s} [{p['app_type']}]")
            print(f"    {p['description'][:80]}")
            if p.get("check"):
                print(f"    install: {p.get('install', '')}")
            print()
```

**Step 2: Commit**

```bash
git add clive.py
git commit -m "feat(cli): show config status in --list-tools output"
```

---

### Task 6: Add `--setup <tool>` CLI flag

**Files:**
- Modify: `clive.py` (argparse section + handler)

**Step 1: Find argparse section**

Find the argparse setup and add:
```python
parser.add_argument("--setup", metavar="TOOL", help="Configure a tool (e.g. --setup email)")
```

**Step 2: Add handler before main execution**

After the `--list-tools` handler, add:
```python
    if args.setup:
        from config import run_setup, is_configured
        from toolsets import PANES, COMMANDS
        tool_name = args.setup
        config_schema = None
        for pane_def in PANES.values():
            if pane_def["name"] == tool_name and pane_def.get("config"):
                config_schema = pane_def["config"]
                break
        if not config_schema:
            for cmd_name, cmd_def in COMMANDS.items():
                if cmd_name == tool_name and cmd_def.get("config"):
                    config_schema = cmd_def["config"]
                    break
        if not config_schema:
            print(f"No configuration needed for '{tool_name}'.")
            raise SystemExit(1)
        if is_configured(config_schema):
            print(f"'{tool_name}' is already configured.")
            reconfigure = input("Reconfigure? [y/N]: ").strip().lower()
            if reconfigure not in ("y", "yes"):
                raise SystemExit(0)
        run_setup(tool_name, config_schema)
        raise SystemExit(0)
```

**Step 3: Commit**

```bash
git add clive.py
git commit -m "feat(cli): add --setup flag for explicit tool configuration"
```
