"""Tests for main module."""

from src.main import greet, process_items


def test_greet():
    assert greet("alice") == "Hello, Alice!"


def test_process_items():
    result = process_items([10, 20, 30])
    assert result["count"] == 3
    assert result["total"] == 60
