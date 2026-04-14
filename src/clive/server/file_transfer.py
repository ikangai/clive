# server/file_transfer.py
"""File transfer utilities for agent-to-agent file exchange."""

import logging
import os
import subprocess
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class FileTransferResult:
    success: bool
    local_path: str
    remote_path: str
    error: str = ""


def build_scp_command(host: str, remote_path: str, local_dir: str, port: int | None = None) -> list[str]:
    """Build an SCP command as a list for subprocess (no shell interpolation)."""
    parts = ["scp", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10"]
    if port:
        parts.extend(["-P", str(port)])
    parts.append(f"{host}:{remote_path}")
    parts.append(local_dir)
    return parts


def transfer_files(
    host: str,
    remote_files: list[str],
    local_dir: str,
    port: int | None = None,
    timeout: int = 30,
    dry_run: bool = False,
) -> list[FileTransferResult]:
    """Transfer files from a remote host to local directory.

    Args:
        host: SSH host (user@host format)
        remote_files: list of remote file paths
        local_dir: local directory to store files
        port: SSH port override
        timeout: per-file transfer timeout
        dry_run: if True, skip actual transfer

    Returns:
        list of FileTransferResult for each file
    """
    os.makedirs(local_dir, exist_ok=True)
    results = []

    for remote_path in remote_files:
        filename = os.path.basename(remote_path)
        local_path = os.path.join(local_dir, filename)

        if dry_run:
            results.append(FileTransferResult(
                success=True,
                local_path=local_path,
                remote_path=remote_path,
            ))
            continue

        cmd = build_scp_command(host, remote_path, local_dir, port=port)
        try:
            subprocess.run(
                cmd,
                capture_output=True, text=True,
                timeout=timeout,
            )
            if os.path.exists(local_path):
                results.append(FileTransferResult(
                    success=True,
                    local_path=local_path,
                    remote_path=remote_path,
                ))
                log.info("Transferred %s -> %s", remote_path, local_path)
            else:
                results.append(FileTransferResult(
                    success=False,
                    local_path=local_path,
                    remote_path=remote_path,
                    error="File not found after transfer",
                ))
        except subprocess.TimeoutExpired:
            results.append(FileTransferResult(
                success=False,
                local_path=local_path,
                remote_path=remote_path,
                error=f"Transfer timed out after {timeout}s",
            ))
        except Exception as e:
            results.append(FileTransferResult(
                success=False,
                local_path=local_path,
                remote_path=remote_path,
                error=str(e),
            ))

    return results
