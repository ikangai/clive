"""Tests for command safety blocklist."""
from executor import _check_command_safety


def test_blocks_rm_rf_root():
    assert _check_command_safety("rm -rf /") is not None


def test_blocks_shutdown():
    assert _check_command_safety("shutdown -h now") is not None


def test_blocks_reboot():
    assert _check_command_safety("reboot") is not None


def test_blocks_mkfs():
    assert _check_command_safety("mkfs.ext4 /dev/sda1") is not None


def test_blocks_dd_to_device():
    assert _check_command_safety("dd if=/dev/zero of=/dev/sda") is not None


def test_allows_normal_rm():
    assert _check_command_safety("rm /tmp/clive/result.txt") is None


def test_allows_normal_commands():
    assert _check_command_safety("ls -la /tmp") is None
    assert _check_command_safety("grep -r TODO .") is None
    assert _check_command_safety("curl -s https://example.com") is None
    assert _check_command_safety("python3 -c 'print(1)'") is None


def test_allows_rm_rf_non_root():
    assert _check_command_safety("rm -rf /tmp/clive/session123") is None
