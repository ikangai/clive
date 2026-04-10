# Bring-Your-Own-LLM for remote clives

When you address a remote clive via `clive@host`, the remote needs an
LLM to plan and execute. Clive supports two modes of LLM delivery —
automatically picked based on the provider you're using locally.

## TL;DR

```bash
# Cloud provider (Anthropic, OpenAI, OpenRouter, Gemini):
export LLM_PROVIDER=anthropic
export ANTHROPIC_API_KEY=sk-ant-...
python3 clive.py "clive@prod list files in /tmp"
# → API key SendEnv'd to remote, remote calls api.anthropic.com directly

# Local provider (LMStudio, Ollama):
# LMStudio or Ollama running on your laptop
export LLM_PROVIDER=lmstudio
python3 clive.py "clive@prod list files in /tmp"
# → remote gets LLM_PROVIDER=delegate, every inference round-trips
#   back through the SSH channel to your laptop's LMStudio
```

No `ssh -R`, no tunnel config, no network changes on the remote. The
same `clive@host` address works the same way for every provider; only
the inference plumbing differs under the hood.

## Cloud providers — SendEnv pass-through

Supported: **Anthropic, OpenAI, OpenRouter, Gemini** (anything
listed in `llm.PROVIDERS` with a non-null `api_key_env`).

Your local env vars are forwarded to the remote via SSH `SendEnv`.
The remote clive uses them to call the cloud endpoint **directly** —
your laptop does not proxy inference.

```
you (outer)         remote (inner)
    │                     │
    │─── ssh+SendEnv ─────▶  reads LLM_PROVIDER, *_API_KEY from env
    │                     │─── https → api.anthropic.com
    │                     │           (or openai.com, openrouter.ai…)
    │                     ◀───  response
    │                     │─── plans, executes, runs tasks
    ◀──── SSH pipe ───────│     (framed protocol on stdout/stderr)
```

**Remote sshd requirements:** the remote's `/etc/ssh/sshd_config` (or
a drop-in under `/etc/ssh/sshd_config.d/`) must have an `AcceptEnv`
line for every env var you want forwarded:

```
AcceptEnv LLM_PROVIDER AGENT_MODEL LLM_BASE_URL \
          ANTHROPIC_API_KEY OPENAI_API_KEY OPENROUTER_API_KEY GOOGLE_API_KEY
```

