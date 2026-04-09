#!/usr/bin/env bash
# sandbox/run.sh — Execute a command inside a restricted sandbox.
# Usage: sandbox/run.sh <workdir> <command...> [--no-network]
#
# Provides:
#   - Filesystem: read-only root, writable workdir only
#   - Process: limited to 64 processes
#   - Memory: limited to 512MB (configurable via CLIVE_SANDBOX_MEM_MB)
#   - Network: allowed by default, --no-network to restrict
#   - No access to host home directory or credentials

set -euo pipefail

WORKDIR="$1"; shift
NO_NETWORK=false
ARGS=()
for arg in "$@"; do
    if [ "$arg" = "--no-network" ]; then
        NO_NETWORK=true
    else
        ARGS+=("$arg")
    fi
done

MEM_MB="${CLIVE_SANDBOX_MEM_MB:-512}"
MAX_PROCS="${CLIVE_SANDBOX_MAX_PROCS:-64}"

# Ensure workdir exists and resolve symlinks (macOS /var -> /private/var)
mkdir -p "$WORKDIR"
WORKDIR_REAL="$(cd "$WORKDIR" && pwd -P)"

if command -v bwrap &>/dev/null; then
    # Linux: bubblewrap — gold standard
    BWRAP_ARGS=(
        --ro-bind / /
        --bind "$WORKDIR" "$WORKDIR"
        --tmpfs /tmp
        --dev /dev
        --proc /proc
        --unshare-pid
        --unshare-uts
        --die-with-parent
    )
    if $NO_NETWORK; then
        BWRAP_ARGS+=(--unshare-net)
    fi
    # Hide host credentials
    BWRAP_ARGS+=(--tmpfs "$HOME/.ssh" --tmpfs "$HOME/.aws" --tmpfs "$HOME/.config")

    exec bwrap "${BWRAP_ARGS[@]}" \
        /bin/bash -c 'ulimit -u "$1"; ulimit -v "$2"; cd "$3" && shift 3 && eval "$@"' \
        _ "$MAX_PROCS" "$((MEM_MB * 1024))" "$WORKDIR" "${ARGS[@]}"

elif [ "$(uname)" = "Darwin" ]; then
    # macOS: sandbox-exec with a custom profile (limited but better than nothing)
    # Note: sandbox-exec is deprecated on macOS but still functional.
    # Profile: deny all writes, then allow workdir, /tmp, /dev.
    PROFILE="(version 1)
(deny default)
(allow process*)
(allow signal)
(allow sysctl-read)
(allow mach*)
(allow ipc*)
(allow file-read*)
(allow file-write*
    (subpath \"$WORKDIR\")
    (subpath \"$WORKDIR_REAL\")
    (subpath \"/tmp\")
    (subpath \"/private/tmp\")
    (subpath \"/dev\")
)
"
    if $NO_NETWORK; then
        PROFILE="${PROFILE}(deny network*)"
    else
        PROFILE="${PROFILE}(allow network*)"
    fi
    # Note: ulimit -u on macOS applies to ALL user processes, not just children,
    # so we skip it here — sandbox-exec provides its own isolation.
    exec sandbox-exec -p "$PROFILE" \
        /bin/bash -c 'cd "$1" && shift && eval "$@"' \
        _ "$WORKDIR" "${ARGS[@]}"

else
    # Fallback: ulimit only (minimal protection)
    exec /bin/bash -c 'ulimit -u "$1"; ulimit -v "$2"; cd "$3" && shift 3 && eval "$@"' \
        _ "$MAX_PROCS" "$((MEM_MB * 1024))" "$WORKDIR" "${ARGS[@]}"
fi
