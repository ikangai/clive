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
    list_toolsets()          → dict         (legacy)
    DEFAULT_TOOLSET
"""

import subprocess

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
            "Uses framed conversational protocol (see protocol.py)."
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
        "card": (
            "[jq] JSON processor\n"
            "  jq 'FILTER' [file]   .key | .[] | select(c) | map(f) | length\n"
            "  curl -s api | jq -r '.data[].name'"
        ),
    },
    "rg": {
        "description": "ripgrep — fast recursive text search across files",
        "usage": "rg 'pattern' /path/to/search",
        "check": "command -v rg",
        "install": "brew install ripgrep",
        "category": "data",
        "card": (
            "[rg] ripgrep — fast recursive search\n"
            "  rg 'PATTERN' [path]   -i ignore-case  -l names-only  -n line#  -A/-B ctx\n"
            "  rg -tpy 'def foo' src/"
        ),
    },
    "mlr": {
        "description": "miller — CSV/JSON/TSV processing, spreadsheet-like ops in shell",
        "usage": "mlr --csv filter '$age > 30' then sort-by name data.csv",
        "check": "command -v mlr",
        "install": "brew install miller",
        "category": "data",
        "card": (
            "[mlr] miller — CSV/JSON/TSV ops\n"
            "  mlr --csv VERB then VERB file   filter cut sort-by stats1 cat\n"
            "  mlr --csv filter '$age>30' then sort-by name data.csv"
        ),
    },
    "sqlite3": {
        "description": "SQLite — full SQL database engine, single-file databases",
        "usage": "sqlite3 data.db 'SELECT * FROM users WHERE active=1'",
        "check": "command -v sqlite3",
        "install": "built-in on macOS",
        "category": "data",
        "card": (
            "[sqlite3] SQL on single-file DBs\n"
            "  sqlite3 FILE 'SQL'    .tables  .schema TBL  .mode csv  .import\n"
            "  sqlite3 data.db 'SELECT * FROM users WHERE active=1'"
        ),
    },

    # ── Docs ──
    "pandoc": {
        "description": "Universal document converter — markdown, PDF, Word, HTML, LaTeX",
        "usage": "pandoc report.md -o report.pdf",
        "check": "command -v pandoc",
        "install": "brew install pandoc",
        "category": "docs",
        "card": (
            "[pandoc] universal doc converter\n"
            "  pandoc IN -o OUT   -f/-t fmt (md html pdf docx tex)  --toc  -s standalone\n"
            "  pandoc report.md -o report.pdf"
        ),
    },
    "pdftotext": {
        "description": "Extract text content from PDF files",
        "usage": "pdftotext document.pdf - | head -50",
        "check": "command -v pdftotext",
        "install": "brew install poppler",
        "category": "docs",
        "card": (
            "[pdftotext] extract text from PDF\n"
            "  pdftotext IN.pdf [OUT|-]   -layout keep cols  -f/-l first/last page\n"
            "  pdftotext doc.pdf - | head -50"
        ),
    },

    # ── Images ──
    "convert": {
        "description": "ImageMagick — resize, convert, composite, annotate images",
        "usage": "convert input.png -resize 800x600 output.jpg",
        "check": "command -v convert",
        "install": "brew install imagemagick",
        "category": "images",
        "card": (
            "[convert] ImageMagick image ops\n"
            "  convert IN OUT   -resize WxH  -quality N  -crop  -rotate  -composite\n"
            "  convert in.png -resize 800x600 out.jpg"
        ),
    },
    "exiftool": {
        "description": "Read and write image/video metadata (EXIF, IPTC, XMP)",
        "usage": "exiftool -DateTimeOriginal -GPSPosition photo.jpg",
        "check": "command -v exiftool",
        "install": "brew install exiftool",
        "category": "images",
        "card": (
            "[exiftool] image/video metadata\n"
            "  exiftool [-TAG] FILE   -GPSPosition -DateTimeOriginal -all=  -overwrite_original\n"
            "  exiftool -DateTimeOriginal photo.jpg"
        ),
    },

    # ── Media ──
    "yt-dlp": {
        "description": "Download video/audio from YouTube and 1000+ sites",
        "usage": "yt-dlp -x --audio-format mp3 'https://youtube.com/watch?v=...'",
        "check": "command -v yt-dlp",
        "install": "brew install yt-dlp",
        "category": "media",
        "card": (
            "[yt-dlp] download video/audio (1000+ sites)\n"
            "  yt-dlp URL   -x audio-only  --audio-format mp3  -f best  --write-sub  -o TMPL\n"
            "  yt-dlp -x --audio-format mp3 URL"
        ),
    },
    "whisper": {
        "description": "OpenAI Whisper — local speech-to-text transcription",
        "usage": "whisper audio.mp3 --model small --output_format txt",
        "check": "command -v whisper",
        "install": "pip install openai-whisper",
        "category": "media",
        "card": (
            "[whisper] local speech-to-text\n"
            "  whisper AUDIO   --model tiny|base|small|medium  --output_format txt|srt|json\n"
            "  whisper audio.mp3 --model small --output_format txt"
        ),
    },
    "ffmpeg": {
        "description": "Audio/video converter, processor, and stream handler",
        "usage": "ffmpeg -i video.mp4 -vn -acodec mp3 audio.mp3",
        "check": "command -v ffmpeg",
        "install": "brew install ffmpeg",
        "category": "media",
        "card": (
            "[ffmpeg] audio/video convert/process\n"
            "  ffmpeg -i IN [opts] OUT   -vn no-video  -ss/-to trim  -c:a/-c:v codec  -vf filter\n"
            "  ffmpeg -i in.mp4 -vn -acodec mp3 out.mp3"
        ),
    },

    # ── Comms ──
    "icalBuddy": {
        "description": "macOS calendar access — today's events, upcoming schedule",
        "usage": "icalBuddy -f eventsToday; icalBuddy eventsToday+7",
        "check": "command -v icalBuddy",
        "install": "brew install ical-buddy",
        "category": "comms",
        "card": (
            "[icalBuddy] macOS calendar query\n"
            "  icalBuddy [-f] EVENTS   eventsToday  eventsToday+N  eventsFrom:DATE to:DATE\n"
            "  icalBuddy -f eventsToday+7"
        ),
    },
    "khard": {
        "description": "CLI contacts manager — CardDAV compatible address book",
        "usage": "khard list; khard show 'John Doe'",
        "check": "command -v khard",
        "install": "pip install khard",
        "category": "comms",
        "card": (
            "[khard] CLI contacts (CardDAV)\n"
            "  khard CMD   list  show NAME  add  edit  email NAME  phone NAME\n"
            "  khard show 'John Doe'"
        ),
    },
    "terminal-notifier": {
        "description": "macOS native desktop notifications from CLI",
        "usage": "terminal-notifier -message 'Task done!' -title 'clive'",
        "check": "command -v terminal-notifier",
        "install": "brew install terminal-notifier",
        "category": "comms",
        "card": (
            "[terminal-notifier] macOS desktop notification\n"
            "  terminal-notifier -message MSG [-title T] [-subtitle S] [-sound NAME] [-open URL]\n"
            "  terminal-notifier -message 'Done' -title 'clive'"
        ),
    },

    # ── Productivity ──
    "task": {
        "description": "Taskwarrior — full CLI task management with filters and reports",
        "usage": "task list; task add 'Review PR' project:work due:tomorrow; task 1 done",
        "check": "command -v task",
        "install": "brew install task",
        "category": "productivity",
        "card": (
            "[task] Taskwarrior task mgmt\n"
            "  task CMD   list  add DESC project:P due:D  ID done  ID modify  next\n"
            "  task add 'Review PR' project:work due:tomorrow"
        ),
    },
    "watson": {
        "description": "Time tracker — log hours per project from the terminal",
        "usage": "watson start myproject; watson stop; watson report",
        "check": "command -v watson",
        "install": "brew install watson",
        "category": "productivity",
        "card": (
            "[watson] CLI time tracker\n"
            "  watson CMD   start PROJ  stop  status  log  report  cancel\n"
            "  watson start myproject"
        ),
    },
    "nb": {
        "description": "CLI notebook — notes, bookmarks, todos, wiki in plain text",
        "usage": "nb add 'Meeting notes...'; nb list; nb search 'quarterly'",
        "check": "command -v nb",
        "install": "brew install nb",
        "category": "productivity",
        "card": (
            "[nb] CLI notes/bookmarks/todos\n"
            "  nb CMD   add TEXT  list  search Q  edit ID  bookmark URL  todo do ID\n"
            "  nb add 'Meeting notes...'"
        ),
    },

    # ── Finance ──
    "hledger": {
        "description": "Plain text accounting — double-entry bookkeeping, budgets, reports",
        "usage": "hledger -f journal.txt balance; hledger register expenses",
        "check": "command -v hledger",
        "install": "brew install hledger",
        "category": "finance",
        "card": (
            "[hledger] plain text accounting\n"
            "  hledger [-f FILE] CMD   balance  register  incomestatement  print  accounts\n"
            "  hledger -f journal.txt balance"
        ),
    },

    # ── Social ──
    "toot": {
        "description": "Mastodon CLI — post, read timeline, interact with fediverse",
        "usage": "toot post 'Hello fediverse!'; toot timeline",
        "check": "command -v toot",
        "install": "pip install toot",
        "category": "social",
        "card": (
            "[toot] Mastodon/fediverse CLI\n"
            "  toot CMD   post TEXT  timeline  search Q  follow USER  whois USER\n"
            "  toot post 'Hello fediverse!'"
        ),
    },

    # ── Translation ──
    "trans": {
        "description": "Translate text between languages via Google/Bing/Yandex",
        "usage": "trans en:de 'hello world'; echo 'bonjour' | trans :en",
        "check": "command -v trans",
        "install": "brew install translate-shell",
        "category": "translation",
        "card": (
            "[trans] translate-shell\n"
            "  trans [SRC:DST] TEXT   :en auto→en  en:de  -b brief  -d dict mode\n"
            "  trans en:de 'hello world'"
        ),
    },

    # ── Search ──
    "ddgr": {
        "description": "DuckDuckGo search from the terminal — web search without a browser",
        "usage": "ddgr --np 'python asyncio tutorial'",
        "check": "command -v ddgr",
        "install": "brew install ddgr",
        "category": "search",
        "card": (
            "[ddgr] DuckDuckGo terminal search\n"
            "  ddgr [opts] QUERY   --np no-prompt  -n N results  -j json  -r REGION\n"
            "  ddgr --np 'python asyncio tutorial'"
        ),
    },

    # ── Web ──
    "monolith": {
        "description": "Save complete web pages as single self-contained HTML files",
        "usage": "monolith https://example.com/article -o saved.html",
        "check": "command -v monolith",
        "install": "brew install monolith",
        "category": "web",
        "card": (
            "[monolith] save self-contained HTML\n"
            "  monolith URL -o FILE   -j no-js  -i no-img  -a no-audio  -F frames\n"
            "  monolith https://ex.com/a -o saved.html"
        ),
    },

    # ── Dev ──
    "gh": {
        "description": "GitHub CLI — PRs, issues, CI checks, releases, repo management",
        "usage": "gh pr list; gh issue create --title 'Bug'; gh run view",
        "check": "command -v gh",
        "install": "brew install gh",
        "category": "dev",
        "card": (
            "[gh] GitHub CLI\n"
            "  gh OBJ CMD   pr list|view|create|merge  issue list|create  run view  repo clone  api PATH\n"
            "  gh pr create --title T --body B"
        ),
    },

    # ── Voice ──
    "sox": {
        "description": "Record audio from microphone — sound processing toolkit",
        "usage": "sox -d recording.wav trim 0 30  # record 30 seconds",
        "check": "command -v sox",
        "install": "brew install sox",
        "category": "voice",
        "card": (
            "[sox] record/process audio\n"
            "  sox [-d|IN] OUT [effects]   -d default input  trim S DUR  rate  vol  norm\n"
            "  sox -d rec.wav trim 0 30"
        ),
    },
    "say": {
        "description": "macOS text-to-speech — speak text aloud",
        "usage": "say 'Task complete'; echo 'hello' | say",
        "check": "command -v say",
        "install": "built-in on macOS",
        "category": "voice",
        "card": (
            "[say] macOS text-to-speech\n"
            "  say 'TEXT'   -v VOICE  -r RATE  -o FILE.aiff   echo X | say\n"
            "  say 'Task complete'"
        ),
    },

    # ── AI ──
    "claude": {
        "description": "Claude API wrapper — summarize, translate, generate documents",
        "usage": "cat notes.txt | bash tools/claude.sh 'write a summary'",
        "check": "test -x tools/claude.sh",
        "install": "included in clive (set ANTHROPIC_API_KEY)",
        "category": "ai",
        "card": (
            "[claude] LLM sub-task helper\n"
            "  cat IN | bash tools/claude.sh 'INSTRUCTION'\n"
            "  cat notes.txt | bash tools/claude.sh 'write a summary'"
        ),
    },

    # ── Sync ──
    "rclone": {
        "description": "Sync files to/from cloud storage (S3, Drive, Dropbox, 50+ backends)",
        "usage": "rclone sync /local/path remote:bucket/path",
        "check": "command -v rclone",
        "install": "brew install rclone",
        "category": "sync",
        "card": (
            "[rclone] cloud storage sync\n"
            "  rclone CMD SRC DST   copy sync ls lsd mkdir delete  -n dry-run  -P progress\n"
            "  rclone sync /local remote:bucket/path"
        ),
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


def build_tier0_summary(active_categories: list[str]) -> str:
    """Tier 0: category index with tool counts. ~100 tokens.

    The planner sees what *kinds* of tools exist, not every individual tool.
    Combined with Tier 1 (`build_tier1_names`) for categories the planner picks.
    """
    parts = []
    for cat in active_categories:
        cat_def = CATEGORIES.get(cat)
        if not cat_def:
            continue
        count = (len(cat_def.get("commands", []))
                 + len(cat_def.get("panes", []))
                 + len(cat_def.get("endpoints", [])))
        parts.append(f"{cat}({count})")
    if not parts:
        return ""
    listing = ", ".join(parts)
    return (
        f"Tool categories available: {listing}\n"
        "Use `tool_info <name>` for details on a specific tool."
    )


def build_tier1_names(categories: list[str]) -> str:
    """Tier 1: tool names per category, no descriptions. ~50 tokens/category.

    Duplicate categories produce duplicate lines; callers should dedupe.
    """
    lines = []
    for cat in categories:
        cat_def = CATEGORIES.get(cat)
        if not cat_def:
            continue
        names = []
        names.extend(cat_def.get("panes", []))
        names.extend(cat_def.get("commands", []))
        names.extend(cat_def.get("endpoints", []))
        if names:
            lines.append(f"{cat}: {', '.join(names)}")
    return "\n".join(lines)


def build_tier2_card(name: str) -> str | None:
    """Tier 2: compact reference card for a single tool. ~150 tokens.

    Returns None if the tool isn't known. Resolves COMMAND aliases via
    `normalize_tool_name`. Panes synthesize a card from their definition.
    """
    canonical = normalize_tool_name(name)
    if canonical in COMMANDS:
        return COMMANDS[canonical].get("card")
    if canonical in PANES:
        pane = PANES[canonical]
        return f"[{canonical}] {pane.get('description', '').strip()}"
    return None


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


# Iteration order = tie-break priority on equal scores. Don't reshuffle silently.
# Category keyword hints for `classify_tool_to_category`.
# Conservative — only obvious words. Returns None on miss rather than guess.
_CATEGORY_KEYWORDS = {
    "data":   ["json", "csv", "tsv", "xml", "yaml", "parse", "query",
               "filter", "transform", "sql", "database"],
    "web":    ["http", "curl", "url", "web", "html", "browser",
               "scrape", "rest", "api client"],
    "docs":   ["pdf", "markdown", "document", "convert", "doc ", "latex"],
    "media":  ["video", "audio", "youtube", "podcast", "transcribe",
               "ffmpeg", "stream"],
    "images": ["image", "png", "jpeg", "jpg", "photo", "exif", "gif"],
    "comms":  ["email", "calendar", "contact", "notification", "chat",
               "message"],
    "dev":    [" git ", "github", "pull request", "issue", "commit",
               "diff", "code"],
    "search": ["search engine", "google", "duckduckgo", "bing"],
    "ai":     ["llm", "openai", "anthropic", "claude", "gpt", "summariz",
               "language model"],
    "voice":  ["microphone", "speech", "speak", "audio record",
               "text-to-speech", "tts"],
    "sync":   ["s3", "rclone", "cloud storage", "dropbox", "sync"],
    "core":   ["filesystem", "directory", " cd ", "shell", "navigate"],
}


def classify_tool_to_category(name: str, description: str) -> str | None:
    """Best-effort classify an unknown tool into an existing category.

    Used by gh#41 Phase 1 auto-explore to surface a newly generated
    driver into a toolset entry. Returns None when no keyword matches.
    Conservative on purpose: a wrong category bucket is worse than no
    bucket (auto-explore can fall back to core).
    """
    haystack = f"{name} {description}".lower()
    matches: dict[str, int] = {}
    for cat, keywords in _CATEGORY_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in haystack)
        if score:
            matches[cat] = score
    if not matches:
        return None
    return max(matches, key=matches.get)
