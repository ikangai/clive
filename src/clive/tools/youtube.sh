#!/usr/bin/env bash
# youtube.sh — fetch and transcribe YouTube content
#
# Subcommands:
#   list    <channel_url>   List recent videos from a channel
#   get     <url>           Download video audio to /tmp/clive/
#   captions <url>          Fetch auto-captions (fast, no download needed)
#   transcribe <file>       Transcribe audio file using whisper
#
# Requirements: yt-dlp, whisper (openai-whisper), curl, jq

set -euo pipefail

OUTDIR="/tmp/clive"

usage() {
    echo "Usage: bash tools/youtube.sh <command> [args]"
    echo ""
    echo "Commands:"
    echo "  list    <channel_url>   List recent videos from a channel"
    echo "  get     <url>           Download video audio"
    echo "  captions <url>          Fetch captions (fast path — no download)"
    echo "  transcribe <file>       Transcribe audio with whisper"
    echo ""
    echo "Examples:"
    echo "  bash tools/youtube.sh list https://www.youtube.com/@channel"
    echo "  bash tools/youtube.sh captions https://www.youtube.com/watch?v=VIDEO_ID"
    echo "  bash tools/youtube.sh get https://www.youtube.com/watch?v=VIDEO_ID"
    echo "  bash tools/youtube.sh transcribe /tmp/clive/video.mp3"
    exit 1
}

cmd_list() {
    local url="$1"
    echo "Listing recent videos from: $url"
    yt-dlp --flat-playlist --print "%(id)s  %(title)s" "$url" 2>/dev/null | head -20
}

cmd_get() {
    local url="$1"
    mkdir -p "$OUTDIR"

    echo "Downloading audio: $url"
    yt-dlp \
        -x --audio-format mp3 \
        -o "${OUTDIR}/%(title)s.%(ext)s" \
        "$url"

    echo ""
    echo "Downloaded to ${OUTDIR}/"
    ls -lh "${OUTDIR}"/*.mp3 2>/dev/null | tail -1
}

cmd_captions() {
    local url="$1"
    mkdir -p "$OUTDIR"

    echo "Fetching captions for: $url"

    # Try auto-generated captions first, then manual subs
    yt-dlp \
        --write-auto-sub --sub-lang en \
        --skip-download \
        --convert-subs srt \
        -o "${OUTDIR}/%(title)s" \
        "$url" 2>/dev/null

    local srt_file
    srt_file=$(ls -t "${OUTDIR}"/*.srt 2>/dev/null | head -1)

    if [ -z "$srt_file" ]; then
        echo "No captions available. Use 'get' + 'transcribe' instead."
        exit 1
    fi

    # Strip SRT timing lines, deduplicate, produce clean text
    local txt_file="${srt_file%.srt}.txt"
    grep -v '^[0-9]' "$srt_file" | grep -v '^\s*$' | grep -v -- '-->' | \
        awk '!seen[$0]++' > "$txt_file"

    echo "Captions saved to: $txt_file"
    echo ""
    echo "--- First 20 lines ---"
    head -20 "$txt_file"
    echo ""
    echo "Total lines: $(wc -l < "$txt_file")"
}

cmd_transcribe() {
    local file="$1"
    if [ ! -f "$file" ]; then
        echo "Error: file not found: $file"
        exit 1
    fi

    local outbase="${file%.*}"
    echo "Transcribing: $file"
    echo "This may take a while..."
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
        [ $# -lt 2 ] && { echo "Error: list requires a channel URL"; exit 1; }
        cmd_list "$2"
        ;;
    get)
        [ $# -lt 2 ] && { echo "Error: get requires a video URL"; exit 1; }
        cmd_get "$2"
        ;;
    captions)
        [ $# -lt 2 ] && { echo "Error: captions requires a video URL"; exit 1; }
        cmd_captions "$2"
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
