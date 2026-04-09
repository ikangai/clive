"""Three-surface tool registry: panes, commands, endpoints.

The pane is the universal agent interface. The LLM reads the screen,
reasons about what it sees, and types commands. Every tool interaction
flows through a pane — the distinction is what needs its own conversation
channel vs what shares one.

Surfaces:
    Panes     — persistent conversation channels (own tmux window)
    Commands  — CLI tools that run in shell panes (auto-detected)
    Endpoints — APIs via curl in any pane (always available)

Profiles compose categories via + syntax:
    -t standard              named profile
    -t web+comms+data        compose categories
    -t standard+media+ai     profile + extras

Public API:
    resolve_toolset(spec)    → {panes, commands, endpoints, categories}
    check_commands(cmds)     → (available, missing)
    build_tools_summary(…)   → str for LLM planner
    get_toolset(name)        → list[dict]  (legacy, panes only)
    list_toolsets()          → dict         (legacy)
    DEFAULT_TOOLSET
"""

import subprocess

from output import progress

# ── Pane definitions ─────────────────────────────────────────────────────────
# Each creates a tmux window — a conversation channel the agent controls.
# Only use a separate pane when the tool needs persistent state (TUI)
# or when a parallel conversation channel adds value.

PANES = {
    "shell": {
        "name": "shell",
        "cmd": None,
        "app_type": "shell",
        "description": (
            "Local bash shell for filesystem ops, scripting, and general commands. "
            "Working directory: /tmp/clive"
        ),
        "host": None,
        "category": "core",
    },
    "browser": {
        "name": "browser",
        "aliases": ["lynx", "web", "www"],
        "cmd": None,
        "app_type": "browser",
        "description": (
            "Web browsing and API pane. Use lynx -dump <url> for web pages, "
            "curl -s for APIs, wget for downloads. "
            "Pipe through head/grep/sed to extract what you need."
        ),
        "host": None,
        "category": "web",
    },
    "data": {
        "name": "data",
        "cmd": None,
        "app_type": "data",
        "description": (
            "Data processing pane for transforms, queries, and analysis. "
            "Parallel channel so data work doesn't block other tasks."
        ),
        "host": None,
        "category": "data",
    },
    "docs": {
        "name": "docs",
        "cmd": None,
        "app_type": "docs",
        "description": (
            "Documentation and research pane. Convert formats, read PDFs, "
            "compile notes. Write results to /tmp/clive/."
        ),
        "host": None,
        "category": "docs",
    },
    "email": {
        "name": "email",
        "aliases": ["mail", "neomutt", "mutt", "msmtp", "sendmail"],
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
    "media": {
        "name": "media",
        "cmd": None,
        "app_type": "media",
        "description": (
            "Media processing pane. Helper scripts in tools/: "
            "youtube.sh (list/get/captions/transcribe), "
            "podcast.sh (list/get/transcribe), "
            "claude.sh (LLM sub-tasks). "
            "Long-running transcriptions run here without blocking other work."
        ),
        "host": None,
        "category": "media",
    },
    # Remote panes — user must configure host/key
    "remote_browser": {
        "name": "browser",
        "cmd": "ssh -i ~/.ssh/agent_key user@remote.example.com",
        "app_type": "browser",
        "description": (
            "Restricted remote shell for web access. "
            "Commands: lynx -dump <url>, head, grep. "
            "Write output to ~/files/ using >."
        ),
        "host": "user@remote.example.com",
        "category": "remote",
    },
    "remote_files": {
        "name": "files",
        "cmd": "ssh -i ~/.ssh/agent_key user@remote.example.com",
        "app_type": "files",
        "description": (
            "Remote filesystem. Write files to ~/files/ using shell redirects. "
            "This is a REMOTE shell — scp downloads must run from the local 'shell' pane."
        ),
        "host": "user@remote.example.com",
        "category": "remote",
    },
    # ── Agent (clive-to-clive) ─────────────────────────────────────────────
    "remote_agent": {
        "name": "agent",
        "cmd": "ssh -i ~/.ssh/agent_key deploy@agents.example.com 'python3 clive.py --quiet'",
        "app_type": "agent",
        "description": (
            "Remote clive instance. Send tasks as plain text, read results. "
            "Uses DONE: JSON protocol for structured responses."
        ),
        "host": "deploy@agents.example.com",
        "connect_timeout": 5,
        "category": "remote",
    },
}


# ── Command definitions ──────────────────────────────────────────────────────
# CLI tools that run in any shell-type pane. The LLM reads their output
# on the pane screen and reasons about it — no structured API needed.
# Auto-detected at startup: only available commands are shown to the LLM.

COMMANDS = {
    # ── Data ──
    "jq": {
        "description": "JSON processor — parse, filter, transform JSON output",
        "usage": "curl -s api.example.com | jq '.data[] | {name, id}'",
        "check": "command -v jq",
        "install": "brew install jq",
        "category": "data",
    },
    "rg": {
        "description": "ripgrep — fast recursive text search across files",
        "usage": "rg 'pattern' /path/to/search",
        "check": "command -v rg",
        "install": "brew install ripgrep",
        "category": "data",
    },
    "mlr": {
        "description": "miller — CSV/JSON/TSV processing, spreadsheet-like ops in shell",
        "usage": "mlr --csv filter '$age > 30' then sort-by name data.csv",
        "check": "command -v mlr",
        "install": "brew install miller",
        "category": "data",
    },
    "sqlite3": {
        "description": "SQLite — full SQL database engine, single-file databases",
        "usage": "sqlite3 data.db 'SELECT * FROM users WHERE active=1'",
        "check": "command -v sqlite3",
        "install": "built-in on macOS",
        "category": "data",
    },

    # ── Docs ──
    "pandoc": {
        "description": "Universal document converter — markdown, PDF, Word, HTML, LaTeX",
        "usage": "pandoc report.md -o report.pdf",
        "check": "command -v pandoc",
        "install": "brew install pandoc",
        "category": "docs",
    },
    "pdftotext": {
        "description": "Extract text content from PDF files",
        "usage": "pdftotext document.pdf - | head -50",
        "check": "command -v pdftotext",
        "install": "brew install poppler",
        "category": "docs",
    },

    # ── Images ──
    "convert": {
        "description": "ImageMagick — resize, convert, composite, annotate images",
        "usage": "convert input.png -resize 800x600 output.jpg",
        "check": "command -v convert",
        "install": "brew install imagemagick",
        "category": "images",
    },
    "exiftool": {
        "description": "Read and write image/video metadata (EXIF, IPTC, XMP)",
        "usage": "exiftool -DateTimeOriginal -GPSPosition photo.jpg",
        "check": "command -v exiftool",
        "install": "brew install exiftool",
        "category": "images",
    },

    # ── Media ──
    "yt-dlp": {
        "description": "Download video/audio from YouTube and 1000+ sites",
        "usage": "yt-dlp -x --audio-format mp3 'https://youtube.com/watch?v=...'",
        "check": "command -v yt-dlp",
        "install": "brew install yt-dlp",
        "category": "media",
    },
    "whisper": {
        "description": "OpenAI Whisper — local speech-to-text transcription",
        "usage": "whisper audio.mp3 --model small --output_format txt",
        "check": "command -v whisper",
        "install": "pip install openai-whisper",
        "category": "media",
    },
    "ffmpeg": {
        "description": "Audio/video converter, processor, and stream handler",
        "usage": "ffmpeg -i video.mp4 -vn -acodec mp3 audio.mp3",
        "check": "command -v ffmpeg",
        "install": "brew install ffmpeg",
        "category": "media",
    },

    # ── Comms ──
    "icalBuddy": {
        "description": "macOS calendar access — today's events, upcoming schedule",
        "usage": "icalBuddy -f eventsToday; icalBuddy eventsToday+7",
        "check": "command -v icalBuddy",
        "install": "brew install ical-buddy",
        "category": "comms",
    },
    "khard": {
        "description": "CLI contacts manager — CardDAV compatible address book",
        "usage": "khard list; khard show 'John Doe'",
        "check": "command -v khard",
        "install": "pip install khard",
        "category": "comms",
    },
    "terminal-notifier": {
        "description": "macOS native desktop notifications from CLI",
        "usage": "terminal-notifier -message 'Task done!' -title 'clive'",
        "check": "command -v terminal-notifier",
        "install": "brew install terminal-notifier",
        "category": "comms",
    },

    # ── Productivity ──
    "task": {
        "description": "Taskwarrior — full CLI task management with filters and reports",
        "usage": "task list; task add 'Review PR' project:work due:tomorrow; task 1 done",
        "check": "command -v task",
        "install": "brew install task",
        "category": "productivity",
    },
    "watson": {
        "description": "Time tracker — log hours per project from the terminal",
        "usage": "watson start myproject; watson stop; watson report",
        "check": "command -v watson",
        "install": "brew install watson",
        "category": "productivity",
    },
    "nb": {
        "description": "CLI notebook — notes, bookmarks, todos, wiki in plain text",
        "usage": "nb add 'Meeting notes...'; nb list; nb search 'quarterly'",
        "check": "command -v nb",
        "install": "brew install nb",
        "category": "productivity",
    },

    # ── Finance ──
    "hledger": {
        "description": "Plain text accounting — double-entry bookkeeping, budgets, reports",
        "usage": "hledger -f journal.txt balance; hledger register expenses",
        "check": "command -v hledger",
        "install": "brew install hledger",
        "category": "finance",
    },

    # ── Social ──
    "toot": {
        "description": "Mastodon CLI — post, read timeline, interact with fediverse",
        "usage": "toot post 'Hello fediverse!'; toot timeline",
        "check": "command -v toot",
        "install": "pip install toot",
        "category": "social",
    },

    # ── Translation ──
    "trans": {
        "description": "Translate text between languages via Google/Bing/Yandex",
        "usage": "trans en:de 'hello world'; echo 'bonjour' | trans :en",
        "check": "command -v trans",
        "install": "brew install translate-shell",
        "category": "translation",
    },

    # ── Search ──
    "ddgr": {
        "description": "DuckDuckGo search from the terminal — web search without a browser",
        "usage": "ddgr --np 'python asyncio tutorial'",
        "check": "command -v ddgr",
        "install": "brew install ddgr",
        "category": "search",
    },

    # ── Web ──
    "monolith": {
        "description": "Save complete web pages as single self-contained HTML files",
        "usage": "monolith https://example.com/article -o saved.html",
        "check": "command -v monolith",
        "install": "brew install monolith",
        "category": "web",
    },

    # ── Dev ──
    "gh": {
        "description": "GitHub CLI — PRs, issues, CI checks, releases, repo management",
        "usage": "gh pr list; gh issue create --title 'Bug'; gh run view",
        "check": "command -v gh",
        "install": "brew install gh",
        "category": "dev",
    },

    # ── Voice ──
    "sox": {
        "description": "Record audio from microphone — sound processing toolkit",
        "usage": "sox -d recording.wav trim 0 30  # record 30 seconds",
        "check": "command -v sox",
        "install": "brew install sox",
        "category": "voice",
    },
    "say": {
        "description": "macOS text-to-speech — speak text aloud",
        "usage": "say 'Task complete'; echo 'hello' | say",
        "check": "command -v say",
        "install": "built-in on macOS",
        "category": "voice",
    },

    # ── AI ──
    "claude": {
        "description": "Claude API wrapper — summarize, translate, generate documents",
        "usage": "cat notes.txt | bash tools/claude.sh 'write a summary'",
        "check": "test -x tools/claude.sh",
        "install": "included in clive (set ANTHROPIC_API_KEY)",
        "category": "ai",
    },

    # ── Sync ──
    "rclone": {
        "description": "Sync files to/from cloud storage (S3, Drive, Dropbox, 50+ backends)",
        "usage": "rclone sync /local/path remote:bucket/path",
        "check": "command -v rclone",
        "install": "brew install rclone",
        "category": "sync",
    },
}


# ── Endpoint definitions ─────────────────────────────────────────────────────
# APIs accessible via curl from any pane. Always available, no install.
# The agent reads the response on the pane screen, just like any other output.

ENDPOINTS = {
    "weather": {
        "description": "Weather forecasts and current conditions",
        "usage": "curl -s wttr.in/Berlin",
        "category": "info",
    },
    "hackernews": {
        "description": "Hacker News top/new/best stories",
        "usage": (
            "curl -s 'https://hacker-news.firebaseio.com/v0/topstories.json' "
            "| jq '.[0:5]'"
        ),
        "category": "info",
    },
    "exchange": {
        "description": "Live currency exchange rates",
        "usage": "curl -s 'https://api.frankfurter.app/latest?from=USD'",
        "category": "info",
    },
    "github_api": {
        "description": "GitHub REST API — repos, users, gists, search",
        "usage": "curl -s 'https://api.github.com/users/USERNAME' | jq .",
        "category": "info",
    },
}


# ── Categories ───────────────────────────────────────────────────────────────
# Each category groups related tools across all three surfaces.
# Categories that define panes create parallel conversation channels.
# Categories without panes add capabilities to existing channels.

CATEGORIES = {
    "core":         {"panes": ["shell"],            "commands": [],                                         "endpoints": []},
    "web":          {"panes": ["browser"],           "commands": ["monolith"],                               "endpoints": []},
    "data":         {"panes": ["data"],              "commands": ["jq", "rg", "mlr", "sqlite3"],            "endpoints": []},
    "docs":         {"panes": ["docs"],              "commands": ["pandoc", "pdftotext"],                    "endpoints": []},
    "comms":        {"panes": ["email"],             "commands": ["icalBuddy", "khard", "terminal-notifier"], "endpoints": []},
    "media":        {"panes": ["media"],             "commands": ["yt-dlp", "whisper", "ffmpeg"],            "endpoints": []},
    "productivity": {"panes": [],                    "commands": ["task", "watson", "nb"],                   "endpoints": []},
    "finance":      {"panes": [],                    "commands": ["hledger"],                                "endpoints": []},
    "social":       {"panes": [],                    "commands": ["toot"],                                   "endpoints": []},
    "translation":  {"panes": [],                    "commands": ["trans"],                                  "endpoints": []},
    "search":       {"panes": [],                    "commands": ["ddgr"],                                   "endpoints": []},
    "images":       {"panes": [],                    "commands": ["convert", "exiftool"],                    "endpoints": []},
    "dev":          {"panes": [],                    "commands": ["gh"],                                     "endpoints": []},
    "voice":        {"panes": [],                    "commands": ["sox", "say"],                             "endpoints": []},
    "ai":           {"panes": [],                    "commands": ["claude"],                                 "endpoints": []},
    "sync":         {"panes": [],                    "commands": ["rclone"],                                 "endpoints": []},
    "info":         {"panes": [],                    "commands": [],                                         "endpoints": ["weather", "hackernews", "exchange", "github_api"]},
    "remote":       {"panes": ["remote_browser", "remote_files"], "commands": [],                           "endpoints": []},
}


# ── Named profiles ──────────────────────────────────────────────────────────
# Convenience aliases for category combinations.
# Use + syntax to compose: -t standard+media+ai

PROFILES = {
    "minimal":  ["core"],
    "standard": ["core", "web", "data", "docs", "info"],
    "full":     ["core", "web", "data", "docs", "comms", "media",
                 "productivity", "search", "info"],
    "research": ["core", "web", "data", "docs", "media", "search",
                 "info", "ai"],
    "business": ["core", "web", "data", "docs", "comms", "productivity",
                 "finance", "info"],
    "creative": ["core", "web", "media", "images", "ai", "translation"],
    "remote":   ["core", "comms", "remote"],
}

DEFAULT_TOOLSET = "minimal"


# ── Resolution ───────────────────────────────────────────────────────────────

def resolve_toolset(spec: str) -> dict:
    """Resolve a toolset spec into panes, commands, and endpoints.

    Spec can be:
        "standard"              → named profile
        "web+comms+data"        → compose categories
        "standard+media"        → profile + additional categories

    Returns dict with keys: panes, commands, endpoints, categories.
    """
    parts = [p.strip() for p in spec.split("+")]
    categories = set()

    for part in parts:
        if part in PROFILES:
            categories.update(PROFILES[part])
        elif part in CATEGORIES:
            categories.add(part)
        else:
            available = sorted(set(PROFILES) | set(CATEGORIES))
            raise ValueError(
                f"Unknown profile or category {part!r}. "
                f"Available: {', '.join(available)}"
            )

    # core is always included
    categories.add("core")

    # Collect tools from categories, deduplicating
    pane_ids = []
    command_ids = []
    endpoint_ids = []
    seen_panes = set()
    seen_commands = set()
    seen_endpoints = set()

    for cat in sorted(categories):
        cat_def = CATEGORIES.get(cat, {})

        for pane_id in cat_def.get("panes", []):
            if pane_id not in seen_panes and pane_id in PANES:
                seen_panes.add(pane_id)
                pane_ids.append(pane_id)

        for cmd_id in cat_def.get("commands", []):
            if cmd_id not in seen_commands and cmd_id in COMMANDS:
                seen_commands.add(cmd_id)
                command_ids.append(cmd_id)

        for ep_id in cat_def.get("endpoints", []):
            if ep_id not in seen_endpoints and ep_id in ENDPOINTS:
                seen_endpoints.add(ep_id)
                endpoint_ids.append(ep_id)

    panes = [PANES[pid] for pid in pane_ids]
    commands = [{"name": cid, **COMMANDS[cid]} for cid in command_ids]
    endpoints = [{"name": eid, **ENDPOINTS[eid]} for eid in endpoint_ids]

    return {
        "panes": panes,
        "commands": commands,
        "endpoints": endpoints,
        "categories": sorted(categories),
    }


def check_commands(commands: list[dict]) -> tuple[list[dict], list[dict]]:
    """Check which command-line tools are installed.

    Runs the 'check' command for each tool (e.g. 'command -v jq').
    Returns (available, missing).
    """
    available = []
    missing = []

    for cmd in commands:
        check = cmd.get("check", "")
        if not check:
            available.append(cmd)
            continue
        try:
            result = subprocess.run(
                check, shell=True, capture_output=True, timeout=2,
            )
            if result.returncode == 0:
                available.append(cmd)
            else:
                missing.append(cmd)
        except (subprocess.TimeoutExpired, OSError):
            missing.append(cmd)

    return available, missing


def build_tools_summary(
    pane_status: dict[str, dict],
    available_commands: list[dict],
    endpoints: list[dict],
) -> str:
    """Build the enriched tool description for the LLM planner.

    Describes all three surfaces so the LLM knows:
    - Which panes exist for task routing
    - Which commands are available in any shell pane
    - Which APIs can be called via curl
    """
    sections = []

    # Panes — conversation channels the LLM can target subtasks to
    from prompts import load_driver_meta
    pane_lines = []
    for name, info in pane_status.items():
        if info["status"] == "ready":
            meta = load_driver_meta(info["app_type"])
            mode_hint = ""
            if meta.get("preferred_mode"):
                mode_hint = f" [prefer: {meta['preferred_mode']}"
                if meta.get("use_interactive_when"):
                    mode_hint += f" — interactive when: {meta['use_interactive_when']}"
                mode_hint += "]"
            pane_lines.append(
                f"  - {name} [{info['app_type']}]: {info['description']}{mode_hint}"
            )
    if pane_lines:
        sections.append(
            "PANES (each is an independent terminal — "
            "target subtasks to these):\n" + "\n".join(pane_lines)
        )

    # Commands — available in any shell-type pane
    if available_commands:
        cmd_lines = []
        for cmd in available_commands:
            cmd_lines.append(
                f"  - {cmd['name']}: {cmd['description']}. "
                f"Usage: {cmd['usage']}"
            )
        sections.append(
            "COMMANDS (run these in any shell-type pane):\n"
            + "\n".join(cmd_lines)
        )

    # Endpoints — curl from any pane, always available
    if endpoints:
        ep_lines = []
        for ep in endpoints:
            ep_lines.append(
                f"  - {ep['name']}: {ep['description']}. "
                f"Example: {ep['usage']}"
            )
        sections.append(
            "APIS (call via curl from any pane — no install needed):\n"
            + "\n".join(ep_lines)
        )

    if not sections:
        return "No tools available. Only basic shell commands can be used."
    return "\n\n".join(sections)


def print_availability(
    pane_status: dict[str, dict],
    available_commands: list[dict],
    missing_commands: list[dict],
    endpoints: list[dict],
    categories: list[str],
) -> None:
    """Print a startup status table showing what's available."""
    cat_str = ", ".join(categories)
    progress(f"  Categories: {cat_str}")
    progress("")

    # Commands
    if available_commands or missing_commands:
        progress("  Commands:")
        for cmd in available_commands:
            desc = cmd["description"].split(" — ")[0] if " — " in cmd["description"] else cmd["description"][:40]
            progress(f"    + {cmd['name']:20s} {desc}")
        for cmd in missing_commands:
            install = cmd.get("install", "")
            progress(f"    - {cmd['name']:20s} not found ({install})")
        progress("")

    # Endpoints
    if endpoints:
        progress(f"  APIs: {len(endpoints)} endpoints (always available)")
        progress("")


# ── Legacy API (backward compatible) ─────────────────────────────────────────

def get_toolset(name: str) -> list[dict]:
    """Return pane tool list for a named profile.

    Legacy API — use resolve_toolset() for the full three-surface model.
    """
    resolved = resolve_toolset(name)
    return resolved["panes"]


def list_toolsets() -> dict[str, list[str]]:
    """Return dict mapping profile names to their pane tool names."""
    result = {}
    for name in PROFILES:
        try:
            resolved = resolve_toolset(name)
            result[name] = [p["name"] for p in resolved["panes"]]
        except ValueError:
            result[name] = []
    return result


def normalize_tool_name(name: str) -> str:
    """Map classifier tool names to canonical pane/command names via aliases.

    E.g. "mail" → "email", "lynx" → "browser". Returns original if no match.
    """
    # Direct match
    if name in PANES or name in COMMANDS:
        return name
    # Check pane aliases
    for pane_id, pane_def in PANES.items():
        if name in pane_def.get("aliases", []):
            return pane_def["name"]
    return name


def find_category(tool_name: str) -> str | None:
    """Reverse lookup: find which category provides a tool (pane or command).

    Handles aliases: find_category("mail") → "comms" (via email pane alias).
    """
    canonical = normalize_tool_name(tool_name)
    for cat_name, cat_def in CATEGORIES.items():
        if canonical in cat_def.get("panes", []):
            return cat_name
        if canonical in cat_def.get("commands", []):
            return cat_name
    # Also check pane name field
    for cat_name, cat_def in CATEGORIES.items():
        for pane_id in cat_def.get("panes", []):
            pane_def = PANES.get(pane_id)
            if pane_def and pane_def["name"] == canonical:
                return cat_name
    return None


def list_categories() -> dict[str, dict]:
    """Return the category registry for display."""
    return dict(CATEGORIES)