Run `clive --agents-doctor` to check this — see the
[Troubleshooting](#troubleshooting) section below.

**What gets forwarded** (from `agents._FORWARD_ENVS`):

| Env var | Purpose |
|---|---|
| `LLM_PROVIDER` | which provider the remote should use |
| `AGENT_MODEL` | override the default model |
| `LLM_BASE_URL` | point at a self-hosted proxy (LiteLLM, vLLM, etc.) |
| `ANTHROPIC_API_KEY` | Anthropic cloud auth |
| `OPENAI_API_KEY` | OpenAI cloud auth |
| `OPENROUTER_API_KEY` | OpenRouter auth |
| `GOOGLE_API_KEY` | Gemini auth |

**What gets injected** (as a remote env assignment, not SendEnv — so no
`AcceptEnv` dependency for these):

| Env var | Purpose |
|---|---|
| `CLIVE_FRAME_NONCE` | session nonce for framed protocol authentication |

## Local providers — delegation over stdio

Supported: **LMStudio, Ollama** (and anything else in `llm.PROVIDERS`
with `api_key_env: None` and a localhost `base_url`).

Local LLMs live on *your* laptop's localhost. The remote has no way
to reach them without tunneling. Clive handles this automatically by
switching the remote to a `delegate` LLM provider — every inference
call the remote wants to run is serialized as a framed `llm_request`
message on stdout (i.e. back over the SSH channel), answered by your
local LMStudio/Ollama, and typed back into the remote as an
`llm_response` frame.

```
you (outer)         remote (inner)
    │                     │
    │─── ssh+override ────▶  LLM_PROVIDER=delegate on the remote
    │                     │   AGENT_MODEL=delegate
    │                     │   CLIVE_FRAME_NONCE=<random>
    │                     │
    │                     │  planner wants inference:
    │                     │  ┌─────────────────────────────┐
    │                     │  │ DelegateClient.chat() →     │
    │                     │  │   encode(llm_request, ...)  │
    │                     │  │   → stdout (SSH pipe)       │
    │                     │  └──────────────┬──────────────┘
    │                     │                 │
    ◀───── llm_request ───│                 │
    │                                       │
    │ ┌───────────────────────────────┐     │
    │ │ outer's interactive_runner:    │     │
    │ │  detects llm_request in pane   │     │
    │ │  calls local llm.chat(...)     │     │
    │ │  (LMStudio on localhost:1234)  │     │
    │ │  → encode(llm_response, ...)   │     │
    │ │  → pane.send_keys(...)         │     │
    │ └──────────────┬─────────────────┘     │
    │                │                       │
    │─── llm_response ─▶                     │
    │                     │  DelegateClient.│
    │                     │  _read_available│
    │                     │  returns →       │
    │                     │  _ChatCompletion│
    │                     ◀─ planner resumes
    │                     │
    │                     │  ... continue planning, then tool exec...
```

Every remote inference call makes one round trip over the SSH channel.
The remote never touches the network for inference — your laptop does
all the brain work.

### What you do as a user

1. Start LMStudio (port 1234) or Ollama (port 11434) as normal.
2. `export LLM_PROVIDER=lmstudio` (or `ollama`).
3. `python3 clive.py "clive@prod do something"`.

That's it. No `ssh -R`, no tunnel config, no `AcceptEnv` worries for
API keys (there are none).

### Caveats

- **Latency.** Every remote LLM call adds one SSH round-trip on top
  of the normal LMStudio/Ollama inference time. For a task that
  involves 10 LLM calls, that's 10 round-trips. LAN latency (~5ms)
  is fine; inter-continental WAN (100+ms) adds up. Acceptable for
  local dev and interactive testing; consider a cloud provider with
  a regional endpoint for throughput-sensitive batch jobs.

- **Streaming is not yet supported.** DelegateClient's `chat_stream`
  falls back to non-streaming and fires `on_token` once with the
  complete content. Streaming is a planned follow-up.

- **Disconnect behaviour.** If the outer crashes or the SSH channel
  drops while the remote is waiting for an `llm_response`, the
  remote's `DelegateClient` times out after 5 minutes (the default
  `DelegateClient.timeout`) and raises `TimeoutError` into the
  remote's task runner. The task fails with `turn=failed`. On the
  outer's next reconnect the remote continues from a clean state.

- **Privacy.** Delegation routes the remote's LLM prompts (planner,
  executor, classifier templates) through **your outer clive's
  configured provider**. If you're running the outer on Anthropic,
  Anthropic sees the remote's inner prompts. If you're running the
  outer on LMStudio locally, nothing leaves your laptop. Choose the
  outer's provider based on where you want the data to land.

- **Model preference is ignored.** If the remote's task config
  specifies a particular model (e.g. `AGENT_MODEL=gpt-4`), the outer
  ignores it under delegation and uses its own configured model.
  The outer is paying for inference — the outer's model choice
  wins. If you need a specific model, set it on the outer.

## Configuration cheat sheet

```bash
# Cloud provider setup
export LLM_PROVIDER=anthropic               # or openai, openrouter, gemini
export ANTHROPIC_API_KEY=sk-ant-...
# Remote's sshd must have AcceptEnv for LLM_PROVIDER + ANTHROPIC_API_KEY

# Local provider setup
export LLM_PROVIDER=lmstudio                # or ollama
# Start LMStudio on port 1234 (or Ollama on 11434)
# No AcceptEnv config needed on the remote

# Pointing at a self-hosted proxy (cloud path)
export LLM_PROVIDER=openrouter
export LLM_BASE_URL=http://my-litellm:8080/v1
export OPENROUTER_API_KEY=sk-or-...
# Remote inherits LLM_BASE_URL via SendEnv

# Per-agent config in ~/.clive/agents.yaml
prod:
  host: prod.example.com
  key: ~/.ssh/prod_key                      # optional, defaults to SSH default identity
  toolset: minimal                          # toolset on the remote
  path: /opt/clive/.venv/bin/python clive.py  # path override for venv installs
  timeout: 10                               # SSH connect timeout in seconds
```

