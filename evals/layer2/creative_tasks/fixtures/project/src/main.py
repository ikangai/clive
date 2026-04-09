#!/usr/bin/env python3
"""Main application entry point."""

import sys
from utils import format_name, calculate_total


def greet(name: str) -> str:
    """Return a greeting for the given name."""
    return f"Hello, {format_name(name)}!"


def process_items(items: list) -> dict:
    """Process a list of items and return summary statistics."""
    total = calculate_total(items)
    return {"count": len(items), "total": total, "average": total / len(items) if items else 0}


def main():
    """Run the main application loop."""
    if len(sys.argv) < 2:
        print("Usage: main.py <name>")
        sys.exit(1)
    print(greet(sys.argv[1]))


if __name__ == "__main__":
    main()
