# Email Driver (mutt/neomutt)

STATE MACHINE:
  index(default) → [o]pen → message → [r]eply → compose → [y]send
  index → [m]ail → compose → [y]send
  index → [/]search → results → [Enter]select
  any submenu → [q]back; [Q]quit mutt entirely

KEYS:
  index: j/k=scroll  o=open  m=new  r=reply  d=mark-delete  $=sync/purge
         /=search  l=limit  c=change-folder  ?=help
  message: q=back  r=reply  f=forward  s=save  d=delete
  compose: Tab=next-field  y=send  q=abort  a=attach-file
           To:/Cc:/Subject: fields at top, body below

NAVIGATION:
- Unread messages: press Tab from index to jump to next unread
- Search: /pattern then n=next-match N=prev-match
- Limit view: l then ~f sender or ~s subject
- Change mailbox: c then type path (~/Mail/sent, etc.)

PITFALLS:
- d marks for deletion but does NOT delete: must press $ to sync
- q exits submenu; Q exits mutt — don't confuse them
- Compose mode: must fill To: before body
- "Really delete?" prompt: press y to confirm
- Large mailbox: limit view first to avoid slow scrolling

ERRORS:
- "No strstrstrstrstrstrstrstrstrstrstrstrstrstr" → wrong strstrstrstrstrstrstrstrstrstrstrstrstrstr path
- "Connection refused" → check IMAP/SMTP config
- "Send error" → check msmtp config

COMPLETION: Use <cmd type="task_complete">summary</cmd> when done.
