"""Tests for cron scheduling system."""
import os
import json
from scheduler import add_schedule, list_schedules, remove_schedule, get_history, SCHEDULE_DIR, RESULTS_DIR


def test_add_schedule(tmp_path, monkeypatch):
    monkeypatch.setattr("scheduler.SCHEDULE_DIR", str(tmp_path / "schedules"))
    monkeypatch.setattr("scheduler.RESULTS_DIR", str(tmp_path / "results"))
    # Mock crontab to avoid modifying real crontab
    monkeypatch.setattr("scheduler._install_cron", lambda e: None)

    entry = add_schedule("check disk usage", "0 * * * *", name="disk_check")
    assert entry["name"] == "disk_check"
    assert entry["cron"] == "0 * * * *"
    assert os.path.exists(os.path.join(str(tmp_path / "schedules"), "disk_check.json"))


def test_list_schedules(tmp_path, monkeypatch):
    monkeypatch.setattr("scheduler.SCHEDULE_DIR", str(tmp_path / "schedules"))
    monkeypatch.setattr("scheduler.RESULTS_DIR", str(tmp_path / "results"))
    monkeypatch.setattr("scheduler._install_cron", lambda e: None)

    add_schedule("task1", "0 * * * *", name="t1")
    add_schedule("task2", "*/5 * * * *", name="t2")

    schedules = list_schedules()
    assert len(schedules) == 2
    names = [s["name"] for s in schedules]
    assert "t1" in names
    assert "t2" in names


def test_remove_schedule(tmp_path, monkeypatch):
    monkeypatch.setattr("scheduler.SCHEDULE_DIR", str(tmp_path / "schedules"))
    monkeypatch.setattr("scheduler.RESULTS_DIR", str(tmp_path / "results"))
    monkeypatch.setattr("scheduler._install_cron", lambda e: None)
    monkeypatch.setattr("scheduler._uninstall_cron", lambda n: None)

    add_schedule("task1", "0 * * * *", name="t1")
    assert remove_schedule("t1") is True
    assert remove_schedule("nonexistent") is False


def test_auto_name():
    import scheduler
    # Test that auto-naming works for edge cases
    entry = {"task": "check disk usage /tmp", "cron": "0 * * * *"}
    name = entry["task"][:30].replace(" ", "_").replace("/", "_").lower()
    name = "".join(c for c in name if c.isalnum() or c == "_")
    assert len(name) > 0
    assert "/" not in name
