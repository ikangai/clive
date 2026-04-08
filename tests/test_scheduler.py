"""Tests for cron scheduling system."""
import os
import json
import pytest
from scheduler import (
    add_schedule, list_schedules, remove_schedule, get_history,
    pause_schedule, resume_schedule, cleanup_history, _auto_name,
    validate_cron, get_health,
)


def _mock_cron(monkeypatch):
    """Mock crontab calls to avoid modifying real crontab."""
    monkeypatch.setattr("scheduler._install_cron", lambda e: None)
    monkeypatch.setattr("scheduler._uninstall_cron", lambda n: None)


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr("scheduler.SCHEDULE_DIR", str(tmp_path / "schedules"))
    monkeypatch.setattr("scheduler.RESULTS_DIR", str(tmp_path / "results"))
    _mock_cron(monkeypatch)


# ─── Basic CRUD ───────────────────────────────────────────────────────────────

def test_add_schedule(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    entry = add_schedule("check disk", "0 * * * *", name="disk")
    assert entry["name"] == "disk"
    assert entry["cron"] == "0 * * * *"
    assert entry["active"] is True
    assert os.path.exists(str(tmp_path / "schedules" / "disk.json"))


def test_add_with_notify(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    entry = add_schedule("check", "0 * * * *", name="check", notify="email:admin@co.com")
    assert entry["notify"] == "email:admin@co.com"


def test_list_schedules(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    add_schedule("t1", "0 * * * *", name="t1")
    add_schedule("t2", "*/5 * * * *", name="t2")
    schedules = list_schedules()
    assert len(schedules) == 2


def test_remove_schedule(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    add_schedule("t1", "0 * * * *", name="t1")
    assert remove_schedule("t1") is True
    assert remove_schedule("nonexistent") is False


# ─── Pause / Resume ──────────────────────────────────────────────────────────

def test_pause_schedule(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    add_schedule("t1", "0 * * * *", name="t1")
    assert pause_schedule("t1") is True
    with open(str(tmp_path / "schedules" / "t1.json")) as f:
        data = json.load(f)
    assert data["active"] is False


def test_resume_schedule(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    add_schedule("t1", "0 * * * *", name="t1")
    pause_schedule("t1")
    assert resume_schedule("t1") is True
    with open(str(tmp_path / "schedules" / "t1.json")) as f:
        data = json.load(f)
    assert data["active"] is True


def test_pause_nonexistent(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    assert pause_schedule("nope") is False


# ─── History ──────────────────────────────────────────────────────────────────

def test_get_history(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    # Create fake result files
    results = tmp_path / "results" / "disk"
    results.mkdir(parents=True)
    (results / "20260407_120000.json").write_text(json.dumps({
        "timestamp": "20260407_120000", "status": "success", "duration_seconds": 5,
    }))
    (results / "20260407_130000.json").write_text(json.dumps({
        "timestamp": "20260407_130000", "status": "failed", "error": "timeout",
    }))

    history = get_history("disk", limit=10)
    assert len(history) == 2
    assert history[0]["timestamp"] == "20260407_130000"  # most recent first
    assert history[0]["status"] == "failed"


def test_get_history_malformed(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    results = tmp_path / "results" / "bad"
    results.mkdir(parents=True)
    (results / "20260407_120000.json").write_text("not json at all")

    history = get_history("bad")
    assert len(history) == 1
    assert history[0]["status"] == "parse_error"


# ─── Cleanup ──────────────────────────────────────────────────────────────────

def test_cleanup_history(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    results = tmp_path / "results" / "old"
    results.mkdir(parents=True)
    # Create a file and backdate it
    f = results / "old_result.json"
    f.write_text("{}")
    os.utime(str(f), (0, 0))  # set mtime to epoch

    removed = cleanup_history("old", retention_days=1)
    assert removed == 1
    assert not f.exists()


# ─── Auto-naming ──────────────────────────────────────────────────────────────

def test_auto_name():
    assert _auto_name("check disk usage /tmp") == "check_disk_usage__tmp"
    assert "/" not in _auto_name("path/to/thing")
    assert len(_auto_name("a" * 100)) <= 30


# ─── Cron validation ─────────────────────────────────────────────────────────

def test_valid_cron():
    assert validate_cron("0 * * * *") is True
    assert validate_cron("*/5 * * * *") is True
    assert validate_cron("0 9 * * 1-5") is True
    assert validate_cron("30 2 1 */3 *") is True


def test_invalid_cron():
    assert validate_cron("banana") is False
    assert validate_cron("* * *") is False  # only 3 fields
    assert validate_cron("") is False


def test_add_rejects_invalid_cron(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="Invalid cron"):
        add_schedule("task", "not_a_cron", name="bad")


# ─── Health stats ─────────────────────────────────────────────────────────────

def test_health_all_success(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    results = tmp_path / "results" / "healthy"
    results.mkdir(parents=True)
    for i in range(5):
        (results / f"2026040{i}_120000.json").write_text(json.dumps({
            "status": "success", "duration_seconds": 3,
        }))
    health = get_health("healthy")
    assert health["success_rate"] == 1.0
    assert health["failure_streak"] == 0
    assert health["avg_duration"] == 3.0


def test_health_failure_streak(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    results = tmp_path / "results" / "failing"
    results.mkdir(parents=True)
    (results / "20260401_120000.json").write_text(json.dumps({"status": "success"}))
    (results / "20260402_120000.json").write_text(json.dumps({"status": "failed"}))
    (results / "20260403_120000.json").write_text(json.dumps({"status": "failed"}))
    (results / "20260404_120000.json").write_text(json.dumps({"status": "failed"}))
    health = get_health("failing")
    assert health["failure_streak"] == 3  # 3 most recent are failures
    assert health["success_rate"] == 0.25


def test_health_empty(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    health = get_health("nonexistent")
    assert health["runs"] == 0


# ─── Update detection ────────────────────────────────────────────────────────

def test_update_detection(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    add_schedule("task", "0 * * * *", name="t1")
    entry = add_schedule("task", "*/5 * * * *", name="t1")
    assert entry.get("_updated_from") == "0 * * * *"


# ─── List with last run ──────────────────────────────────────────────────────

def test_list_includes_last_run(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    add_schedule("t1", "0 * * * *", name="t1")
    # Add a result
    results = tmp_path / "results" / "t1"
    results.mkdir(parents=True, exist_ok=True)
    (results / "20260407_120000.json").write_text(json.dumps({
        "timestamp": "20260407_120000", "status": "success",
    }))

    schedules = list_schedules()
    assert schedules[0].get("last_run") == "20260407_120000"
    assert schedules[0].get("last_status") == "success"
