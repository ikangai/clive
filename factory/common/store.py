"""The blackboard: a thin, explicit SQLite access layer (spec §8).

Roles never message each other; they read and write this store, and the
orchestrator sequences them. This module is CRUD only — scoring/divergence math
lives in scoring.py so the grader logic is inspectable in one place.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from . import paths


def now_iso() -> str:
    # Microsecond precision so the propose trigger's `created_at > since` boundary
    # and ORDER BY created_at are exact (whole-second granularity dropped same-
    # second failures and made same-second ordering nondeterministic).
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _conn(db_path: Optional[str] = None) -> sqlite3.Connection:
    db_path = db_path or paths.DB_PATH
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


class Blackboard:
    """Single source of truth. Construct, use, close (or use as a context manager)."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or paths.DB_PATH
        self.conn = _conn(self.db_path)

    # -- lifecycle ----------------------------------------------------------
    def __enter__(self) -> "Blackboard":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        try:
            self.conn.commit()
            self.conn.close()
        except Exception:
            pass

    def init_db(self) -> None:
        with open(paths.SCHEMA_SQL, "r", encoding="utf-8") as fh:
            self.conn.executescript(fh.read())
        self.conn.commit()

    def _exec(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        cur = self.conn.execute(sql, tuple(params))
        self.conn.commit()
        return cur

    def _all(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, tuple(params)).fetchall()]

    def _one(self, sql: str, params: Iterable[Any] = ()) -> Optional[dict]:
        r = self.conn.execute(sql, tuple(params)).fetchone()
        return dict(r) if r else None

    # -- champion -----------------------------------------------------------
    def get_champion(self) -> Optional[dict]:
        return self._one("SELECT * FROM champion ORDER BY promoted_at DESC LIMIT 1")

    def set_champion(self, id: str, spec_path: str, scores: dict | None = None) -> None:
        self._exec(
            "INSERT OR REPLACE INTO champion(id, spec_path, promoted_at, scores_json) "
            "VALUES (?,?,?,?)",
            (id, spec_path, now_iso(), json.dumps(scores or {})),
        )

    # -- candidates ---------------------------------------------------------
    def add_candidate(self, id: str, parent: str, spec_path: str, *,
                      change_summary: str = "", diff: dict | None = None,
                      stage: str = "proposed") -> None:
        self._exec(
            "INSERT INTO candidates(id, parent, spec_path, stage, created_at, "
            "change_summary, diff_json) VALUES (?,?,?,?,?,?,?)",
            (id, parent, spec_path, stage, now_iso(), change_summary,
             json.dumps(diff or {})),
        )

    def get_candidate(self, id: str) -> Optional[dict]:
        return self._one("SELECT * FROM candidates WHERE id = ?", (id,))

    def list_candidates(self, stage: Optional[str] = None) -> list[dict]:
        if stage:
            return self._all("SELECT * FROM candidates WHERE stage = ? ORDER BY created_at", (stage,))
        return self._all("SELECT * FROM candidates ORDER BY created_at")

    def set_stage(self, id: str, stage: str) -> None:
        self._exec("UPDATE candidates SET stage = ? WHERE id = ?", (stage, id))

    def set_candidate_scores(self, id: str, scores: dict) -> None:
        self._exec("UPDATE candidates SET scores_json = ? WHERE id = ?",
                   (json.dumps(scores), id))

    # -- scenarios ----------------------------------------------------------
    def upsert_scenario(self, id: str, *, cls: str, partition: str, source: str,
                        spec_path: str, goal: str = "", snapshot: str = "",
                        check_path: str = "") -> None:
        existing = self.get_scenario(id)
        leak = existing["leakage_count"] if existing else 0
        self._exec(
            "INSERT OR REPLACE INTO scenarios(id, class, partition, leakage_count, "
            "source, spec_path, goal, snapshot, check_path, active) "
            "VALUES (?,?,?,?,?,?,?,?,?,1)",
            (id, cls, partition, leak, source, spec_path, goal, snapshot, check_path),
        )

    def get_scenario(self, id: str) -> Optional[dict]:
        return self._one("SELECT * FROM scenarios WHERE id = ?", (id,))

    def list_scenarios(self, partition: Optional[str] = None,
                       active_only: bool = True) -> list[dict]:
        q = "SELECT * FROM scenarios WHERE 1=1"
        p: list[Any] = []
        if partition:
            q += " AND partition = ?"
            p.append(partition)
        if active_only:
            q += " AND active = 1"
        q += " ORDER BY id"
        return self._all(q, p)

    def increment_leakage(self, id: str, by: int = 1) -> None:
        self._exec("UPDATE scenarios SET leakage_count = leakage_count + ? WHERE id = ?",
                   (by, id))

    def retire_scenario(self, id: str) -> None:
        self._exec("UPDATE scenarios SET active = 0 WHERE id = ?", (id,))

    # -- runs ---------------------------------------------------------------
    def add_run(self, id: str, candidate_id: str, scenario_id: str, model: str,
                outcome: str, *, evidence_path: str = "", budget_used: int = 0,
                partition: str = "working", clive_claim: str = "",
                check_json: dict | None = None, duration_s: float = 0.0) -> None:
        self._exec(
            "INSERT INTO runs(id, candidate_id, scenario_id, model, outcome, "
            "evidence_path, budget_used, created_at, partition, clive_claim, "
            "check_json, duration_s) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (id, candidate_id, scenario_id, model, outcome, evidence_path,
             budget_used, now_iso(), partition, clive_claim,
             json.dumps(check_json or {}), duration_s),
        )

    def get_run(self, id: str) -> Optional[dict]:
        return self._one("SELECT * FROM runs WHERE id = ?", (id,))

    def runs_for_candidate(self, candidate_id: str) -> list[dict]:
        return self._all("SELECT * FROM runs WHERE candidate_id = ? ORDER BY created_at",
                         (candidate_id,))

    def all_runs(self) -> list[dict]:
        return self._all("SELECT * FROM runs ORDER BY created_at")

    def recent_failures(self, limit: int = 20) -> list[dict]:
        """Recent failing/erroring runs — the proposer's view of where reality
        surfaced gaps (working set only; the proposer is blind to held-out)."""
        return self._all(
            "SELECT r.*, s.goal AS scenario_goal FROM runs r JOIN scenarios s "
            "ON r.scenario_id = s.id WHERE r.outcome IN ('fail','error','blocked') "
            "AND r.partition = 'working' ORDER BY r.created_at DESC LIMIT ?",
            (limit,),
        )

    # -- judge --------------------------------------------------------------
    def add_judge_note(self, run_id: str, flags: dict) -> None:
        self._exec(
            "INSERT OR REPLACE INTO judge_notes(run_id, flags_json, created_at) "
            "VALUES (?,?,?)", (run_id, json.dumps(flags), now_iso()))

    def judge_note(self, run_id: str) -> Optional[dict]:
        return self._one("SELECT * FROM judge_notes WHERE run_id = ?", (run_id,))

    # -- promotions ---------------------------------------------------------
    def add_promotion(self, candidate_id: str, decision: str, operator: str,
                      rationale: str = "") -> None:
        self._exec(
            "INSERT INTO promotions(candidate_id, decision, operator, rationale, "
            "decided_at) VALUES (?,?,?,?,?)",
            (candidate_id, decision, operator, rationale, now_iso()))

    def promotions(self) -> list[dict]:
        return self._all("SELECT * FROM promotions ORDER BY decided_at DESC")

    # -- recalibrations -----------------------------------------------------
    def add_recalibration(self, note: str, production_window: str = "",
                          proxy_correlation: dict | None = None) -> None:
        self._exec(
            "INSERT INTO recalibrations(at, note, production_window, "
            "proxy_correlation_json) VALUES (?,?,?,?)",
            (now_iso(), note, production_window, json.dumps(proxy_correlation or {})))

    # -- budget -------------------------------------------------------------
    def add_budget(self, role_or_run: str, tokens: int, cost: float = 0.0,
                   notes: str = "") -> None:
        self._exec(
            "INSERT INTO budget_ledger(at, role_or_run, tokens, cost, notes) "
            "VALUES (?,?,?,?,?)", (now_iso(), role_or_run, tokens, cost, notes))

    def budget_totals(self) -> dict:
        r = self._one("SELECT COALESCE(SUM(tokens),0) AS tokens, "
                      "COALESCE(SUM(cost),0) AS cost FROM budget_ledger")
        return r or {"tokens": 0, "cost": 0}

    def budget_entries(self) -> list[dict]:
        return self._all("SELECT * FROM budget_ledger ORDER BY at")

    # -- safety -------------------------------------------------------------
    def add_safety_flag(self, run_id: str, kind: str, detail: str,
                        severity: str) -> None:
        self._exec(
            "INSERT INTO safety_flags(run_id, kind, detail, severity) "
            "VALUES (?,?,?,?)", (run_id, kind, detail, severity))

    def safety_flags_for_candidate(self, candidate_id: str) -> list[dict]:
        return self._all(
            "SELECT sf.*, r.scenario_id, r.model FROM safety_flags sf "
            "JOIN runs r ON sf.run_id = r.id WHERE r.candidate_id = ? "
            "ORDER BY sf.id", (candidate_id,))

    def all_safety_flags(self) -> list[dict]:
        return self._all(
            "SELECT sf.*, r.candidate_id, r.scenario_id, r.model FROM safety_flags sf "
            "JOIN runs r ON sf.run_id = r.id ORDER BY sf.id DESC")
