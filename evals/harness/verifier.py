"""Eval verifiers: deterministic (shell) and LLM-based with caching.

Deterministic verifiers run a shell command and check exit code.
LLM verifiers send the task + result to an LLM and cache the verdict.
"""
import hashlib
import json
import os
import subprocess


class DeterministicVerifier:
    """Verify eval results via shell command exit code."""

    def __init__(self, check: str, workdir: str):
        self.check = check
        self.workdir = workdir

    def verify(self) -> bool:
        """Run the check command. Returns True if exit code is 0."""
        try:
            result = subprocess.run(
                self.check,
                shell=True,
                cwd=self.workdir,
                capture_output=True,
                timeout=10,
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            return False


class LLMVerifier:
    """Verify eval results via LLM judgment, with caching.

    Cache key is hash of (task_description, agent_output, verify_prompt).
    Cached verdicts are stored as JSON in the cache directory.
    """

    def __init__(
        self,
        verify_prompt: str,
        cache_dir: str = ".eval_cache",
    ):
        self.verify_prompt = verify_prompt
        self.cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)

    def _cache_key(self, task: str, output: str) -> str:
        content = f"{task}|{output}|{self.verify_prompt}"
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    def _get_cached(self, key: str) -> dict | None:
        path = os.path.join(self.cache_dir, f"{key}.json")
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
        return None

    def _set_cached(self, key: str, result: dict):
        path = os.path.join(self.cache_dir, f"{key}.json")
        with open(path, "w") as f:
            json.dump(result, f)

    def verify(self, task_description: str, agent_output: str) -> tuple[bool, str]:
        """Verify via LLM. Returns (passed, reasoning).

        Uses cache if available. Otherwise calls LLM and caches result.
        """
        key = self._cache_key(task_description, agent_output)
        cached = self._get_cached(key)
        if cached:
            return cached["passed"], cached.get("reasoning", "cached")

        from llm import get_client, chat

        client = get_client()
        messages = [
            {"role": "system", "content": self.verify_prompt},
            {
                "role": "user",
                "content": (
                    f"Task: {task_description}\n\n"
                    f"Agent output:\n{agent_output}\n\n"
                    "Did the agent successfully complete the task? "
                    'Respond with JSON: {"passed": true/false, "reasoning": "..."}'
                ),
            },
        ]
        response, _, _ = chat(client, messages)

        try:
            verdict = json.loads(response)
            passed = verdict.get("passed", False)
            reasoning = verdict.get("reasoning", "")
        except json.JSONDecodeError:
            # Strict fallback: look for exact words, not substrings
            words = set(response.lower().split())
            passed = "passed" in words and "true" in words
            reasoning = response

        result = {"passed": passed, "reasoning": reasoning}
        self._set_cached(key, result)
        return passed, reasoning


def verify_task(
    task_def: dict,
    workdir: str,
    agent_output: str = "",
    cache_dir: str = ".eval_cache",
) -> tuple[bool, str]:
    """Verify a task result based on its success_criteria definition.

    Returns (passed, detail_string).
    """
    criteria = task_def["success_criteria"]

    if criteria["type"] == "deterministic":
        v = DeterministicVerifier(check=criteria["check"], workdir=workdir)
        passed = v.verify()
        return passed, "deterministic check " + ("passed" if passed else "failed")

    elif criteria["type"] == "llm":
        prompt_path = criteria.get("prompt", "")
        if os.path.exists(prompt_path):
            with open(prompt_path) as f:
                verify_prompt = f.read()
        else:
            verify_prompt = (
                "You are an eval verifier. Determine if the agent completed "
                "the task successfully based on the output provided."
            )
        v = LLMVerifier(verify_prompt=verify_prompt, cache_dir=cache_dir)
        return v.verify(task_def.get("task", ""), agent_output)

    else:
        return False, f"Unknown criteria type: {criteria['type']}"
