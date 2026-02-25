#!/usr/bin/env python3
"""
tmux Agent Loop — v0 multi-pane
Supports multiple CLI tools running in parallel panes.

Usage:
    python agent.py "your task description"
    python agent.py                          # uses built-in example task

    Watch in real-time:
        tmux attach -t agent

Requirements:
    pip install -r requirements.txt

Environment:
    OPENROUTER_API_KEY (set in .env file)
"""

import argparse
import os
import re
import time
import openai
import libtmux
from dotenv import load_dotenv

load_dotenv()

# ─── Config ───────────────────────────────────────────────────────────────────

MODEL = "z-ai/glm-5"
OPENROUTER_BASE = "https://openrouter.ai/api/v1"
SESSION_NAME = "agent"
IDLE_TIMEOUT = 2.0
MAX_TURNS = 50

# ─── Tool Registry ────────────────────────────────────────────────────────────

DEFAULT_TOOLS = [
    {
        "name": "shell",
        "cmd": None,
        "app_type": "shell",
        "description": "General purpose bash shell for filesystem ops and scripting",
        "host": None
    },
    {
        "name": "browser",
        "cmd": None,
        "app_type": "browser",
        "description": "Fetch and render web pages as plain text. Usage: lynx -dump <url>",
        "host": None
    },

    {
    "name": "email",
    "cmd": "bash ./fetch_emails.sh",
    "app_type": "email_cli",
    "description": (
        "Fetches unread IMAP emails as plain text. "
        "To send a reply: bash ./send_reply.sh <to> <subject> <body>. "
        "To search: neomutt -e 'limit ~s keyword'"
    ),
    "host": None
    },
    # {
    #     "name": "calendar",
    #     "cmd": "bash /opt/tools/calendar.sh",
    #     "app_type": "calendar_cli",
    #     "description": "Shows today's events and upcoming schedule",
    #     "host": None
    # },
    # Remote tool over SSH:
    # {
    #     "name": "remote",
    #     "cmd": None,
    #     "app_type": "shell",
    #     "description": "Shell on remote build server",
    #     "host": "user@remote.example.com"
    # },
]


# ─── System Prompt ────────────────────────────────────────────────────────────

def build_system_prompt(tool_status: dict) -> str:
    lines = []
    for name, info in tool_status.items():
        indicator = "✓" if info["status"] == "ready" else "✗"
        lines.append(
            f"  {indicator} {name:16} [{info['app_type']}] — {info['description']}"
        )
    tool_list = "\n".join(lines)

    return f"""You are an autonomous agent operating multiple terminal panes via tmux.
Each pane runs a different tool. You choose which pane to act on each turn.

Available tools (verified at startup):
{tool_list}

Send exactly one command per turn using XML tags.
Always specify which pane to target:

  <cmd type="shell" pane="shell">ls -la /tmp</cmd>
  <cmd type="shell" pane="browser">lynx -dump https://example.com</cmd>
  <cmd type="read_file" pane="shell">/tmp/agent/links.txt</cmd>
  <cmd type="write_file" pane="shell" path="/tmp/agent/out.txt">content</cmd>
  <cmd type="task_complete">your final summary</cmd>
  <cmd type="unknown" pane="email">description of unexpected state</cmd>

Rules:
- One command per turn
- Only use tools marked with ✓
- Write state to /tmp/agent/ using >> redirects — never accumulate lists in conversation
- Use read_file for large files, never cat them directly to the terminal
- Silent commands (mkdir, touch) produce no output — this is normal
- If a pane shows something unexpected, use unknown to escalate
"""


# ─── Session Setup ────────────────────────────────────────────────────────────

def setup_session(tools: list) -> tuple:
    server = libtmux.Server()
    session = server.new_session(
        session_name=SESSION_NAME,
        kill_session=True,
        attach=False,
        window_name=tools[0]["name"]
    )

    panes = {}

    for i, tool in enumerate(tools):
        if i == 0:
            window = session.active_window
            window.rename_window(tool["name"])
        else:
            window = session.new_window(window_name=tool["name"], attach=False)

        pane = window.active_pane

        # SSH for remote tools
        if tool.get("host"):
            pane.send_keys(f"ssh {tool['host']}", enter=True)
            time.sleep(2)

        # sentinel prompt
        pane.send_keys('export PS1="[AGENT_READY] $ "', enter=True)

        # pane title as metadata channel
        # agent can update this mid-session:
        # printf "\033]2;app:lynx\033\\"
        pane.send_keys(
            f'printf "\\033]2;{tool["app_type"]}\\033\\\\"',
            enter=True
        )

        # launch tool if specified
        if tool.get("cmd"):
            pane.send_keys(tool["cmd"], enter=True)

        panes[tool["name"]] = {
            "pane": pane,
            "app_type": tool["app_type"],
            "description": tool["description"]
        }

    # shared working directory on first pane
    list(panes.values())[0]["pane"].send_keys("mkdir -p /tmp/agent", enter=True)
    time.sleep(1.5)

    return session, panes


