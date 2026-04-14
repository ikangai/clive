# API Test

PROCEDURE:
1. Check if the endpoint is reachable: `curl -sI URL | head -1`
2. GET request with timing: `curl -s -w "\ntime: %{time_total}s\nstatus: %{http_code}" URL`
3. Validate response format: pipe through `jq .` for JSON, check Content-Type header
4. Test error cases: invalid params, missing auth, wrong method
5. Write results to session_dir/api_test.json with: url, status, time, response_preview
6. task_complete with summary

TIPS:
- Add -H 'Accept: application/json' for API calls
- Use -w to capture timing without polluting output
- For auth: check .env or environment for API keys
