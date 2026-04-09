"""Utility functions for the application."""


def format_name(name: str) -> str:
    """Capitalize and strip whitespace from a name."""
    return name.strip().title()


def calculate_total(items: list) -> float:
    """Sum numeric values in a list."""
    return sum(float(x) for x in items)


def truncate(text: str, max_len: int = 80) -> str:
    """Truncate text to max_len characters with ellipsis."""
    if len(text) <= max_len:
        return text
    return text[:max_len - 3] + "..."
