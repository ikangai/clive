# Git Summary

PROCEDURE:
1. Check repo status: `git status --short`
2. Recent commits: `git log --oneline -20`
3. Branch info: `git branch -a`
4. Changed files since last tag: `git diff --stat $(git describe --tags --abbrev=0 2>/dev/null || echo HEAD~10)..HEAD`
5. Contributors: `git shortlog -sn --since="30 days ago"`
6. Write summary to session_dir/git_summary.json
7. task_complete with overview

TIPS:
- Use --no-pager to avoid interactive mode
- If not a git repo, report that and exit
