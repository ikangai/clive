#!/usr/bin/env bash
# podcast.sh — fetch and transcribe podcast episodes
#
# Subcommands:
#   list  <rss_url>       List episodes (title + enclosure URL)
#   get   <episode_url>   Download episode audio to /tmp/clive/
#   transcribe <file>     Transcribe audio file using whisper
#
# Requirements: curl, xmllint (libxml2), whisper (openai-whisper)

set -euo pipefail

OUTDIR="/tmp/clive"

usage() {
    echo "Usage: bash tools/podcast.sh <command> [args]"
    echo ""
    echo "Commands:"
    echo "  list  <rss_url>       List episodes from RSS feed"
    echo "  get   <episode_url>   Download episode audio"
    echo "  transcribe <file>     Transcribe audio with whisper"
    echo ""
    echo "Examples:"
    echo "  bash tools/podcast.sh list https://feeds.example.com/podcast.xml"
    echo "  bash tools/podcast.sh get https://example.com/episode.mp3"
    echo "  bash tools/podcast.sh transcribe /tmp/clive/episode.mp3"
    exit 1
}

cmd_list() {
    local rss_url="$1"
    echo "Fetching RSS feed..."
    local feed
    feed=$(curl -sL "$rss_url")

    # Extract title + enclosure URL pairs using sed (POSIX-compatible)
    echo "$feed" | xmllint --xpath '//item' - 2>/dev/null | \
        sed -n '
            s/.*<title>\([^<]*\)<\/title>.*/TITLE: \1/p
            s/.*enclosure.*url="\([^"]*\)".*/  \1/p
        ' || {
        # Fallback: just list titles
        echo "$feed" | sed -n 's/.*<title>\([^<]*\)<\/title>.*/\1/p' | head -20
        echo ""
        echo "(enclosure URLs not parsed — feed may use non-standard format)"
    }
}

cmd_get() {
    local url="$1"
    local filename
    filename=$(basename "$url" | sed 's/?.*//')
    local outpath="${OUTDIR}/${filename}"

    mkdir -p "$OUTDIR"
    echo "Downloading: $url"
    echo "       To: $outpath"
    curl -L -o "$outpath" "$url"
    echo "Done: $outpath ($(du -h "$outpath" | cut -f1))"
}

cmd_transcribe() {
    local file="$1"
    if [ ! -f "$file" ]; then
        echo "Error: file not found: $file"
        exit 1
    fi

    local outbase="${file%.*}"
    echo "Transcribing: $file"
    echo "This may take a while for long episodes..."
    whisper "$file" --model base --output_format txt --output_dir "$(dirname "$file")"
    echo "Done: ${outbase}.txt"
    echo ""
    echo "--- First 20 lines ---"
    head -20 "${outbase}.txt"
}

# ── Main ─────────────────────────────────────────────────────────────────────

[ $# -lt 1 ] && usage

case "$1" in
    list)
        [ $# -lt 2 ] && { echo "Error: list requires an RSS URL"; exit 1; }
        cmd_list "$2"
        ;;
    get)
        [ $# -lt 2 ] && { echo "Error: get requires an episode URL"; exit 1; }
        cmd_get "$2"
        ;;
    transcribe)
        [ $# -lt 2 ] && { echo "Error: transcribe requires a file path"; exit 1; }
        cmd_transcribe "$2"
        ;;
    *)
        echo "Unknown command: $1"
        usage
        ;;
esac
