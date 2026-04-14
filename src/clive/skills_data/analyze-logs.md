# Analyze Logs

PROCEDURE:
1. Identify the log file(s) — check common locations: /var/log/, ./logs/, or ask
2. Count total lines, ERROR lines, WARN lines: `wc -l file && grep -c ERROR file && grep -c WARN file`
3. Extract unique error messages: `grep ERROR file | sort -u`
4. Find the most recent errors: `grep ERROR file | tail -20`
5. Check for patterns: `grep ERROR file | awk '{print $4}' | sort | uniq -c | sort -rn | head -10`
6. Write summary to session_dir/log_analysis.json with: total_lines, error_count, warn_count, top_errors
7. task_complete with findings

TIPS:
- Large logs: use tail -10000 first, then analyze
- Compressed logs: use zgrep for .gz files
- Multiple files: use find + xargs