## Troubleshooting

### Step 1: Run the doctor

```bash
clive --agents-doctor
```

Output format:

```
✓ prod
  ✓ key_exists: using SSH default identity
  ✓ ssh_connect: ok
  ✓ clive_installed: ok
  ✓ accept_env: all set envs accepted
✗ stage
  ✓ key_exists: /home/me/.ssh/stage_key
  ✗ ssh_connect: ssh: connect to host stage.example.com port 22: Connection timed out
```

Exit code 0 if every check for every host passes, 1 if any check
failed, 0 with a helpful message if no agents are configured. Fits
into CI pipelines.

**Doctor check meanings:**

- `key_exists` — the SSH key file referenced in the registry entry
  exists on disk, OR the entry has no key (SSH default identity).
- `ssh_connect` — `ssh -o BatchMode=yes -o ConnectTimeout=5 <host>
  "echo clive-doctor-ok"` completes successfully.
- `clive_installed` — running `python3 -c 'import clive; print("ok")'`
  on the remote succeeds. Honours venv / versioned-python `path:`
  config.
- `accept_env` — the remote's `sshd -T` output lists every
  `AcceptEnv` var that the outer is currently sending. Reports
  "could not verify" when `sshd -T` can't be run as the login user
  (needs sudo on most distros — not a failure, just an unknown).

### Step 2: Read the pane scrollback

If a task is hanging, attach to clive's tmux session and look at the
agent pane:

```bash
tmux -L clive attach -t clive:agent-prod
```

You'll see the decoded view the outer LLM is reading: lines like

```
⎇ CLIVE» turn=thinking
⎇ CLIVE» progress: step 2 of 3
⎇ CLIVE» turn=waiting
⎇ CLIVE» question: "which format?"
⎇ CLIVE» turn=done
⎇ CLIVE» context: {"result":"42"}
```

Any line starting with `⎇ CLIVE»` is a decoded protocol frame from
the remote. Raw `<<<CLIVE:...>>>` bytes you see in the scrollback
are the wire protocol before decoding — normal, not an error.

### Common failure modes

**Symptom:** task hangs with no output.

Causes:
1. SSH connection never established. Check `clive --agents-doctor`
   for the `ssh_connect` line.
2. Remote clive crashed during planner startup. Check the pane
   scrollback for Python tracebacks.
3. Delegate response never came back. If you're on `LLM_PROVIDER=lmstudio`,
   check that LMStudio is actually running on port 1234 and the model
   is loaded. `curl http://localhost:1234/v1/models` should list
   something.

**Symptom:** `⎇ CLIVE» turn=failed` with `error: LMStudio unreachable`.

Your laptop's LMStudio/Ollama process went away mid-task. Restart it
and re-send the task. The failure is logged as an `llm_error` frame,
so the remote's `DelegateClient` raised `RuntimeError` into the task
runner.

**Symptom:** `clive@host` resolution works but every delegated call
times out after 5 minutes.

The outer's pane reader isn't detecting the `llm_request` frame.
Check that the outer is on a local provider (`LLM_PROVIDER=lmstudio`
or `ollama`) — the delegate override is only active for those.
Under cloud providers, the remote tries to call the cloud endpoint
directly and delegation doesn't run at all.

**Symptom:** `clive --agents-doctor` says
`accept_env: remote sshd missing AcceptEnv for: LLM_PROVIDER, ...`

The remote's `/etc/ssh/sshd_config` has no `AcceptEnv` directive, or
is missing the specific vars the outer is sending. Add them:

