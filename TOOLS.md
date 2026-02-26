# Tool catalog

clive uses **toolset profiles** to configure which tmux panes the agent gets. Select a profile with `--toolset` (or `-t`):

```bash
python clive.py -t standard "browse example.com and summarize it"
python clive.py --list-toolsets
```

## Quick reference

| Tool | Panes | Install requirements |
|---|---|---|
| **minimal** | shell | None (just bash) |
| **standard** | shell, browser, data, docs | lynx, ripgrep, pandoc, miller, poppler |
| **full** | shell, browser, data, docs, email, calendar, tasks, media | standard + neomutt, icalBuddy, taskwarrior, whisper, yt-dlp, jq |
| **remote** | shell, browser (remote), files (remote), email | SSH key + remote host configured |

## Profiles

### minimal (default)

Zero install. Just a shell pane.

```bash
python clive.py "list files in /tmp and summarize"
```

**Panes:**
- **shell** — local bash for filesystem ops, scripting, general commands

### standard

Good for research and data tasks. Install dependencies:

```bash
brew install lynx ripgrep pandoc miller poppler
# or: apt install lynx ripgrep pandoc miller poppler-utils
```

**Panes:**
- **shell** — local bash
- **browser** — web browsing with `lynx -dump`, `curl -s`, `wget`
- **data** — data processing with `rg`, `mlr` (miller), `jq`, `pdftotext`
- **docs** — documentation with `pandoc`, `pdftotext`, `lynx -dump`

### full

Everything in standard plus productivity tools and media processing.

```bash
# standard deps +
brew install neomutt icalbuddy jq
pip install openai-whisper
pip install yt-dlp
# taskwarrior: brew install task
```

**Panes:**
- All standard panes, plus:
- **email** — `neomutt`, `fetch_emails.sh`, `send_reply.sh`
- **calendar** — `icalBuddy eventsToday`, `icalBuddy eventsToday+7`
- **tasks** — `task list`, `task add`, `task done` (taskwarrior)
- **media** — YouTube, podcast, and Claude API helper scripts (see below)

### remote

For driving tools on a remote server over SSH. Requires:

1. SSH key at `~/.ssh/agent_key`
2. Remote host configured (edit `TOOL_REMOTE_BROWSER` and `TOOL_REMOTE_FILES` in `toolsets.py`)

**Panes:**
- **shell** — local bash (for scp, local work)
- **browser** — remote shell with `lynx -dump`, `head`, `grep`
- **files** — remote filesystem, write to `~/files/`
- **email** — local email access

## Helper scripts

Located in `tools/`. Used by the **media** pane in the `full` profile, but can be run from any shell pane.

### tools/youtube.sh

Fetch and transcribe YouTube content. The **captions** fast path is the key feature — most videos have auto-captions, so you get text without downloading or running whisper.

```bash
bash tools/youtube.sh list https://www.youtube.com/@channel
bash tools/youtube.sh captions https://www.youtube.com/watch?v=VIDEO_ID
bash tools/youtube.sh get https://www.youtube.com/watch?v=VIDEO_ID
bash tools/youtube.sh transcribe /tmp/clive/video.mp3
```

| Subcommand | What it does | Requirements |
|---|---|---|
| `list` | List recent videos from channel | yt-dlp |
| `captions` | Fetch auto-captions as text (fast) | yt-dlp |
| `get` | Download audio as mp3 | yt-dlp |
| `transcribe` | Transcribe audio file | whisper |

### tools/podcast.sh

Fetch and transcribe podcast episodes from RSS feeds.

```bash
bash tools/podcast.sh list https://feeds.example.com/podcast.xml
bash tools/podcast.sh get https://example.com/episode.mp3
bash tools/podcast.sh transcribe /tmp/clive/episode.mp3
```

| Subcommand | What it does | Requirements |
|---|---|---|
| `list` | List episodes from RSS feed | curl, xmllint |
| `get` | Download episode audio | curl |
| `transcribe` | Transcribe audio file | whisper |

### tools/claude.sh

Thin wrapper around the Anthropic Messages API. Useful for sub-tasks where the agent needs a separate LLM call (summarization, translation, analysis).

```bash
bash tools/claude.sh "What is the capital of France?"
cat document.txt | bash tools/claude.sh "Summarize this document"
```

| Environment variable | Description |
|---|---|
| `ANTHROPIC_API_KEY` | Required. Your Anthropic API key. |
| `CLAUDE_MODEL` | Optional. Default: `claude-sonnet-4-20250514` |

## Custom profiles

To create a custom profile, edit `toolsets.py`:

```python
# Add a new tool
TOOL_MY_TOOL = {
    "name": "my_tool",
    "cmd": None,              # None = plain shell pane
    "app_type": "my_type",    # metadata tag for the LLM
    "description": "...",     # tells the LLM what this pane is for
    "host": None,             # set to SSH target for remote tools
}

# Add a new profile
TOOLSETS["custom"] = [TOOL_SHELL, TOOL_MY_TOOL]
```

Then use it:

```bash
python clive.py -t custom "your task"
```

The description is what matters — it tells the LLM what the pane is for and which commands are available. The `cmd` field is only needed for tools that require a startup command (like SSH connections).
