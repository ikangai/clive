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

from output import step, detail, finish

CONFIG_DIR = os.path.expanduser("~/.clive/config")


def _escape_toml_string(s: str) -> str:
    """Escape special characters for TOML string values."""
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _write_toml(data: dict, path: str) -> None:
    """Write a flat dict as TOML. Sets 0o600 permissions (may contain secrets)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        for k, v in data.items():
            if isinstance(v, bool):
                f.write(f"{k} = {'true' if v else 'false'}\n")
            elif isinstance(v, int):
                f.write(f"{k} = {v}\n")
            else:
                f.write(f'{k} = "{_escape_toml_string(str(v))}"\n')
    os.chmod(path, 0o600)


def load_config(filename: str) -> dict:
    """Read a TOML config file from CONFIG_DIR. Returns {} if missing or malformed."""
    path = os.path.join(CONFIG_DIR, filename)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError:
        import logging
        logging.warning(f"Malformed TOML in {path}, treating as unconfigured")
        return {}


def is_configured(config_schema: dict) -> bool:
    """Check if a tool's config file exists with all required fields."""
    filename = config_schema.get("file", "")
    if not filename:
        return True
    data = load_config(filename)
    if not data:
        return False
    for field in config_schema.get("fields", []):
        if field.get("required") and field["key"] not in data:
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


def find_config_schema(tool_name: str) -> dict | None:
    """Look up the config schema for a tool by name (checks panes and commands)."""
    from toolsets import PANES, COMMANDS
    for pane_def in PANES.values():
        if pane_def["name"] == tool_name and pane_def.get("config"):
            return pane_def["config"]
    for cmd_name, cmd_def in COMMANDS.items():
        if cmd_name == tool_name and cmd_def.get("config"):
            return cmd_def["config"]
    return None


def run_setup(tool_name: str, config_schema: dict) -> bool:
    """Interactive setup: prompt for fields, write config, generate native config.

    Returns True if user wants to retry the original task.
    """
    step(f"{tool_name} not configured. Let's set it up.")
    finish()  # stop pulse animation before input() prompts
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

        if isinstance(default, int) and isinstance(val, str):
            try:
                val = int(val)
            except ValueError:
                pass

        values[key] = val

    toml_path = os.path.join(CONFIG_DIR, config_schema.get("file", f"{tool_name}.toml"))
    _write_toml(values, toml_path)
    detail(f"Config saved to {toml_path}")

    generator_name = config_schema.get("generator")
    if generator_name:
        gen_fn = GENERATORS.get(generator_name)
        if gen_fn:
            gen_fn(values, config_schema)

    try:
        retry = input("  Try the task again? [Y/n]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return retry in ("", "y", "yes")


def generate_neomuttrc(config: dict, schema: dict) -> None:
    """Write ~/.config/neomutt/neomuttrc from config values."""
    target = os.path.expanduser(schema.get("generates", "~/.config/neomutt/neomuttrc"))
    os.makedirs(os.path.dirname(target), exist_ok=True)

    if os.path.exists(target):
        import time
        ts = time.strftime("%Y%m%d_%H%M%S")
        backup = f"{target}.{ts}.bak"
        os.rename(target, backup)
        detail(f"Backed up existing config to {backup}")

    addr = config["address"]
    smtp = config["smtp_server"]
    smtp_port = config.get("smtp_port", 587)
    smtp_tls = config.get("smtp_tls", "starttls")
    imap = config["imap_server"]
    imap_port = config.get("imap_port", 993)
    password = config["password"]

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
