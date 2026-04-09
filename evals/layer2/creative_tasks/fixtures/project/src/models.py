"""Data models for the application."""

from dataclasses import dataclass, field
from typing import List


@dataclass
class User:
    """Represents a user in the system."""
    name: str
    email: str
    active: bool = True


@dataclass
class Project:
    """Represents a project with members."""
    title: str
    description: str
    members: List[User] = field(default_factory=list)
