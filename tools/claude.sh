#!/usr/bin/env bash
# claude.sh — thin wrapper around the Anthropic Messages API
#
# Usage:
#   bash tools/claude.sh "your prompt here"
#   echo "context text" | bash tools/claude.sh "summarize this"
#
# Environment:
#   ANTHROPIC_API_KEY — required
#
# Requirements: curl, jq

set -euo pipefail

MODEL="${CLAUDE_MODEL:-claude-sonnet-4-20250514}"

usage() {
    echo "Usage: bash tools/claude.sh <prompt>"
    echo "       echo 'input' | bash tools/claude.sh <prompt>"
    echo ""
    echo "Environment:"
    echo "  ANTHROPIC_API_KEY   Required. Your Anthropic API key."
    echo "  CLAUDE_MODEL        Optional. Default: ${MODEL}"
    echo ""
    echo "Examples:"
    echo "  bash tools/claude.sh 'What is the capital of France?'"
    echo "  cat document.txt | bash tools/claude.sh 'Summarize this document'"
    exit 1
}

[ $# -lt 1 ] && usage

if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
    echo "Error: ANTHROPIC_API_KEY not set"
    exit 1
fi

PROMPT="$1"

# Read stdin if available (non-interactive)
STDIN_CONTENT=""
if [ ! -t 0 ]; then
    STDIN_CONTENT=$(cat)
fi

# Build the user message
if [ -n "$STDIN_CONTENT" ]; then
    USER_MSG="${PROMPT}\n\n--- Input ---\n${STDIN_CONTENT}"
else
    USER_MSG="${PROMPT}"
fi

# Escape for JSON
USER_MSG_JSON=$(printf '%s' "$USER_MSG" | jq -Rs .)

RESPONSE=$(curl -s https://api.anthropic.com/v1/messages \
    -H "content-type: application/json" \
    -H "x-api-key: ${ANTHROPIC_API_KEY}" \
    -H "anthropic-version: 2023-06-01" \
    -d "{
        \"model\": \"${MODEL}\",
        \"max_tokens\": 4096,
        \"messages\": [{\"role\": \"user\", \"content\": ${USER_MSG_JSON}}]
    }")

# Extract text content, handle errors
ERROR=$(echo "$RESPONSE" | jq -r '.error.message // empty' 2>/dev/null)
if [ -n "$ERROR" ]; then
    echo "API Error: $ERROR"
    exit 1
fi

echo "$RESPONSE" | jq -r '.content[0].text'
