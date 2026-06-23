-- clive-harness-factory blackboard schema (spec §8)
-- Single source of truth. Inspectable, version-controllable (sqlite3 db .dump),
-- and the data source for the board. Roles never message each other; they read
-- and write this store, and the orchestrator sequences them.
--
-- Conventions: all timestamps are ISO-8601 UTC strings. JSON columns hold
-- machine-readable blobs the board and roles deserialize.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- The reigning harness spec.
CREATE TABLE IF NOT EXISTS champion (
    id          TEXT PRIMARY KEY,         -- spec content hash or "champion-<n>"
    spec_path   TEXT NOT NULL,            -- factory/specs/champion.yaml (or archived copy)
    promoted_at TEXT NOT NULL,
    scores_json TEXT NOT NULL DEFAULT '{}' -- aggregate scores at time of promotion
);

-- Proposed harness specs (one bounded change to the open block each).
CREATE TABLE IF NOT EXISTS candidates (
    id             TEXT PRIMARY KEY,
    parent         TEXT NOT NULL,         -- candidate id or "champion"
    spec_path      TEXT NOT NULL,         -- factory/specs/candidates/<id>.yaml
    stage          TEXT NOT NULL          -- proposed|evaluating|scored|awaiting_gate|promoted|rejected
                     CHECK (stage IN ('proposed','evaluating','scored','awaiting_gate','promoted','rejected')),
    created_at     TEXT NOT NULL,
    change_summary TEXT NOT NULL DEFAULT '',  -- one-line human description of the bounded change
    diff_json      TEXT NOT NULL DEFAULT '{}',-- structural diff of open vs parent (proposer-visible history)
    scores_json    TEXT NOT NULL DEFAULT '{}',-- computed scores after evaluation
    notes          TEXT NOT NULL DEFAULT ''
);

-- Scenario corpus mirror (the YAML files on disk are authoritative; this row
-- carries board + leakage state). Working set is proposer-visible; held-out is not.
CREATE TABLE IF NOT EXISTS scenarios (
    id            TEXT PRIMARY KEY,
    class         TEXT NOT NULL CHECK (class IN ('single','multi-clive')),
    partition     TEXT NOT NULL CHECK (partition IN ('working','held-out')),
    leakage_count INTEGER NOT NULL DEFAULT 0,  -- promotion decisions this held-out scenario influenced
    source        TEXT NOT NULL CHECK (source IN ('seed','mined')),
    spec_path     TEXT NOT NULL,               -- factory/scenarios/<part>/<id>.yaml
    goal          TEXT NOT NULL DEFAULT '',
    snapshot      TEXT NOT NULL DEFAULT '',     -- env image / provisioning spec
    check_path    TEXT NOT NULL DEFAULT '',     -- deterministic acceptance check
    active        INTEGER NOT NULL DEFAULT 1
);

-- One run = one (candidate, scenario, model) evaluation. The candidate clive's
-- own claim of success is recorded in clive_claim but NEVER scored (principle 2).
CREATE TABLE IF NOT EXISTS runs (
    id            TEXT PRIMARY KEY,
    candidate_id  TEXT NOT NULL REFERENCES candidates(id),
    scenario_id   TEXT NOT NULL REFERENCES scenarios(id),
    model         TEXT NOT NULL,               -- panel model name
    outcome       TEXT NOT NULL                -- pass|fail|error|budget_exceeded|blocked
                    CHECK (outcome IN ('pass','fail','error','budget_exceeded','blocked')),
    evidence_path TEXT NOT NULL DEFAULT '',    -- logs/runs/<run_id>/
    budget_used   INTEGER NOT NULL DEFAULT 0,  -- tokens consumed by the candidate clive
    created_at    TEXT NOT NULL,
    partition     TEXT NOT NULL DEFAULT 'working', -- denormalized for divergence math
    clive_claim   TEXT NOT NULL DEFAULT '',    -- clive's own success report (recorded, not scored)
    check_json    TEXT NOT NULL DEFAULT '{}',  -- acceptance-check evidence
    duration_s    REAL NOT NULL DEFAULT 0
);

-- Judge's semantic annotations on the surface deterministic checks cannot reach.
-- The Judge does NOT set pass/fail; it annotates.
CREATE TABLE IF NOT EXISTS judge_notes (
    run_id     TEXT NOT NULL REFERENCES runs(id),
    flags_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id)
);

-- Human promotion decisions at the gate (the one write action of the board).
CREATE TABLE IF NOT EXISTS promotions (
    candidate_id TEXT NOT NULL REFERENCES candidates(id),
    decision     TEXT NOT NULL CHECK (decision IN ('promote','reject','defer')),
    operator     TEXT NOT NULL,
    rationale    TEXT NOT NULL DEFAULT '',
    decided_at   TEXT NOT NULL,
    PRIMARY KEY (candidate_id, decided_at)
);

-- Log of re-grounding proxies against production (arbitration loop, §9).
CREATE TABLE IF NOT EXISTS recalibrations (
    at                    TEXT NOT NULL,
    note                  TEXT NOT NULL DEFAULT '',
    production_window      TEXT NOT NULL DEFAULT '',
    proxy_correlation_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (at)
);

-- Token / compute spend, for the cost-burn meter and budget caps.
CREATE TABLE IF NOT EXISTS budget_ledger (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    role_or_run TEXT NOT NULL,               -- 'proposer' | 'run:<run_id>' | 'judge' | ...
    tokens      INTEGER NOT NULL DEFAULT 0,
    cost        REAL NOT NULL DEFAULT 0,
    notes       TEXT NOT NULL DEFAULT ''
);

-- Negative-safety check trips. Any high/critical severity blocks promotion.
CREATE TABLE IF NOT EXISTS safety_flags (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id   TEXT NOT NULL REFERENCES runs(id),
    kind     TEXT NOT NULL,                  -- out_of_scope_path|grader_heldout_access|unrequested_port|destructive_op|budget_exceeded|...
    detail   TEXT NOT NULL DEFAULT '',
    severity TEXT NOT NULL CHECK (severity IN ('info','low','medium','high','critical'))
);

CREATE INDEX IF NOT EXISTS idx_runs_candidate ON runs(candidate_id);
CREATE INDEX IF NOT EXISTS idx_runs_scenario  ON runs(scenario_id);
CREATE INDEX IF NOT EXISTS idx_safety_run     ON safety_flags(run_id);
CREATE INDEX IF NOT EXISTS idx_candidates_stage ON candidates(stage);
