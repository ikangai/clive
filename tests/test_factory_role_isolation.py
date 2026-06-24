"""The factory's own roles (proposer / judge / reporter / scenario-miner /
check-synth) are `claude -p` workers. Like the claude-cli *panel*, each role call
must run ISOLATED — otherwise operating the loop loads the operator's ~/.claude
on every call: the group-chat plugin registers a ghost teammate and can POST to
the live chat, the SessionStart/Stop hooks fire (incl. a 600s team barrier that
would hang every role call), and all MCP servers connect. These tests pin the
isolation flags on the role transport. Keep in sync with llm._build_claude_cli_argv.
"""
import json


def test_role_claude_argv_is_isolated():
    from factory.roles import common
    argv = common._isolated_claude_argv()
    assert argv[0] == "claude"
    assert "-p" in argv
    # --setting-sources "" → enabledPlugins not read → no plugin, no hooks (no
    # ghost handle, no 600s barrier) — while keychain/subscription auth still works.
    assert argv[argv.index("--setting-sources") + 1] == ""
    # zero tools: a role returns text/JSON, never runs Bash/MCP/etc.
    assert argv[argv.index("--tools") + 1] == ""
    # zero MCP servers, ignoring all ambient MCP config
    assert "--strict-mcp-config" in argv
    mcp = json.loads(argv[argv.index("--mcp-config") + 1])
    assert mcp == {"mcpServers": {}}
    # --bare would disable subscription/keychain auth — must NOT be used.
    assert "--bare" not in argv


def test_role_claude_argv_json_toggle():
    from factory.roles import common
    assert "--output-format" in common._isolated_claude_argv(json_output=True)
    assert "--output-format" not in common._isolated_claude_argv(json_output=False)
    # isolation flags present regardless of output format
    assert common._isolated_claude_argv(json_output=False)[
        common._isolated_claude_argv(json_output=False).index("--setting-sources") + 1] == ""