# ─── Health Check ─────────────────────────────────────────────────────────────

def check_health(panes: dict) -> dict:
    status = {}
    for name, info in panes.items():
        lines = info["pane"].cmd("capture-pane", "-p").stdout
        screen = "\n".join(lines) if lines else ""
        ready = "[AGENT_READY]" in screen
        status[name] = {
            "status": "ready" if ready else "unavailable",
            "app_type": info["app_type"],
            "description": info["description"]
        }
        indicator = "✓" if ready else "✗"
        print(f"  {indicator} {name:16} [{info['app_type']}]")
    return status


# ─── Terminal Capture ─────────────────────────────────────────────────────────

def wait_for_ready(pane: libtmux.Pane, timeout: float = IDLE_TIMEOUT) -> str:
    last = ""
    last_change = time.time()

    while True:
        lines = pane.cmd("capture-pane", "-p").stdout
        screen = "\n".join(lines) if lines else ""

        if lines and "[AGENT_READY] $" in lines[-1]:
            return screen

        if screen != last:
            last = screen
            last_change = time.time()
        elif time.time() - last_change > timeout:
            return screen

        time.sleep(0.1)


def get_meta(pane: libtmux.Pane) -> str:
    try:
        return pane.cmd("display-message", "-p", "#T").stdout[0]
    except Exception:
        return "unknown"


# ─── Command Parsing ──────────────────────────────────────────────────────────

def parse_command(text: str) -> dict:
    # write_file — pane before path
    m = re.search(
        r'<cmd\s+type=["\']write_file["\'][^>]*pane=["\']([^"\']+)["\'][^>]*path=["\']([^"\']+)["\']>([\s\S]*?)</cmd>',
        text
    )
    if m:
        return {"type": "write_file", "pane": m.group(1), "path": m.group(2), "value": m.group(3).strip()}

    # write_file — path before pane
    m = re.search(
        r'<cmd\s+type=["\']write_file["\'][^>]*path=["\']([^"\']+)["\'][^>]*pane=["\']([^"\']+)["\']>([\s\S]*?)</cmd>',
        text
    )
    if m:
        return {"type": "write_file", "pane": m.group(2), "path": m.group(1), "value": m.group(3).strip()}

    # task_complete — no pane needed
    m = re.search(r'<cmd\s+type=["\']task_complete["\']>([\s\S]*?)</cmd>', text)
    if m:
        return {"type": "task_complete", "pane": None, "value": m.group(1).strip()}

    # everything else with pane
    m = re.search(
        r'<cmd\s+type=["\'](\w+)["\'][^>]*pane=["\']([^"\']+)["\'][^>]*>([\s\S]*?)</cmd>',
        text
    )
    if m:
        return {"type": m.group(1), "pane": m.group(2), "value": m.group(3).strip()}

    # fallback: no pane attribute
    m = re.search(r'<cmd\s+type=["\'](\w+)["\']>([\s\S]*?)</cmd>', text)
    if m:
        return {"type": m.group(1), "pane": None, "value": m.group(2).strip()}

    return {"type": "none", "pane": None, "value": ""}


# ─── File Channel ─────────────────────────────────────────────────────────────

def read_file(path: str) -> str:
    try:
        with open(path, "r", errors="replace") as f:
            content = f.read()
        return f"[File: {path} — {len(content.splitlines())} lines]\n{content}"
    except FileNotFoundError:
        return f"[Error: file not found: {path}]"
    except Exception as e:
        return f"[Error reading {path}: {e}]"


