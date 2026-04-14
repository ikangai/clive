#!/bin/bash
# ./send_reply.sh
# Usage: send_reply.sh "to@example.com" "Subject" "Body"

TO="$1"
SUBJECT="$2"
BODY="$3"

echo -e "To: ${TO}\nSubject: ${SUBJECT}\n\n${BODY}" \
  | msmtp "${TO}"

echo "[Sent to ${TO}]"