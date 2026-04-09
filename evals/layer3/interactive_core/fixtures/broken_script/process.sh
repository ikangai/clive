#!/bin/bash
set -euo pipefail
INPUT="data.txt"
OUTPUT="result.txt"
echo "Processing $INPUT
cat "$INPUT" | wc -l > "$OUTPUT"
echo "Done"
