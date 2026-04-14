---
preferred_mode: script
use_interactive_when: browsing or exploring the inbox interactively
agent_model: default
observation_model: fast
---
# Email Driver (neomutt / msmtp)

ENVIRONMENT: bash shell with email tools. Pane starts at a shell prompt.
WORKING DIR: /tmp/clive

PRIMARY TOOLS:
  bash send_reply.sh "to" "subject" "body"  → send email via msmtp
  neomutt                                    → interactive email client (IMAP)

## SENDING EMAIL (use send_reply.sh — fast, no TUI)
  bash send_reply.sh "recipient@example.com" "Subject line" "Body text"
  # Output on success: [Sent to recipient@example.com]
  # On error: msmtp prints error to stderr
  For multi-line body, use $'line1\nline2' or a heredoc.

## READING EMAIL — SCRIPTED (preferred for batch read/summarize)
  Config: ~/.clive/config/email.toml has address, imap_server, imap_port, password
  Write a Python script using imaplib + email.message to fetch messages programmatically.
  Login: use address as username, password as password, imap_server:imap_port for SSL connection.
  Pattern: read toml config → IMAP SSL connect → select INBOX → fetch latest N → parse → output
  This is FASTER and more reliable than TUI navigation. Use for: read N emails, search, summarize.

## READING EMAIL — INTERACTIVE (only when you need to explore/browse)
  1. Type: neomutt
  2. Wait: index screen appears (list of messages: date, sender, subject)
  3. If TLS/connection error → config issue, report and stop
  4. Navigate the index to find relevant messages

NEOMUTT INDEX SCREEN (what you see after launch):
  Lines like: 123 N Apr 09 sender@example   (1.2K) Subject text here
  N=new, O=old, D=deleted. Cursor highlights current message.

NEOMUTT KEYS:
  index: j/k=scroll  o/Enter=open  m=compose new  r=reply  d=mark-delete
         /=search  l=limit view  c=change-folder  q=quit  $=sync/purge
  message: q=back-to-index  r=reply  f=forward  s=save  d=delete
  compose: Tab=next-field  y=send  q=abort  a=attach-file

PATTERNS:
- Send quick email: bash send_reply.sh "to" "subject" "body"
- Read latest N / summarize: Python script with imaplib (see SCRIPTED section above)
- Search by sender/subject: Python script with IMAP SEARCH command
- Browse/explore inbox interactively: neomutt (TUI)
- Reply to email: open message (o) → r → type reply → y to send → q back
- Exit neomutt: q from index (may ask "Strstrstrstrstrstrstrstrstrstrstrstrstrstr?" → press n)

PITFALLS:
- DO NOT use the system `mail` or `sendmail` command — they don't work. Use send_reply.sh.
- neomutt blocks the pane: only launch it when you need to READ emails
- d only marks for deletion, press $ to actually delete
- q from index may ask to move read messages — press n to keep them
- Large inbox: use l (limit) then ~d<1w to show only last week
- If neomutt hangs on connect: Ctrl-C, report connection error, stop

COMPLETION: When done, say DONE: <one-line summary of what was accomplished>.
Write results to /tmp/clive/ for other subtasks.
