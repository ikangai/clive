"""Tests for file inspection and schema detection."""
import json
import os
from file_inspect import sniff_file, sniff_session_files, format_file_context


def test_sniff_json_array(tmp_path):
    f = tmp_path / "data.json"
    f.write_text(json.dumps([{"name": "alice", "age": 30}, {"name": "bob", "age": 25}]))
    info = sniff_file(str(f))
    assert info["type"] == "json_array"
    assert info["items"] == 2
    assert "name" in info["schema"]
    assert "age" in info["schema"]


def test_sniff_json_object(tmp_path):
    f = tmp_path / "config.json"
    f.write_text(json.dumps({"host": "localhost", "port": 8080}))
    info = sniff_file(str(f))
    assert info["type"] == "json_object"
    assert "host" in info["schema"]


def test_sniff_csv(tmp_path):
    f = tmp_path / "data.csv"
    f.write_text("name,age,city\nalice,30,london\nbob,25,paris\n")
    info = sniff_file(str(f))
    assert info["type"] == "csv"
    assert "name" in info["schema"]
    assert info["lines"] >= 3


def test_sniff_plain_text(tmp_path):
    f = tmp_path / "notes.txt"
    f.write_text("line 1\nline 2\nline 3\n")
    info = sniff_file(str(f))
    assert info["type"] == "text"
    assert info["lines"] == 3


def test_sniff_empty_file(tmp_path):
    f = tmp_path / "empty.txt"
    f.write_text("")
    info = sniff_file(str(f))
    assert info["type"] == "empty"


def test_sniff_nonexistent():
    info = sniff_file("/nonexistent/file.txt")
    assert info["size"] == 0


def test_sniff_session_files(tmp_path):
    # Create some files
    (tmp_path / "data.json").write_text('[{"x": 1}]')
    (tmp_path / "_result_1.json").write_text('{"status": "ok"}')
    (tmp_path / "_log_1.txt").write_text("log output")
    (tmp_path / "_script_2.sh").write_text("#!/bin/bash")

    files = sniff_session_files(str(tmp_path), "1")
    names = [f["path"] for f in files]
    assert "data.json" in names
    assert "_result_1.json" in names
    assert "_log_1.txt" in names
    # _script_2.sh should NOT be included (belongs to subtask 2)
    assert "_script_2.sh" not in names


def test_format_file_context():
    files = [
        {"path": "data.json", "type": "json_array", "items": 47,
         "schema": ["name", "amount", "city"], "size": 1024, "lines": 48,
         "preview": "[47 objects, keys: name, amount, city]"},
        {"path": "log.txt", "type": "text", "size": 256, "lines": 12,
         "preview": "2026-04-01 started..."},
    ]
    ctx = format_file_context(files)
    assert "data.json" in ctx
    assert "json_array" in ctx
    assert "name" in ctx
    assert "log.txt" in ctx


def test_format_empty_files():
    assert format_file_context([]) == ""
