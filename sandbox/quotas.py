# sandbox/quotas.py
"""Per-user resource quotas for clive sessions."""

import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class UserQuota:
    max_tokens_per_day: int = 100_000
    max_concurrent: int = 3
    max_disk_mb: int = 1024
    max_wall_seconds: int = 3600


@dataclass
class QuotaResult:
    allowed: bool
    reason: str = ""


DEFAULT_QUOTAS = UserQuota()


def check_quota(
    quota: UserQuota,
    tokens_used: int = 0,
    concurrent: int = 0,
    disk_mb: float = 0,
    wall_seconds: float = 0,
) -> QuotaResult:
    """Check current usage against quota limits."""
    if tokens_used > quota.max_tokens_per_day:
        return QuotaResult(allowed=False, reason=f"Token limit exceeded: {tokens_used}/{quota.max_tokens_per_day}")
    if concurrent >= quota.max_concurrent:
        return QuotaResult(allowed=False, reason=f"Concurrent session limit exceeded: {concurrent}/{quota.max_concurrent}")
    if disk_mb > quota.max_disk_mb:
        return QuotaResult(allowed=False, reason=f"Disk usage limit exceeded: {disk_mb:.0f}MB/{quota.max_disk_mb}MB")
    if wall_seconds > quota.max_wall_seconds:
        return QuotaResult(allowed=False, reason=f"Wall time limit exceeded: {wall_seconds:.0f}s/{quota.max_wall_seconds}s")
    return QuotaResult(allowed=True)


def load_quotas(path: str) -> dict[str, UserQuota]:
    """Load per-user quotas from a YAML file. Returns dict mapping username to UserQuota."""
    result = {"default": DEFAULT_QUOTAS}
    p = Path(path)
    if not p.exists():
        log.debug("Quota file %s not found, using defaults", path)
        return result
    try:
        import yaml
        with open(p) as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            return result
        for username, settings in data.items():
            if isinstance(settings, dict):
                result[username] = UserQuota(**{
                    k: v for k, v in settings.items()
                    if k in UserQuota.__dataclass_fields__
                })
    except ImportError:
        log.warning("PyYAML not installed, cannot load quota file")
    except Exception as e:
        log.warning("Failed to load quotas from %s: %s", path, e)
    return result
