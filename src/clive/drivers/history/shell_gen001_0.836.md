# Shell Driver (bash)

ENVIRONMENT: bash shell with PS1="[AGENT_READY] $ "
WORKING DIR: /tmp/clive (shared workspace — write results here)

COMMAND EXECUTION:
- One command per turn. Wait for output before next.
- Chain dependent: mkdir -p out && cp file out/
- Chain independent: cmd1; cmd2
- Capture output: cmd > /tmp/clive/result.txt

EXIT CODES:
- Check: cmd; echo "EXIT:$?"
- 0=success, 1=error, 2=misuse, 126=not executable, 127=not found

FILE OPERATIONS:
- List files: ls -la /path (includes hidden, shows details)
- List recursively: find /path -type f | sort
- Find by name: find /path -type f -name '*.ext'
- Find by content: grep -rl 'pattern' /path
- Disk usage: du -sh /path/* | sort -h
- Check file type: file /path/file

TEXT PROCESSING:
- Search: grep -r 'pattern' /path | head -20
- Count in file: grep -c 'pattern' file
- Count all: grep -ro 'pattern' /path | wc -l
- Extract columns: awk '{print $N}' file
- Sort & count: sort file | uniq -c | sort -rn
- Limit output: cmd | head -N or cmd | tail -N

DATA FORMATS:
- JSON read: jq '.field' file
- JSON from URL: curl -s url | jq '.[]'
- JSON create: jq -n --arg k v '{"key":$k}'
- CSV process: mlr --csv filter '$col > val' file.csv

COMMON PIPELINES:
- Find and process: find /path -name '*.txt' -exec grep 'pattern' {} +
- Count matching files: grep -rl 'pattern' /path | wc -l
- Directory tree: find /path -type f | head -50

PITFALLS:
- Quote patterns: grep 'TODO' *.py (not grep TODO *.py)
- Single quotes for literals, double for variables
- Large output: always pipe ls/find through head
- Binary files: check type with file before reading
- Permission denied: check ls -la, use sudo only if needed

COMPLETION: <cmd type="task_complete">summary</cmd>
Write results to /tmp/clive/ for downstream tasks.