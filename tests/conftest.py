"""Shared fixtures for the characterization tests.

These tests pin the behaviour of the blog archive viewer (app.py) and the
scraper helpers (scrape.py) against the real, tracked cooking_issues.db. The
database is only ever read.

app.py uses a cwd-relative DB_PATH, so every test runs with cwd = repo root,
set explicitly here rather than depending on how pytest was launched.

NOTE for the restructure (T2): REPO_ROOT / sys.path / the `app` and `scrape`
imports below are the only places that know where the code lives.
"""

import hashlib
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_FILE = REPO_ROOT / "cooking_issues.db"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(autouse=True)
def _repo_cwd(monkeypatch):
    """Run every test with cwd = repo root (app.py's DB_PATH is relative)."""
    monkeypatch.chdir(REPO_ROOT)


@pytest.fixture(scope="session", autouse=True)
def _db_untouched():
    """Fail the session if anything modified the real database."""
    before = hashlib.sha256(DB_FILE.read_bytes()).hexdigest()
    yield
    after = hashlib.sha256(DB_FILE.read_bytes()).hexdigest()
    assert before == after, "cooking_issues.db was modified during the test run"


@pytest.fixture
def flask_app():
    import app as app_module

    app_module.app.config["TESTING"] = True
    return app_module.app


@pytest.fixture
def client(flask_app):
    return flask_app.test_client()


@pytest.fixture
def db():
    """Read-only sqlite connection to the real database."""
    import sqlite3

    conn = sqlite3.connect(f"file:{DB_FILE}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()
