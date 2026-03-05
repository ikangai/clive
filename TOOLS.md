# Tool catalog

clive uses the CLI as its universal agent interface. The LLM reads the terminal screen, reasons about what it sees, and types commands — exactly like a human at a terminal. No structured APIs, no MCP servers, no protocol adapters. Just text in, text out.

## Three surfaces

Every tool falls into one of three surfaces:

| Surface | What it is | Creates panes? | How the LLM uses it |
|---------|-----------|----------------|---------------------|
| **Panes** | Conversation channels (tmux windows) | Yes | Reads screen, types commands |
| **Commands** | CLI tools in any shell pane | No | Runs command, reads output on screen |
| **Endpoints** | APIs via curl | No | `curl` in any pane, reads response |

Panes provide parallel execution lanes. Commands and endpoints add capabilities without creating new panes. A `full` profile has 30+ tools but only 6 panes.

## Composable profiles

Select a profile with `--toolset` (or `-t`). Compose categories with `+`:

```bash
python clive.py -t standard                    # named profile
python clive.py -t web+comms+data              # compose categories
python clive.py -t standard+media+ai           # profile + extras
python clive.py --list-toolsets                 # show all profiles & categories
python clive.py --list-tools                    # show all tools with install status
```

## Profiles

| Profile | Categories | Panes |
|---------|-----------|-------|
| **minimal** (default) | core | shell |
| **standard** | core, web, data, docs, info | shell, browser, data, docs |
| **full** | core, web, data, docs, comms, media, productivity, search, info | shell, browser, data, docs, email, media |
| **research** | core, web, data, docs, media, search, info, ai | shell, browser, data, docs, media |
| **business** | core, web, data, docs, comms, productivity, finance, info | shell, browser, data, docs, email |
| **creative** | core, web, media, images, ai, translation | shell, browser, media |
| **remote** | core, comms, remote | shell, email, browser (remote), files (remote) |

## Categories

Categories group related tools. Compose freely with `+`:

| Category | Panes | Commands | Endpoints |
|----------|-------|----------|-----------|
| **core** | shell | — | — |
| **web** | browser | monolith | — |
| **data** | data | jq, rg, mlr, sqlite3 | — |
| **docs** | docs | pandoc, pdftotext | — |
| **comms** | email | icalBuddy, khard, terminal-notifier | — |
| **media** | media | yt-dlp, whisper, ffmpeg | — |
| **productivity** | — | task, watson, nb | — |
| **finance** | — | hledger | — |
| **social** | — | toot | — |
| **translation** | — | trans | — |
| **search** | — | ddgr | — |
| **images** | — | convert, exiftool | — |
| **dev** | — | gh | — |
| **voice** | — | sox, say | — |
| **ai** | — | claude (tools/claude.sh) | — |
| **sync** | — | rclone | — |
| **info** | — | — | weather, hackernews, exchange, github_api |

## Auto-detection

At startup, clive checks which commands are installed and only tells the LLM about available ones. Missing tools show a warning with install instructions:

```
  Commands:
    + jq                   JSON processor
    + rg                   ripgrep
    - mlr                  not found (brew install miller)
    + sqlite3              SQLite
```

## Installing tools

**Standard profile deps:**
```bash
brew install lynx ripgrep pandoc miller poppler jq
```

**Full profile deps:**
```bash
brew install lynx ripgrep pandoc miller poppler jq neomutt ical-buddy task
pip install openai-whisper yt-dlp
```

**Additional categories:**
```bash
# images
brew install imagemagick exiftool

# dev
brew install gh

# search
brew install ddgr

# translation
brew install translate-shell

# social
pip install toot

# finance
brew install hledger

# productivity
brew install task watson nb

# voice
brew install sox
# say is built-in on macOS

# sync
brew install rclone

# ai — included in clive, just set ANTHROPIC_API_KEY

# media
brew install yt-dlp ffmpeg
pip install openai-whisper
```

## Helper scripts

Located in `tools/`. Used by the **media** pane but can be run from any shell pane.

### tools/youtube.sh

```bash
bash tools/youtube.sh list https://www.youtube.com/@channel     # list videos
bash tools/youtube.sh captions https://youtube.com/watch?v=ID   # get captions (fast)
bash tools/youtube.sh get https://youtube.com/watch?v=ID        # download audio
bash tools/youtube.sh transcribe /tmp/clive/video.mp3           # transcribe
```

