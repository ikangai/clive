# tests/test_agent_file_transfer.py
import os
from protocol import encode
from remote import parse_remote_files
from server.file_transfer import build_scp_command, transfer_files, FileTransferResult

def test_parse_remote_files():
    screen = "\n".join([
        encode("turn", {"state": "done"}),
        encode("file", {"name": "/tmp/result.csv"}),
        encode("file", {"name": "/tmp/report.pdf"}),
    ])
    files = parse_remote_files(screen)
    assert files == ["/tmp/result.csv", "/tmp/report.pdf"]

def test_build_scp_command():
    cmd = build_scp_command("user@host", "/tmp/result.csv", "/local/session/")
    assert isinstance(cmd, list)
    assert cmd[0] == "scp"
    assert "user@host:/tmp/result.csv" in cmd
    assert "/local/session/" in cmd

def test_build_scp_command_with_port():
    cmd = build_scp_command("user@host", "/tmp/file.txt", "/local/", port=2222)
    assert "-P" in cmd
    assert "2222" in cmd

def test_transfer_result_success():
    result = FileTransferResult(success=True, local_path="/local/file.csv", remote_path="/tmp/file.csv")
    assert result.success

def test_transfer_result_failure():
    result = FileTransferResult(success=False, local_path="", remote_path="/tmp/file.csv", error="Connection refused")
    assert not result.success
    assert "Connection" in result.error

def test_transfer_files_dry_run(tmp_path):
    """Dry run should return success without actually transferring."""
    results = transfer_files(
        host="user@host",
        remote_files=["/tmp/a.txt", "/tmp/b.txt"],
        local_dir=str(tmp_path),
        dry_run=True,
    )
    assert len(results) == 2
    assert all(r.success for r in results)
