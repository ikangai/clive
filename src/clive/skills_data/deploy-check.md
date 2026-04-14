---
tags: ops, deployment
params: SERVICE
---
# Deploy Check

Pre-deployment verification procedure. Uses other skills for thoroughness.

PROCEDURE:
1. Check git status to ensure clean working tree:
   [use:git-summary]

2. Verify the service is currently running:
   `curl -sI {SERVICE} | head -1`
   If not reachable, note the issue.

3. Run a quick API health check:
   [use:api-test]

4. Check disk space: `df -h /`
   Warn if < 20% free.

5. Check recent logs for errors:
   `tail -100 /var/log/{SERVICE}.log 2>/dev/null | grep -c ERROR`

6. Write pre-deploy report to session_dir/deploy_check.json with:
   git_status, service_reachable, api_health, disk_free, recent_errors

7. task_complete with go/no-go recommendation
