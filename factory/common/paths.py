"""Canonical filesystem paths for the factory. Everything is files + a SQLite
store, so path resolution is centralized here. No hidden state."""
from __future__ import annotations

import os

# factory/common/paths.py -> factory/
FACTORY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# parent of factory/ == the clive repo root
CLIVE_ROOT = os.path.dirname(FACTORY_ROOT)


def factory(*parts: str) -> str:
    return os.path.join(FACTORY_ROOT, *parts)


# Core locations
CONFIG_YAML = factory("config.yaml")
PANEL_YAML = factory("panel.yaml")
STORE_DIR = factory("store")
DB_PATH = factory("store", "blackboard.db")
SCHEMA_SQL = factory("store", "schema.sql")
SPECS_DIR = factory("specs")
CHAMPION_YAML = factory("specs", "champion.yaml")
CANDIDATES_DIR = factory("specs", "candidates")
SCENARIOS_DIR = factory("scenarios")
WORKING_DIR = factory("scenarios", "working")
HELD_OUT_DIR = factory("scenarios", "held-out")
STAGING_DIR = factory("scenarios", "staging")
CHECKS_DIR = factory("checks")
LOGS_DIR = factory("logs")
RUNS_DIR = factory("logs", "runs")
ROLES_DIR = factory("roles")


def run_evidence_dir(run_id: str) -> str:
    """Per-run evidence directory under logs/runs/<run_id>/."""
    d = os.path.join(RUNS_DIR, run_id)
    os.makedirs(d, exist_ok=True)
    return d


def resolve_clive_root(config_root: str) -> str:
    """Resolve the clive repo root from config (relative to factory/)."""
    if os.path.isabs(config_root):
        return os.path.normpath(config_root)
    return os.path.normpath(os.path.join(FACTORY_ROOT, config_root))