```
# /etc/ssh/sshd_config.d/clive.conf
AcceptEnv LLM_PROVIDER AGENT_MODEL LLM_BASE_URL \
          ANTHROPIC_API_KEY OPENAI_API_KEY OPENROUTER_API_KEY GOOGLE_API_KEY
```

Then `sudo systemctl reload sshd` and re-run the doctor.

## Threat model & security notes

### Session nonce

Every framed protocol message carries a session nonce:

```
<<<CLIVE:{kind}:{nonce}:{base64(json(payload))}>>>
```

The outer generates a fresh 128-bit urlsafe random nonce per agent
session via `protocol.generate_nonce()`, injects it into the remote
env as `CLIVE_FRAME_NONCE`, and stores it on the returned
`pane_def["frame_nonce"]`. Every frame the remote emits carries that
nonce; every frame the outer parses is rejected unless the nonce
matches.

**Why:** base64 wrapping on its own prevents accidental protocol
spoofing (stray tool output can't form a valid frame because `<`/`>`
are outside the b64 alphabet), but an adversarial LLM running inside
the remote — one that has been prompt-injected by a malicious file,
website, or tool output — can emit a valid base64 frame by design.
Without the nonce, a compromised remote LLM could fabricate a
`turn=done` to prematurely terminate a task, or (Phase 2) forge an
`llm_request` to trick the outer into burning inference tokens on
an attacker-controlled prompt. The nonce is not present in any
prompt the remote LLM can see; it's an env var, not reasoning
context.

The regression test
`tests/test_protocol.py::test_forged_frame_is_currently_accepted_documented_threat`
was explicitly removed in Phase 1.5 once the nonce landed; the
current test `test_decode_rejects_mismatched_nonce` enforces the
invariant.

### What the outer LLM sees

When the outer operates an agent pane, its `interactive_runner.py`
passes the captured pane screen through `remote.render_agent_screen`
BEFORE the LLM sees it. That function replaces every valid frame
with a human-readable `⎇ CLIVE»` pseudo-line and drops any frame
with a mismatched nonce. **The outer LLM never sees raw
`<<<CLIVE:...>>>` bytes** — it cannot learn the wire format by
observation and cannot be prompt-injected into forging frames at
any layer.

Delegated `llm_request`/`llm_response`/`llm_error` frames appear
in the outer LLM's view as terse side-channel markers:

```
⎇ CLIVE» llm_request id=req-abc
⎇ CLIVE» llm_response id=req-abc
```

The `messages` / `content` payloads are deliberately stripped from
the pseudo-line — the outer LLM doesn't need to see the remote's
inner prompts to make planning decisions, and exposing them would
conflate the remote's reasoning context with the outer's.

### Data flow audit

For a `clive@remote` task:

