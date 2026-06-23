"""Disposable environment providers (spec §6, §11)."""
from __future__ import annotations

from ..common import config
from .base import EnvHandle, EnvProvider


def get_provider(name: str | None = None) -> EnvProvider:
    """Return the configured env provider. Defaults to config env.provider."""
    name = name or config.load_config().get("env", {}).get("provider", "local")
    if name == "local":
        from .local_sandbox import LocalSandboxProvider
        return LocalSandboxProvider()
    if name == "docker":
        from .docker_env import DockerEnvProvider
        return DockerEnvProvider()
    raise ValueError(f"unknown env provider: {name!r} (expected 'local' or 'docker')")


__all__ = ["EnvHandle", "EnvProvider", "get_provider"]
