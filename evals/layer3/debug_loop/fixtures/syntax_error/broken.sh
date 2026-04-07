#!/bin/bash
set -e
echo "Starting..."
if [ -f input.txt; then  # missing closing bracket
  cat input.txt > /tmp/clive/result.txt
fi
echo "Done"
