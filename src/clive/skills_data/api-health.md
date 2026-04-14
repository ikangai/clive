---
tags: ops, monitoring
params: URL
---
# API Health Check (Executable)

Verify an API endpoint is healthy. Zero LLM calls on happy path.

STEPS:
- cmd: curl -sI {URL} | head -1
  check: output_contains 200
  on_fail: abort
- cmd: curl -s {URL} > /tmp/clive/api_response.json
  check: file_exists /tmp/clive/api_response.json
  on_fail: abort
- cmd: python3 -c "import json; json.load(open('/tmp/clive/api_response.json')); print('valid')"
  check: output_contains valid
  on_fail: skip
