---
preferred_mode: script
use_interactive_when: exploring unfamiliar datasets where next step depends on what you find
---
# Data Processing Driver

ENVIRONMENT: bash shell optimized for data transformation.
WORKING DIR: /tmp/clive (shared workspace)

PRIMARY TOOLS:
  jq '.field'              → JSON field extraction
  jq -r '.[] | .name'     → iterate JSON array
  jq -s 'add'             → merge JSON arrays
  mlr --csv filter '$col > val' file.csv  → CSV filtering
  mlr --csv sort-by col file.csv          → CSV sorting
  mlr --csv stats1 -a mean -f col file.csv → CSV statistics
  awk -F, '{print $1}' file.csv           → column extraction
  sort | uniq -c | sort -rn               → frequency count
  csvtool col 1,3 file.csv                → column selection

PATTERNS:
- JSON → CSV: jq -r '.[] | [.a, .b] | @csv' data.json
- CSV → JSON: mlr --c2j cat file.csv
- Aggregate: awk -F, '{sum+=$2} END {print sum}' file.csv
- Join files: paste -d, file1.txt file2.txt
- Pivot: mlr --csv reshape --o-pair-separator : file.csv
- Top N: sort -t, -k2 -rn file.csv | head -N

PITFALLS:
- jq without -r: outputs quoted strings ("value" not value)
- CSV with headers: use mlr (header-aware) over awk
- Large files: stream with pipes, don't load into memory
- Encoding: use iconv for non-UTF-8 input
- Empty fields: awk treats empty as 0 in numeric context

COMPLETION: When done, say DONE: <one-line summary of what was accomplished>.
Write results to /tmp/clive/ for other subtasks.
