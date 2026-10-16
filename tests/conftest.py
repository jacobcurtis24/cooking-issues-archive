"""Shared fixtures for the characterization tests.

These tests pin the behaviour of the blog archive viewer (``ciw.web``) and the
scraper helpers (``ciw.blog``) against the real, tracked cooking_issues.db. The
database is only ever read.

The package is installed in editable mode (``pip install -e .``). Every test
still runs with cwd = repo root, set explicitly here rather than depending on
how pytest was launched.
"""

import hashlib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_FILE = REPO_ROOT / "cooking_issues.db"


@pytest.fixture(autouse=True)
def _repo_cwd(monkeypatch):
    """Run every test with cwd = repo root (independent of how pytest was launched)."""
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
    from ciw.web import create_app

    app = create_app()
    app.config["TESTING"] = True
    return app


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