def write_file(path: str, content: str) -> str:
    try:
        if os.path.dirname(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
        return f"[Written: {path}]"
    except Exception as e:
        return f"[Error writing {path}: {e}]"


# ─── Main Loop ────────────────────────────────────────────────────────────────

def run(task: str, tools: list = DEFAULT_TOOLS):

    print(f"\n{'═' * 60}")
    print(f"Setting up session: {SESSION_NAME}")
    print(f"{'─' * 60}")

    session, panes = setup_session(tools)

    print(f"\nHealth check:")
    tool_status = check_health(panes)

    client = openai.OpenAI(
        base_url=OPENROUTER_BASE,
        api_key=os.environ["OPENROUTER_API_KEY"],
    )
    system_prompt = build_system_prompt(tool_status)
    messages = [{"role": "system", "content": system_prompt}]

    print(f"\n{'─' * 60}")
    print(f"Task: {task}")
    print(f"Watch: tmux attach -t {SESSION_NAME}")
    print(f"{'═' * 60}\n")

    messages.append({"role": "user", "content": f"Task: {task}"})

    start_time = time.time()
    total_prompt_tokens = 0
    total_completion_tokens = 0

    for turn in range(1, MAX_TURNS + 1):

        # capture all ready panes
        pane_screens = []
        for name, info in panes.items():
            if tool_status[name]["status"] == "ready":
                screen = wait_for_ready(info["pane"])
                meta = get_meta(info["pane"])
                pane_screens.append(f"[Pane: {name}] [Meta: {meta}]\n{screen}")

        context = f"[Turn {turn}]\n" + "\n\n".join(pane_screens)
        print(f"{'─' * 40}\n{context}")

        messages.append({"role": "user", "content": context})

        turn_start = time.time()
        response = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            max_tokens=1024,
        )
        turn_elapsed = time.time() - turn_start

        # track tokens
        if response.usage:
            total_prompt_tokens += response.usage.prompt_tokens
            total_completion_tokens += response.usage.completion_tokens
            print(f"⏱  Turn {turn}: {turn_elapsed:.1f}s | tokens: {response.usage.prompt_tokens} in / {response.usage.completion_tokens} out")

        reply = response.choices[0].message.content
        messages.append({"role": "assistant", "content": reply})
        print(f"\nAgent:\n{reply}\n")

        cmd = parse_command(reply)
        cmd_type = cmd["type"]
        cmd_pane = cmd["pane"]
        cmd_value = cmd["value"]

        target = panes.get(cmd_pane, {}).get("pane") if cmd_pane else None

        if cmd_type == "shell":
            if not target:
                print(f"⚠  Unknown pane: {cmd_pane}")
                continue
            print(f"⌨  [{cmd_pane}] {cmd_value}")
            target.send_keys(cmd_value, enter=True)

        elif cmd_type == "read_file":
            print(f"📂  {cmd_value}")
            content = read_file(cmd_value)
            messages.append({"role": "user", "content": content})
            continue

        elif cmd_type == "write_file":
            print(f"💾  {cmd['path']}")
            result = write_file(cmd["path"], cmd_value)
            messages.append({"role": "user", "content": result})
            continue

        elif cmd_type == "task_complete":
            elapsed = time.time() - start_time
            print(f"\n{'═' * 60}\nTASK COMPLETE\n{'═' * 60}")
            print(cmd_value)
            print(f"{'─' * 60}")
            print(f"Time:   {elapsed:.1f}s ({turn} turns)")
            print(f"Tokens: {total_prompt_tokens} prompt + {total_completion_tokens} completion = {total_prompt_tokens + total_completion_tokens} total")
            print(f"{'═' * 60}\n")
            return cmd_value

        elif cmd_type == "unknown":
            print(f"\n⚠  UNKNOWN STATE [{cmd_pane}]: {cmd_value}")
            print(f"Attach and resolve: tmux attach -t {SESSION_NAME}")
            input("Press Enter when resolved...")
            continue

        elif cmd_type == "none":
            print("⚠  No valid command found.")

    elapsed = time.time() - start_time
    print(f"⚠  Max turns ({MAX_TURNS}) reached.")
    print(f"Time:   {elapsed:.1f}s ({MAX_TURNS} turns)")
    print(f"Tokens: {total_prompt_tokens} prompt + {total_completion_tokens} completion = {total_prompt_tokens + total_completion_tokens} total")
    return None


# ─── Entry Point ──────────────────────────────────────────────────────────────

EXAMPLE_TASK = (
    "In the browser pane: fetch https://example.com using lynx -dump "
    "and save the output to /tmp/agent/example.txt. "
    "Then check the links in the file read the content and update the file. After you went through all links summarize what you found."
)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="LLM agent that drives CLI tools via tmux"
    )
    parser.add_argument(
        "task",
        nargs="?",
        default=EXAMPLE_TASK,
        help="Task for the agent to perform (default: built-in example)",
    )
    args = parser.parse_args()

    run(args.task)
