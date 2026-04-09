# tests/test_quotas.py
from sandbox.quotas import UserQuota, check_quota, DEFAULT_QUOTAS

def test_default_quotas_exist():
    q = DEFAULT_QUOTAS
    assert q.max_tokens_per_day == 100_000
    assert q.max_concurrent == 3
    assert q.max_disk_mb == 1024
    assert q.max_wall_seconds == 3600

def test_check_quota_passes_under_limit():
    q = UserQuota(max_tokens_per_day=1000, max_concurrent=2, max_disk_mb=100, max_wall_seconds=60)
    result = check_quota(q, tokens_used=500, concurrent=1, disk_mb=50, wall_seconds=30)
    assert result.allowed

def test_check_quota_fails_over_token_limit():
    q = UserQuota(max_tokens_per_day=1000, max_concurrent=2, max_disk_mb=100, max_wall_seconds=60)
    result = check_quota(q, tokens_used=1500, concurrent=1, disk_mb=50, wall_seconds=30)
    assert not result.allowed
    assert "token" in result.reason.lower()

def test_check_quota_fails_over_concurrent_limit():
    q = UserQuota(max_tokens_per_day=1000, max_concurrent=2, max_disk_mb=100, max_wall_seconds=60)
    result = check_quota(q, tokens_used=0, concurrent=3, disk_mb=0, wall_seconds=0)
    assert not result.allowed
    assert "concurrent" in result.reason.lower()

def test_check_quota_fails_over_disk_limit():
    q = UserQuota(max_tokens_per_day=1000, max_concurrent=2, max_disk_mb=100, max_wall_seconds=60)
    result = check_quota(q, tokens_used=0, concurrent=0, disk_mb=200, wall_seconds=0)
    assert not result.allowed
    assert "disk" in result.reason.lower()

def test_check_quota_fails_over_wall_time_limit():
    q = UserQuota(max_tokens_per_day=1000, max_concurrent=2, max_disk_mb=100, max_wall_seconds=60)
    result = check_quota(q, tokens_used=0, concurrent=0, disk_mb=0, wall_seconds=120)
    assert not result.allowed
    assert "wall" in result.reason.lower() or "time" in result.reason.lower()

def test_load_quotas_from_yaml(tmp_path):
    """Should load per-user quotas from a YAML file."""
    from sandbox.quotas import load_quotas
    yaml_content = """
default:
  max_tokens_per_day: 50000
  max_concurrent: 2
  max_disk_mb: 512
  max_wall_seconds: 1800
testuser:
  max_tokens_per_day: 200000
  max_concurrent: 5
  max_disk_mb: 2048
  max_wall_seconds: 7200
"""
    quota_file = tmp_path / "quotas.yaml"
    quota_file.write_text(yaml_content)
    quotas = load_quotas(str(quota_file))
    assert quotas["testuser"].max_tokens_per_day == 200000
    assert quotas["default"].max_concurrent == 2

def test_load_quotas_missing_file():
    """Missing quota file should return default quotas."""
    from sandbox.quotas import load_quotas
    quotas = load_quotas("/nonexistent/quotas.yaml")
    assert "default" in quotas
    assert quotas["default"].max_tokens_per_day == DEFAULT_QUOTAS.max_tokens_per_day
