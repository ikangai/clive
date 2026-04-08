#!/usr/bin/env bash
# Test two Clive instances talking via clive@localhost addressing.
# Usage: bash tests/test_loopback.sh
#
# Observe:
#   Terminal 1: this script (shows outer Clive output)
#   Terminal 2: tmux attach -t clive   (watch panes live)
#   Logs:       tail -f /tmp/clive/*/clive.log

set -euo pipefail
cd "$(dirname "$0")/.."

echo "=== Clive Loopback Test (clive@localhost addressing) ==="
echo ""
echo "To observe live, open another terminal and run:"
echo "  tmux attach -t clive"
echo ""
echo "Starting outer Clive..."
echo ""

python3 clive.py \
    --debug \
    --max-tokens 30000 \
    "clive@localhost read https://news.ycombinator.com and give me a summary on anthropic mythos"
