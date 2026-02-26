"""Tool registry with named profiles.

Each tool is a dict describing a tmux pane the agent can use.
Tools are composed into profiles (toolsets) that users select via --toolset.

Public API:
    get_toolset(name)   → list[dict]
    list_toolsets()     → dict[str, list[dict]]
    DEFAULT_TOOLSET     → "minimal"
"""

# ── Individual tool definitions ──────────────────────────────────────────────

TOOL_SHELL = {
    "name": "shell",
    "cmd": None,
    "app_type": "shell",
    "description": (
        "Local bash shell for filesystem ops, scripting, and general commands. "
        "Working directory: /tmp/clive"
    ),
    "host": None,
}

TOOL_BROWSER = {
    "name": "browser",
    "cmd": None,
    "app_type": "browser",
    "description": (
        "Web browsing pane. Use lynx -dump <url> for web pages, "
        "curl -s for APIs, wget for downloads. "
        "Pipe through head/grep/sed to extract what you need."
    ),
    "host": None,
}

TOOL_DATA = {
    "name": "data",
    "cmd": None,
    "app_type": "data",
    "description": (
        "Data processing pane. Use rg (ripgrep) for search, "
        "mlr (miller) for CSV/JSON transforms, "
        "jq for JSON, pandoc for document conversion, "
        "pdftotext for PDFs."
    ),
    "host": None,
}

TOOL_DOCS = {
    "name": "docs",
    "cmd": None,
    "app_type": "docs",
    "description": (
        "Documentation and research pane. Use pandoc to convert formats, "
        "pdftotext to read PDFs, lynx -dump for reference pages. "
        "Write results to /tmp/clive/."
    ),
    "host": None,
}

TOOL_EMAIL = {
    "name": "email",
    "cmd": None,
    "app_type": "email_cli",
    "description": (
        "Email pane. Run bash fetch_emails.sh to load unread emails. "
        "Use neomutt for interactive mail, "
        "or bash send_reply.sh <to> <subject> <body> to send. "
        "To search: neomutt -e 'limit ~s keyword'."
    ),
    "host": None,
}

TOOL_CALENDAR = {
    "name": "calendar",
    "cmd": None,
    "app_type": "calendar_cli",
    "description": (
        "Calendar pane. Use icalBuddy to query macOS calendars: "
        "icalBuddy eventsToday, icalBuddy eventsToday+7. "
        "Requires icalBuddy installed."
    ),
    "host": None,
}

TOOL_TASKS = {
    "name": "tasks",
    "cmd": None,
    "app_type": "tasks_cli",
    "description": (
        "Task management pane. Use taskwarrior: "
        "task list, task add <desc>, task <id> done, task <id> modify. "
        "Requires task (taskwarrior) installed."
    ),
    "host": None,
}

TOOL_MEDIA = {
    "name": "media",
    "cmd": None,
    "app_type": "media",
    "description": (
        "Media processing pane. Helper scripts available: "
        "bash tools/youtube.sh <cmd> — list/get/captions/transcribe YouTube content. "
        "bash tools/podcast.sh <cmd> — list/get/transcribe podcast episodes. "
        "bash tools/claude.sh <prompt> — call Claude API for sub-tasks. "
        "Requires yt-dlp, whisper, curl, jq."
    ),
    "host": None,
}

TOOL_REMOTE_BROWSER = {
    "name": "browser",
    "cmd": "ssh -i ~/.ssh/agent_key user@remote.example.com",
    "app_type": "browser",
    "description": (
        "Restricted remote shell for web access. "
        "Commands: lynx -dump <url>, head, grep. "
        "Write output to ~/files/ using >."
    ),
    "host": "user@remote.example.com",
}

TOOL_REMOTE_FILES = {
    "name": "files",
    "cmd": "ssh -i ~/.ssh/agent_key user@remote.example.com",
    "app_type": "files",
    "description": (
        "Remote filesystem. Write files to ~/files/ using shell redirects. "
        "This is a REMOTE shell — scp downloads must run from the local 'shell' pane."
    ),
    "host": "user@remote.example.com",
}

# ── Toolset profiles ────────────────────────────────────────────────────────

TOOLSETS = {
    "minimal": [
        TOOL_SHELL,
    ],
    "standard": [
        TOOL_SHELL,
        TOOL_BROWSER,
        TOOL_DATA,
        TOOL_DOCS,
    ],
    "full": [
        TOOL_SHELL,
        TOOL_BROWSER,
        TOOL_DATA,
        TOOL_DOCS,
        TOOL_EMAIL,
        TOOL_CALENDAR,
        TOOL_TASKS,
        TOOL_MEDIA,
    ],
    "remote": [
        TOOL_SHELL,
        TOOL_REMOTE_BROWSER,
        TOOL_REMOTE_FILES,
        TOOL_EMAIL,
    ],
}

DEFAULT_TOOLSET = "minimal"


def get_toolset(name: str) -> list[dict]:
    """Return the tool list for a named profile.

    Raises KeyError if the profile doesn't exist.
    """
    if name not in TOOLSETS:
        available = ", ".join(sorted(TOOLSETS))
        raise KeyError(f"Unknown toolset {name!r}. Available: {available}")
    return list(TOOLSETS[name])


def list_toolsets() -> dict[str, list[str]]:
    """Return a dict mapping profile names to their tool names."""
    return {
        name: [t["name"] for t in tools]
        for name, tools in TOOLSETS.items()
    }
