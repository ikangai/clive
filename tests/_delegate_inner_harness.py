"""Minimal "inner clive" simulator for the delegate integration test.

Imports DelegateClient, makes ONE chat completion call against stdio,
prints the resulting content as FINAL:<text>, and exits. The integration
test plays the outer role against this harness's stdin/stdout.

This is deliberately simpler than spawning all of clive.py — it isolates
the stdio transport layer so a test failure points at DelegateClient,
not at planner/executor/tmux setup noise.
"""
import sys

from delegate_client import DelegateClient


def main():
    client = DelegateClient(stdout=sys.stdout, stdin=sys.stdin,
                            poll_interval=0.05, timeout=10.0)
    resp = client.chat.completions.create(
        model="delegate",
        messages=[{"role": "user", "content": "say hi"}],
        max_tokens=32,
    )
    content = resp.choices[0].message.content or ""
    # Print on a line the outer can find. flush immediately.
    print(f"FINAL:{content}", flush=True)


if __name__ == "__main__":
    main()
