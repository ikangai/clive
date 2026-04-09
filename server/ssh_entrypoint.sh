#!/bin/bash
# server/ssh_entrypoint.sh — SSH ForceCommand for clive-as-a-service
#
# Invoked by sshd when a remote user connects. Enqueues the task into
# the job queue and polls for the result.
#
# Usage (from client): ssh clive@host 'your task here'

set -euo pipefail

TASK="${SSH_ORIGINAL_COMMAND:-}"
if [ -z "$TASK" ]; then
    echo "Usage: ssh clive@host 'your task here'"
    echo ""
    echo "Examples:"
    echo "  ssh clive@host 'list files in /tmp'"
    echo "  ssh clive@host 'check disk usage'"
    exit 1
fi

CLIVE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
QUEUE_DIR="${CLIVE_QUEUE_DIR:-$HOME/.clive/queue}"
TOOLSET="${CLIVE_TOOLSET:-minimal}"
USER="${USER:-$(whoami)}"

# Enqueue the job — pass task via environment to avoid shell injection
JOB_ID=$(PYTHONPATH="$CLIVE_DIR" QUEUE_DIR="$QUEUE_DIR" TASK="$TASK" USER="$USER" TOOLSET="$TOOLSET" python3 -c "
import os, sys
from server.queue import JobQueue
q = JobQueue(os.environ['QUEUE_DIR'])
j = q.enqueue(task=os.environ['TASK'], user=os.environ['USER'], toolset=os.environ['TOOLSET'])
print(j.id)
" 2>/dev/null) || {
    echo "Error: Failed to enqueue job" >&2
    exit 1
}

echo "Job $JOB_ID queued. Waiting for result..."

# Poll for completion (timeout after 5 minutes)
TIMEOUT=300
ELAPSED=0
while [ $ELAPSED -lt $TIMEOUT ]; do
    RESULT=$(PYTHONPATH="$CLIVE_DIR" QUEUE_DIR="$QUEUE_DIR" JOB_ID="$JOB_ID" python3 -c "
import os, sys, json
from server.queue import JobQueue, JobStatus
q = JobQueue(os.environ['QUEUE_DIR'])
j = q.get(os.environ['JOB_ID'])
if j and j.status in (JobStatus.COMPLETED, JobStatus.FAILED):
    print(j.result)
    sys.exit(0 if j.status == JobStatus.COMPLETED else 1)
else:
    sys.exit(2)
" 2>/dev/null)
    RC=$?
    if [ $RC -ne 2 ]; then
        echo "$RESULT"
        exit $RC
    fi
    sleep 1
    ELAPSED=$((ELAPSED + 1))
done

echo "Error: Job $JOB_ID timed out after ${TIMEOUT}s" >&2
exit 1
