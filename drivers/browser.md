---
preferred_mode: script
use_interactive_when: discovering unknown content, following links, or exploring sites
---
# Browser Driver (lynx/curl/wget)

ENVIRONMENT: bash shell configured for web access.
WORKING DIR: /tmp/clive

PRIMARY TOOLS:
  lynx -dump URL          → rendered text output (best for reading pages)
  lynx -listonly URL      → extract all links from a page
  lynx -source URL        → raw HTML source
  curl -s URL             → raw response (best for APIs, JSON)
  curl -sI URL            → headers only (check redirects, content-type)
  wget -q -O file URL     → download to file (best for binary/large files)

LYNX PATTERNS:
- Extract heading: lynx -dump URL | head -20
- Follow link by text: lynx -dump URL | grep -i 'link text'
- Get all links: lynx -listonly -dump URL
- Handle redirects: lynx follows automatically; curl needs -L flag

CURL PATTERNS:
- JSON API: curl -s URL | jq '.field'
- POST data: curl -s -X POST -H 'Content-Type: application/json' -d '{"key":"val"}' URL
- Auth header: curl -s -H 'Authorization: Bearer TOKEN' URL
- Follow redirects: curl -sL URL
- Save response: curl -s URL > /tmp/clive/response.json

PITFALLS:
- lynx -dump on large pages: pipe through head -100 to avoid flooding screen
- curl without -s: progress bar clutters output, always use -s (silent)
- HTTPS errors: use curl -sk to skip cert verification only if needed
- Binary content: check Content-Type with curl -sI before dumping
- Rate limiting: add sleep 1 between rapid API calls

OUTPUT: Save extracted data to /tmp/clive/ for other subtasks.
COMPLETION: When done, say DONE: <one-line summary of what was accomplished>.
