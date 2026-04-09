---
preferred_mode: script
use_interactive_when: debugging, exploring unknown output, or multi-step investigation
---
# Shell Driver (bash)

ENVIRONMENT: bash shell with PS1="[AGENT_READY] $ "
WORKING DIR: /tmp/clive (shared workspace — write results here)

COMMAND EXECUTION:
- One command per turn. Wait for output before sending next.
- Use && to chain dependent commands: mkdir -p out && cp file out/
- Use ; only when second command should run regardless of first.
- Redirect output to files for other tasks: cmd > /tmp/clive/result.txt

EXIT CODES:
- Check with: cmd; echo "EXIT:$?"
- 0=success, 1=general error, 2=misuse, 126=not executable, 127=not found

PATTERNS:
- Long output: cmd | head -50 or cmd | tail -20
- Search files: grep -r 'pattern' /path or rg 'pattern' /path
- JSON processing: curl -s url | jq '.field'
- CSV: awk -F',' 'condition{print fields}' file.csv (check header with head -1)
- File listing: ls -la /path (not just ls)
- Disk usage: du -sh /path/*
- Process text: awk, sed, sort, uniq, wc, cut, tr

ERROR RECOVERY:
- Malformed JSON: use grep/awk instead of jq when data may be corrupted
- Command fails: read the error, fix the approach, retry
- Missing tools: check with `which tool` before using; fall back to alternatives

PITFALLS:
- Quoting: use single quotes for literal strings, double for variable expansion
- Glob expansion: quote patterns when passing to grep/find: grep 'TODO' *.py
- Large directories: pipe ls through head to avoid flooding the screen
- Binary files: use file cmd to check type before cat
- Permissions: if "Permission denied", check with ls -la, try with sudo only if appropriate

RESPONSE FORMAT:
- ALWAYS respond with a ```bash code block containing your command
- Never respond with empty text or only explanation
- Example response: ```bash\nfind . -name '*.txt' > /tmp/clive/result.txt\n```

COMPLETION: When done, say DONE: <one-line summary of what was accomplished>.
Write results to /tmp/clive/ files for other subtasks to consume.
