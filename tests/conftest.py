"""Shared test fixtures for OpenGiraffe tests."""

import pytest

from core.database import Database
from core.models import Task, TaskStatus, TaskPriority, TaskSource


@pytest.fixture
def tmp_db(tmp_path):
    """Provide a fresh Database backed by a temporary SQLite file."""
    db_path = str(tmp_path / "test.db")
    return Database(db_path)


@pytest.fixture
def make_task():
    """Factory fixture: create a Task with sensible defaults, override via kwargs."""
    def _make(**kwargs):
        defaults = dict(
            title="Test task",
            description="A test task description",
            status=TaskStatus.PENDING,
            priority=TaskPriority.MEDIUM,
            source=TaskSource.MANUAL,
        )
        defaults.update(kwargs)
        return Task(**defaults)
    return _make
