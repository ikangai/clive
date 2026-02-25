#!/bin/bash
# ./fetch_emails.sh
# Pure curl IMAP — no interactive client, agent-safe

IMAP_HOST="imaps://imap.world4you.com"
IMAP_USER="kiu@ikangai.com"
IMAP_PASS=$(security find-internet-password \
  -a "kiu@ikangai.com" \
  -s "imap.world4you.com" -w)

# list unread message IDs
UNREAD=$(curl -s \
  --url "${IMAP_HOST}/INBOX" \
  --user "${IMAP_USER}:${IMAP_PASS}" \
  --ssl-reqd \
  -X "SEARCH UNSEEN")

echo "Unread message IDs: $UNREAD"

# fetch first 5 unread messages
for ID in $(echo $UNREAD | grep -oE '[0-9]+' | head -5); do
  echo "━━━━━━━━━━━━━━ Message $ID ━━━━━━━━━━━━━━"
  curl -s \
    --url "${IMAP_HOST}/INBOX;UID=${ID}" \
    --user "${IMAP_USER}:${IMAP_PASS}" \
    --ssl-reqd \
  | grep -E "^(From|To|Subject|Date):|^$" -A 50 \
  | head -80
done