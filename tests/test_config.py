"""Config loader: defaults, CIW_ROOT override, cwd-independence, .env secrets, redact()."""

from pathlib import Path

import pytest

from ciw import config as cfgmod
from ciw.config import Config, load_config, parse_dotenv, project_root, redact

REPO_ROOT = Path(__file__).resolve().parent.parent

# These tests are about config semantics (defaults, path resolution) and never touch the paths
# they compute; the guard itself is tested in test_guard.py.
pytestmark = pytest.mark.real_paths


@pytest.fixture
def clean_env(monkeypatch):
    """Empty every secret var (restored afterwards even if load_dotenv sets them)."""
    for key in cfgmod.SECRET_ENV_VARS:
        monkeypatch.setenv(key, "")
    monkeypatch.delenv("CIW_ROOT", raising=False)


def test_project_root_found_by_walking_up(clean_env):
    assert project_root() == REPO_ROOT


def test_project_root_env_override(clean_env, monkeypatch, tmp_path):
    monkeypatch.setenv("CIW_ROOT", str(tmp_path))
    assert project_root() == tmp_path.resolve()


def test_repo_config_values(clean_env):
    cfg = load_config()
    assert cfg.root == REPO_ROOT
    assert cfg.blog_db == REPO_ROOT / "cooking_issues.db"
    assert cfg.data_dir == REPO_ROOT / "data"
    assert cfg.state_db == REPO_ROOT / "data" / "state.sqlite"
    assert cfg.audio_dir == Path("/mnt/ssd0/ciw/audio")
    assert cfg.clips_dir == Path("/mnt/ssd0/ciw/clips")
    assert cfg.apple_ids.acast == 1572356745
    assert cfg.apple_ids.hrn == 380317598
    assert cfg.poll_interval_hours == 6
    assert (cfg.asr.backend, cfg.asr.model, cfg.asr.batch_size) == ("whisperx", "large-v3", 8)
    assert (cfg.llm.model, cfg.llm.monthly_budget_usd) == ("claude-sonnet-5-5", 25)
    assert cfg.notify.server == "https://ntfy.sh"
    assert (cfg.notify.threshold, cfg.notify.clip_threshold) == (7, 9)
    assert (cfg.web.host, cfg.web.port, cfg.web.debug) == ("127.0.0.1", 5000, False)
    assert cfg.episodes_dir == REPO_ROOT / "data" / "episodes"


def test_defaults_without_config_file(clean_env, tmp_path):
    cfg = load_config(tmp_path)
    assert cfg == Config(root=tmp_path.resolve()).resolve_paths().model_copy(
        update={"secrets": cfg.secrets}
    )
    assert cfg.blog_db == tmp_path.resolve() / "cooking_issues.db"
    assert cfg.web.debug is False


def test_ciw_root_env_selects_config_and_paths(clean_env, monkeypatch, tmp_path):
    (tmp_path / "config.yaml").write_text(
        "blog_db: blog/x.db\ndata_dir: d\naudio_dir: /abs/audio\nweb:\n  port: 6001\n"
    )
    monkeypatch.setenv("CIW_ROOT", str(tmp_path))
    cfg = load_config()
    root = tmp_path.resolve()
    assert cfg.blog_db == root / "blog" / "x.db"
    assert cfg.data_dir == root / "d"
    assert cfg.audio_dir == Path("/abs/audio")  # absolute paths are left alone
    assert cfg.web.port == 6001
    assert cfg.web.host == "127.0.0.1"  # unspecified keys keep defaults


def test_paths_resolve_the_same_from_any_cwd(clean_env, monkeypatch, tmp_path):
    monkeypatch.chdir(REPO_ROOT)
    here = load_config()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    there = load_config()
    assert here == there
    assert there.blog_db.is_absolute() and there.blog_db.exists()


def test_unknown_key_is_rejected(clean_env, tmp_path):
    (tmp_path / "config.yaml").write_text("blogdb: typo.db\n")
    with pytest.raises(ValueError):
        load_config(tmp_path)


def test_dotenv_parsing():
    text = "# c\nA=1\nexport B='two words'\nC=\"x=y\"\n\nbad line\nD=\n"
    assert parse_dotenv(text) == {"A": "1", "B": "two words", "C": "x=y", "D": ""}


def test_secrets_come_from_dotenv_not_overriding_real_env(clean_env, monkeypatch, tmp_path):
    (tmp_path / ".env").write_text("HF_TOKEN=hf_from_file\nNTFY_TOPIC=topic-from-file\n")
    monkeypatch.setenv("NTFY_TOPIC", "topic-from-env")
    cfg = load_config(tmp_path)
    assert cfg.secrets.hf_token.get_secret_value() == "hf_from_file"
    assert cfg.ntfy_topic == "topic-from-env"
    assert cfg.secrets.anthropic_api_key is None
    assert "hf_from_file" not in repr(cfg) and "hf_from_file" not in str(cfg.secrets)


def test_ntfy_topic_absent(clean_env, tmp_path):
    assert load_config(tmp_path).ntfy_topic is None


def test_redact_known_secret_values(clean_env, monkeypatch):
    monkeypatch.setenv("PATREON_FEED_URL", "https://www.patreon.com/rss/x?auth=SECRETAUTH123")
    msg = "fetching https://www.patreon.com/rss/x?auth=SECRETAUTH123 now"
    out = redact(msg)
    assert "SECRETAUTH123" not in out and "***" in out


def test_redact_literals_and_url_params(clean_env):
    assert redact("key sk-ant-abcdef", ["sk-ant-abcdef"]) == "key ***"
    out = redact("GET https://h/feed?token=abc123&x=1 and api_key=zzz999")
    assert "abc123" not in out and "zzz999" not in out
    assert "x=1" in out and out.count("***") == 2


def test_redact_leaves_plain_text_alone(clean_env):
    assert redact("nothing secret here") == "nothing secret here"
    assert redact(42) == "42"


def test_llm_paths_resolve_against_the_root_and_defaults(tmp_path, monkeypatch):
    """Relative llm.prompt_file / compare_dir must follow the root, never the cwd (a test once
    read the real prompt file and would have written into the real data/compare)."""
    from conftest import write_config

    from ciw.config import load_config

    monkeypatch.setenv("CIW_ROOT", str(tmp_path))
    write_config(tmp_path)
    cfg = load_config(tmp_path, use_dotenv=False)
    assert cfg.llm.prompt_file == tmp_path / "prompts" / "compare.yaml"
    assert cfg.llm.compare_dir == tmp_path / "data" / "compare"
    assert (cfg.llm.concurrency, cfg.llm.max_usd_per_run, cfg.llm.monthly_budget_usd) == (3, 10, 25)
