"""Load factory config + panel. Thin wrappers over YAML so the rest of the
codebase never touches the files directly."""
from __future__ import annotations

import functools
from typing import Any

import yaml

from . import paths


@functools.lru_cache(maxsize=1)
def load_config() -> dict[str, Any]:
    with open(paths.CONFIG_YAML, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


@functools.lru_cache(maxsize=1)
def load_panel() -> dict[str, Any]:
    with open(paths.PANEL_YAML, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def panel_models() -> list[dict[str, Any]]:
    """The panel intelligences that drive candidate clive during optimisation."""
    return list(load_panel().get("panel", []))


def held_out_models() -> list[dict[str, Any]]:
    """Held-out model(s): never used during optimisation, only overfit detection."""
    return list(load_panel().get("held_out", []))


def smoke_model() -> dict[str, Any]:
    """The single panel model the smoke test drives the pipeline with."""
    for m in panel_models():
        if m.get("smoke"):
            return m
    models = panel_models()
    if not models:
        raise RuntimeError("panel.yaml has no panel models")
    return models[0]


def clive_python() -> str:
    cfg = load_config()["clive"]
    root = paths.resolve_clive_root(cfg.get("root", ".."))
    py = cfg.get("python", "python3")
    # Allow a venv-relative interpreter.
    if not py.startswith("/") and "/" in py:
        import os
        cand = os.path.join(root, py)
        if os.path.exists(cand):
            return cand
    return py


def clive_entry() -> tuple[str, str]:
    """Return (clive_root, clive_entry_abs_path)."""
    import os
    cfg = load_config()["clive"]
    root = paths.resolve_clive_root(cfg.get("root", ".."))
    return root, os.path.join(root, cfg.get("entry", "clive.py"))