### tools/podcast.sh

```bash
bash tools/podcast.sh list https://feeds.example.com/podcast.xml  # list episodes
bash tools/podcast.sh get https://example.com/episode.mp3         # download
bash tools/podcast.sh transcribe /tmp/clive/episode.mp3           # transcribe
```

### tools/claude.sh

Claude API wrapper for sub-tasks (summarize, translate, generate). Requires `ANTHROPIC_API_KEY`.

```bash
bash tools/claude.sh "What is the capital of France?"
cat document.txt | bash tools/claude.sh "Summarize this document"
```

## Custom toolsets

### Adding a command tool

Edit `toolsets.py` — add to `COMMANDS` dict and its category:

```python
COMMANDS["mytool"] = {
    "description": "What it does",
    "usage": "mytool --flag input.txt",
    "check": "command -v mytool",
    "install": "brew install mytool",
    "category": "mycategory",
}
```

### Adding a pane tool

For interactive tools that need their own conversation channel:

```python
PANES["mytui"] = {
    "name": "mytui",
    "cmd": None,           # startup command (None = plain shell)
    "app_type": "mytui",
    "description": "What the LLM can do in this pane",
    "host": None,           # SSH target for remote panes
    "category": "mycategory",
}
```

### Adding an endpoint

```python
ENDPOINTS["myapi"] = {
    "description": "What this API provides",
    "usage": "curl -s 'https://api.example.com/data' | jq .",
    "category": "info",
}
```

### Adding a category

```python
CATEGORIES["mycategory"] = {
    "panes": ["mytui"],        # pane IDs from PANES dict
    "commands": ["mytool"],     # command IDs from COMMANDS dict
    "endpoints": ["myapi"],     # endpoint IDs from ENDPOINTS dict
}
```

Then add to a profile or use directly: `-t standard+mycategory`

## TUI slash commands

The TUI (`clive --tui` or `clive-tui`) provides interactive configuration via slash commands. These are not tools — they configure the agent or control execution.

| Command | Arguments | Description |
|---------|-----------|-------------|
| `/profile` | `<name\|+cat>` | Switch toolset profile or add a category (e.g., `/profile standard`, `/profile +media`) |
| `/provider` | `<name>` | Switch LLM provider (e.g., `/provider anthropic`) |
| `/model` | `<name>` | Switch model (e.g., `/model gpt-4o`) |
| `/tools` | — | Show available and missing tools for the current profile |
| `/install` | — | Install missing CLI tools (brew/pip) |
| `/status` | — | Show running tasks with elapsed time and token counts |
| `/cancel` | — | Cancel all running tasks |
| `/clear` | — | Clear the output screen |
| `/selfmod` | `<goal>` | Self-modify clive (experimental, requires `CLIVE_EXPERIMENTAL_SELFMOD=1`) |
| `/undo` | — | Roll back the last self-modification |
| `/safe-mode` | — | Disable self-modification for the current session |
| `/help` | — | Show help with all commands, profiles, categories, and providers |

## CLI flags

```
python clive.py [OPTIONS] [TASK]

Options:
  -t, --toolset SPEC     Toolset profile or category combo (default: minimal)
  --list-toolsets        List available profiles and categories
  --list-tools           List all tools across all surfaces
  --tui                  Launch the interactive TUI
  --selfmod GOAL         Self-modify clive (experimental)
  --undo                 Roll back last self-modification
  --safe-mode            Disable self-modification for this run
```

## The philosophy

The CLI is the universal agent interface. Instead of building MCP servers, REST adapters, or protocol bridges, clive talks to tools the same way a human does — by reading the terminal and typing commands.

This means:
- **Any CLI tool works.** No wrapper needed. If it has a terminal interface, the agent can use it.
- **Local and remote are the same.** SSH into a server and the loop continues. Same read-think-write cycle.
- **Tool updates don't break anything.** The LLM adapts by reading the new output, like a human would.
- **Agent-to-agent communication works.** If another agent has a CLI, clive can interact with it through a pane.
- **Google Workspace CLI, AWS CLI, database CLIs** — all "just work" without any integration code.
