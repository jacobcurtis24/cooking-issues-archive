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
def allow_real_paths(monkeypatch):
    """Opt in to the real repo config/paths (read-only tests only; see ciw.config guard)."""
    from ciw.config import ALLOW_REAL_PATHS_ENV

    monkeypatch.setenv(ALLOW_REAL_PATHS_ENV, "1")


@pytest.fixture(autouse=True)
def _real_paths_marker(request, monkeypatch):
    """``@pytest.mark.real_paths`` is the same opt-in as the ``allow_real_paths`` fixture."""
    if request.node.get_closest_marker("real_paths"):
        request.getfixturevalue("allow_real_paths")


@pytest.fixture
def flask_app(tmp_cfg):
    """The blog app on a tmp config whose only real path is the (read-only) blog database."""
    from ciw.web import create_app

    app = create_app(tmp_cfg.model_copy(update={"blog_db": DB_FILE}))
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


def write_config(root: Path, extra: str = "") -> None:
    """Write ``root/config.yaml`` with audio/clips dirs inside ``root`` (never the real ones)."""
    (root / "config.yaml").write_text(
        f"audio_dir: {root / 'audio'}\nclips_dir: {root / 'clips'}\n{extra}"
    )


@pytest.fixture
def tmp_cfg(tmp_path, monkeypatch):
    """A Config rooted in tmp_path (data/ lives there); no Patreon secret."""
    from ciw.config import load_config

    for var in ("PATREON_FEED_URL", "HF_TOKEN", "ANTHROPIC_API_KEY", "NTFY_TOPIC"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CIW_ROOT", str(tmp_path))
    # Without this the default audio_dir/clips_dir are the real /mnt/ssd0/ciw (guarded).
    write_config(tmp_path)
    return load_config(tmp_path, use_dotenv=False)


@pytest.fixture
def blog_cfg(tmp_cfg, tmp_path):
    """tmp config (CIW_ROOT) whose only real path is the read-only blog database."""
    from ciw.config import load_config

    write_config(tmp_path, f"blog_db: {DB_FILE}\n")
    return load_config(tmp_path, use_dotenv=False)


@pytest.fixture
def with_patreon(tmp_cfg, monkeypatch):
    """Same config, with the synthetic Patreon URL configured."""
    from fakeweb import PATREON_URL

    from ciw.config import Secrets

    monkeypatch.setenv("PATREON_FEED_URL", PATREON_URL)
    secrets = Secrets(patreon_feed_url=PATREON_URL)
    return tmp_cfg.model_copy(update={"secrets": secrets})
