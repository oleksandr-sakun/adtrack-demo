"""
Test fixtures.

Every test runs against a throwaway SQLite file and a mocked Meta transport.
No network, no credentials — `git clone && pytest` is green on a machine that
has never seen a Meta token. A test suite that needs live API access is a test
suite that gets skipped, and a skipped test protects nothing.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    """Point settings.db_path at a fresh file for each test."""
    from app import config

    db = tmp_path / "test.db"
    monkeypatch.setattr(config.settings, "db_path", str(db))
    monkeypatch.setattr(config.settings, "max_delivery_attempts", 3)
    monkeypatch.setattr(config.settings, "meta_pixel_id", "1234567890")
    monkeypatch.setattr(config.settings, "meta_access_token", "test-token")
    monkeypatch.setattr(config.settings, "meta_test_event_code", "")

    from app.store import init_db
    init_db()
    yield db


@pytest.fixture
def no_sleep(monkeypatch):
    """Collapse backoff to nothing. Tests assert behaviour, not wall time."""
    async def instant(_seconds):
        return None

    monkeypatch.setattr("app.capi.asyncio.sleep", instant)