| Data | Where it lives | Who can see it |
|---|---|---|
| Your task text | Outer process → sent to remote over SSH | You, outer LLM (as part of its plan), remote LLM (as part of its plan), SSH intermediaries (encrypted) |
| Outer LLM's plan | Outer process memory | You, outer LLM provider (if cloud) |
| Remote LLM's prompts | Remote process memory; **also outer process via delegation** | Same as outer LLM's provider plus the remote host's local storage |
| Tool output on remote | Remote pane scrollback, SCP'd results | Remote filesystem, outer (via framed `context` or `file` frames), SSH intermediaries |
| SSH traffic | Between outer and remote | Encrypted, attackers with MITM-level access would see ciphertext only |
| Session nonce | Outer process memory + remote env var | Outer process, remote process, anyone with `ps` access on the remote (but the nonce is single-session; it's not a long-lived secret) |

**Under delegation specifically:** the remote's inner LLM prompts
transit through the outer's LLM provider. If the outer is on
LMStudio locally, nothing leaves your laptop. If the outer is on
Anthropic/OpenAI, those providers receive the remote's inner
prompts as if they were outer-originated calls. Choose based on
data-locality requirements.

## Manual smoke test

The automated test suite covers the transport layer end-to-end
(`tests/test_integration_delegate.py`) with a mock LMStudio HTTP
server. This manual procedure validates the full stack against a
real LMStudio instance.

### Prerequisites

- [ ] LMStudio (or Ollama) running on your local machine with a
      chat-capable model loaded and the server enabled.
      Default: LMStudio on `http://localhost:1234/v1`, Ollama on
      `http://localhost:11434/v1`.
- [ ] At least one reachable remote host with clive installed,
      SSH access configured (keys, BatchMode works), and Python 3.
- [ ] `~/.clive/agents.yaml` with an entry for that host, or the
      host reachable via SSH default resolution.
- [ ] tmux installed locally.

### Procedure

```bash
# 1. Confirm your local provider is reachable
curl -s http://localhost:1234/v1/models | head -20
# Expect: JSON with at least one model in data[]

# 2. Set the environment and run the doctor
export LLM_PROVIDER=lmstudio
export LLM_BASE_URL=http://localhost:1234/v1      # optional override
python3 clive.py --agents-doctor
# Expect: all ✓ for your configured host
# If accept_env fails: add AcceptEnv to remote sshd_config (or ignore
# for the local-provider path — delegate injects LLM_PROVIDER directly
# on the remote command, no AcceptEnv needed for that)

# 3. Trigger a trivial remote task
python3 clive.py "clive@<your-host> list files in /tmp and return the count"
# Expected under-the-hood behaviour:
#   - Outer parses `clive@<your-host>`
#   - build_agent_ssh_cmd() injects LLM_PROVIDER=delegate + nonce
#   - SSH opens, inner clive starts in conversational mode
#   - Inner's planner calls llm.chat(), DelegateClient serializes
#     an llm_request frame to stdout (SSH pipe)
#   - Outer's interactive_runner detects the frame, calls local
#     LMStudio, types back llm_response via send_keys
#   - Inner's DelegateClient decodes, returns _ChatCompletion
#   - Planning proceeds, executor runs the task, emits a final
#     context+turn=done
#   - Outer surfaces the result

# 4. Watch LMStudio's server log
# Every planner/classifier/executor/summarizer call produces one
# HTTP request. You should see several entries per task — the
# exact count depends on how the planner collapses the task.

# 5. Attach to the pane scrollback to observe the decoded view
tmux -L clive attach -t clive:agent-<your-host>
# You'll see ⎇ CLIVE» pseudo-lines as the remote progresses.
# Detach with Ctrl-b d.
```

### Expected outcome

- The task completes with a result (e.g. "3 files: a.txt, b.log, c.csv").
- LMStudio's server log shows N > 0 requests (typically 3-8 for a
  trivial task).
- The agent pane scrollback contains `⎇ CLIVE» turn=done` and at
  least one `⎇ CLIVE» llm_request id=...` line.
- No raw `<<<CLIVE:...>>>` bytes are visible to the outer LLM — the
  renderer strips them.

### If something fails

Re-run `clive --agents-doctor` first. If all checks pass, inspect the
agent pane scrollback for a Python traceback, and check LMStudio's
server log for any 4xx/5xx responses. The most common failure modes
are listed in [Common failure modes](#common-failure-modes) above.

## Relevant source files

- `protocol.py` — frame grammar, encode/decode, nonce enforcement
- `delegate_client.py` — remote-side stdio LLM client
- `executor.handle_agent_pane_frame` — outer-side `llm_request`
  handler
- `agents.build_agent_ssh_cmd` — SSH command builder with delegate
  override and nonce injection
- `agents.resolve_agent` — `clive@host` → `pane_def` resolution
- `agents_doctor.py` — `clive --agents-doctor` implementation
- `remote.render_agent_screen` — outer-side pane decoder (strips
  raw frames before the outer LLM sees them)
- `drivers/agent.md` — driver prompt describing the `⎇ CLIVE»`
  pseudo-line grammar
- `tests/test_integration_delegate.py` — automated end-to-end test
  with mock LMStudio
- `docs/plans/2026-04-10-remote-clive-byollm-delegation.md` — the
  original implementation plan
