"""The backfill runner: planning, estimates, failure isolation, stop/resume, prefetch, logs, CLI.

Everything runs against tmp dirs with the fake ASR backend and a fake downloader (plus one
test through the real downloader on an in-memory audio host). No GPU, no network, no real data.
"""

import json
import os
import signal
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from asrfake import FakeTranscriber
from conftest import write_config
from typer.testing import CliRunner

from ciw import backfill as bf
from ciw import download as dl
from ciw import search_index as si
from ciw import state
from ciw import transcribe as tx
from ciw.asr import ASR_BACKENDS, register_backend
from ciw.cli import app
from ciw.config import Secrets, load_config
from ciw.ingest import meta_path
from ciw.models import EpisodeMeta, Word

runner = CliRunner()
IDS = [f"{c}" * 24 for c in "abcdefgh"]  # a..h
A, B, C, D, E, F = IDS[:6]


# ------------------------------------------------------------------------- helpers


class HookTranscriber(FakeTranscriber):
    """FakeTranscriber with a per-call hook (to create a STOP file, send a signal, ...)."""

    hook = None

    def transcribe(self, audio_path, **kw):
        if self.hook:
            self.hook(Path(audio_path).stem, len(self.calls) + 1)
        return super().transcribe(audio_path, **kw)


class FakeDownloads:
    """A controllable replacement for ``download_episode``: writes a tiny file and the state."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.calls: list[str] = []
        self.fail: set[str] = set()  # failed result (like a 404)
        self.boom: set[str] = set()  # raises an unexpected exception
        self.fatal: set[str] = set()  # disk guard
        self.duration = 3600.0
        self.on_call = None
        self.lock = threading.Lock()

    def __call__(self, cfg, eid, client):
        with self.lock:
            self.calls.append(eid)
        if self.on_call:
            self.on_call(eid)
        if eid in self.boom:
            raise RuntimeError("socket exploded " + (cfg.secrets.secret_values() or [""])[0])
        if eid in self.fatal:
            return dl.DownloadResult(
                eid, "failed", "only 1.0 GiB free on the audio disk", fatal=True
            )
        if eid in self.fail:
            with state.connect(cfg.state_db) as conn:
                state.set_error(conn, eid, "HTTP 404")
            return dl.DownloadResult(eid, "failed", "HTTP 404")
        cfg.audio_dir.mkdir(parents=True, exist_ok=True)
        path = cfg.audio_dir / f"{eid}.mp3"
        path.write_bytes(b"x" * 1000)
        with state.connect(cfg.state_db) as conn:
            state.mark_downloaded(
                conn, eid, audio_path=str(path), audio_bytes=1000,
                audio_duration_s=self.duration, audio_sha256="0" * 64,
                audio_downloaded_from="acast",
            )  # fmt: skip
        return dl.DownloadResult(
            eid, "downloaded", "", path, 1000, 1000, self.duration, elapsed_s=0.01
        )


class NoClient:
    def close(self):
        pass


@pytest.fixture
def env(tmp_cfg, tmp_path, monkeypatch):
    write_config(
        tmp_path,
        "asr:\n  backend: fake\n  model: fake-model\ndownload:\n  min_free_gb: 0\n",
    )
    cfg = load_config(tmp_path, use_dotenv=False)
    assert str(cfg.audio_dir).startswith(str(tmp_path))
    assert str(cfg.backfill.log_dir).startswith(str(tmp_path))
    tr = HookTranscriber(cfg)
    register_backend("fake", lambda c, **kw: tr)
    downloads = FakeDownloads(cfg)
    monkeypatch.setattr(dl, "make_client", lambda c: NoClient())
    # the CLI builds its own Backfill: route its downloads to the fake too
    monkeypatch.setattr(
        bf.Backfill, "_default_download", lambda self, c, eid, client: downloads(c, eid, client)
    )
    yield SimpleNamespace(cfg=cfg, tr=tr, downloads=downloads, notes=[], tmp=tmp_path)
    ASR_BACKENDS.pop("fake", None)


def add(cfg, eid, *, day=1, month=1, year=2026, source="acast", duration=3600.0, title=None):
    meta = EpisodeMeta(
        guid=eid,
        guids={source: eid},
        source=source,
        audio_source=source,
        title=title or f"Episode {eid[:2]}",
        pub_date=datetime(year, month, day, 8, tzinfo=UTC),
        duration_s=duration,
        enclosure_url="https://audio.example/x.mp3",
    )
    meta.save(meta_path(cfg, eid))
    with state.connect(cfg.state_db) as conn:
        state.upsert_episode(conn, meta)


def row(cfg, eid):
    with state.connect(cfg.state_db) as conn:
        return dict(state.get_episode(conn, eid))


def finish(env, eid, *, index=True):
    """Make an episode fully done (download + transcript + index) the way the runner would."""
    env.downloads(env.cfg, eid, None)
    with state.connect(env.cfg.state_db) as conn:
        res = tx.transcribe_episode(env.cfg, conn, state.get_episode(conn, eid), env.tr)
        assert res.outcome == "transcribed"
    if index:
        si.update_index(env.cfg, episode_ids=[eid])
    env.tr.calls.clear()
    env.downloads.calls.clear()


def make_plan(env, **sel):
    sel.setdefault("sources", ("acast", "hrn"))
    with state.connect(env.cfg.state_db) as conn:
        return bf.plan_backfill(env.cfg, conn, bf.Selection(**sel), env.tr)


def ids(plan):
    return [i.episode_id for i in plan.items]


def run(env, plan=None, *, signals=False, **kw):
    plan = plan or make_plan(env)
    lines: list[str] = []
    kw.setdefault(
        "notify", lambda cfg, title, body, **k: env.notes.append((title, body, k)) or True
    )
    summary, runner_ = bf.run_backfill(
        env.cfg, plan, transcriber=env.tr, download_fn=env.downloads, echo=lines.append,
        install_signals=signals, **kw,
    )  # fmt: skip
    return summary, runner_, lines


def log_events(cfg):
    files = sorted(cfg.backfill.log_dir.glob("backfill-*.jsonl"))
    assert len(files) == 1
    return list(bf.iter_lines(files[0]))


def seed(env):
    """acast A (newest) .. D (oldest), hrn E (newer than all acast) .. F."""
    add(env.cfg, A, month=4)
    add(env.cfg, B, month=3)
    add(env.cfg, C, month=2)
    add(env.cfg, D, month=1)
    add(env.cfg, E, month=6, year=2026, source="hrn")
    add(env.cfg, F, month=5, year=2015, source="hrn")


# ============================================================================= planning


def test_plan_orders_acast_then_hrn_newest_first(env):
    seed(env)
    plan = make_plan(env)
    assert ids(plan) == [A, B, C, D, E, F]  # acast newest->oldest, then hrn newest->oldest
    assert [i.need for i in plan.items] == ["download+transcribe"] * 6
    assert ids(make_plan(env, oldest_first=True)) == [D, C, B, A, F, E]
    assert ids(make_plan(env, sources=("hrn",))) == [E, F]
    assert ids(make_plan(env, sources=("hrn", "acast"))) == [E, F, A, B, C, D]


def test_plan_date_filters_and_limit(env):
    seed(env)
    assert ids(make_plan(env, since=datetime(2026, 2, 1).date())) == [A, B, C, E]
    assert ids(make_plan(env, until=datetime(2026, 2, 28).date())) == [C, D, F]
    both = make_plan(env, since=datetime(2026, 2, 1).date(), until=datetime(2026, 3, 1).date())
    assert ids(both) == [B, C]
    assert ids(make_plan(env, limit=3)) == [A, B, C]
    assert ids(make_plan(env, limit=3, sources=("hrn",))) == [E, F]


def test_plan_skips_done_episodes_and_limit_counts_only_work(env):
    seed(env)
    finish(env, A)
    finish(env, B)
    plan = make_plan(env, limit=2, sources=("acast",))
    assert ids(plan) == [C, D]  # the limit applies after the done ones are skipped
    assert plan.skipped == {"done": 2}
    assert plan.index_only == []


def test_done_but_unindexed_episode_is_only_reindexed(env):
    seed(env)
    finish(env, A, index=False)
    plan = make_plan(env, sources=("acast",))
    assert A not in ids(plan) and plan.index_only == [A]
    summary, _, lines = run(env, plan)
    assert summary.done == 3 and env.tr.calls and A not in {p.stem for p in env.tr.calls}
    assert A in bf.indexed_ids(env.cfg)


def test_downloaded_episode_needs_only_transcription(env):
    seed(env)
    env.downloads(env.cfg, B, None)
    plan = make_plan(env, sources=("acast",))
    assert {i.episode_id: i.need for i in plan.items}[B] == "transcribe"
    assert plan.count("transcribe") == 1 and plan.count("download+transcribe") == 3


def test_stale_transcript_is_skipped_unless_asked(env):
    seed(env)
    finish(env, A)
    env.tr.version_string = "fake 2.0"
    plan = make_plan(env, sources=("acast",))
    assert A not in ids(plan) and plan.skipped["stale"] == 1
    assert A in ids(make_plan(env, sources=("acast",), stale=True))


def test_cooldown_and_exhausted_attempts_are_parked_until_retry_failed(env):
    seed(env)
    future = (datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    past = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with state.connect(env.cfg.state_db) as conn:
        state.record_failure(conn, A, stage="download", error="x", next_retry_at=future)
        state.record_failure(conn, B, stage="download", error="x", next_retry_at=past)
        for _ in range(3):
            state.record_failure(conn, C, stage="transcribe", error="x", next_retry_at=past)
    plan = make_plan(env, sources=("acast",))
    assert ids(plan) == [B, D]  # A cooling down, C out of attempts (3 of 3)
    assert plan.skipped == {"cooldown": 1, "out-of-attempts": 1}
    forced = make_plan(env, sources=("acast",), retry_failed=True)
    assert ids(forced) == [A, B, C, D]


def test_modes_download_only_and_transcribe_only(env):
    seed(env)
    env.downloads(env.cfg, B, None)
    d = make_plan(env, sources=("acast",), mode="download-only")
    assert ids(d) == [A, C, D] and {i.need for i in d.items} == {"download"}
    assert d.skipped == {"has-audio": 1}
    t = make_plan(env, sources=("acast",), mode="transcribe-only")
    assert ids(t) == [B] and t.skipped == {"no-audio": 3}


def test_plan_has_no_side_effects(env):
    seed(env)
    before = row(env.cfg, A)
    make_plan(env)
    assert row(env.cfg, A) == before
    assert not env.cfg.audio_dir.exists() and not env.cfg.backfill.log_dir.exists()
    assert not env.cfg.backfill.stop_file.exists()


# ============================================================================= estimates


def test_estimates_use_defaults_when_nothing_was_measured(env):
    seed(env)
    plan = make_plan(env, sources=("acast",))
    assert plan.speed_x == bf.DEFAULT_SPEED_X and "default" in plan.speed_basis
    assert plan.bytes_per_s["*"] == bf.DEFAULT_BYTES_PER_S and "default" in plan.bytes_basis
    assert plan.audio_s == pytest.approx(4 * 3600)  # feed length, ratio 1.0
    assert plan.download_bytes == int(4 * 3600 * bf.DEFAULT_BYTES_PER_S)
    assert plan.gpu_s == pytest.approx(4 * 3600 / bf.DEFAULT_SPEED_X)
    text = bf.format_plan(plan)
    assert "ESTIMATE" in text and "default" in text and "GPU: 0.1 h" in text


def test_estimates_use_measured_speed_bytes_and_known_sizes(env):
    seed(env)
    env.downloads.duration = 3700.0  # real audio is 2.8% longer than the feed's 3600
    finish(env, A)
    env.downloads(env.cfg, B, None)  # B already on disk: 0 bytes to fetch
    with state.connect(env.cfg.state_db) as conn:  # pretend the ASR took 100 s: 37x
        conn.execute("UPDATE episodes SET audio_bytes = 59200 WHERE audio_bytes > 0")
        conn.execute("UPDATE episodes SET asr_runtime_s = 100 WHERE episode_id=?", (A,))
    plan = make_plan(env, sources=("acast",))
    assert plan.speed_x == pytest.approx(37.0) and "measured" in plan.speed_basis
    bps = 59200 / 3700
    assert plan.bytes_per_s["acast"] == pytest.approx(bps) and "measured" in plan.bytes_basis
    by_id = {i.episode_id: i for i in plan.items}
    assert by_id[B].est_bytes == 0 and by_id[B].audio_known and by_id[B].audio_s == 3700
    # C and D: feed 3600 s x the observed real/feed ratio (3700/3600) x bytes per second
    assert by_id[C].est_bytes == int(3600 * (3700 / 3600) * bps)
    assert plan.download_bytes == by_id[C].est_bytes + by_id[D].est_bytes
    assert plan.audio_s == pytest.approx(3700 * 3)


def test_disk_check_reports_shortfall(env, monkeypatch):
    seed(env)
    monkeypatch.setattr(bf, "_free_bytes", lambda path: 10_000)
    plan = make_plan(env)
    assert plan.disk.audio_ok is False and plan.disk.ok is False
    assert "NOT ENOUGH" in bf.format_plan(plan)


# ================================================================= running: the happy path


def test_run_processes_everything_in_order_and_a_second_run_is_a_noop(env):
    seed(env)
    summary, _, lines = run(env)
    assert (summary.done, summary.failed, summary.exit_code) == (6, 0, 0)
    assert [p.stem for p in env.tr.calls] == [A, B, C, D, E, F]
    assert env.downloads.calls == [A, B, C, D, E, F]
    for eid in IDS[:6]:
        r = row(env.cfg, eid)
        assert r["stage"] == "transcribed" and r["attempts"] == 0 and r["error"] is None
        assert tx.transcript_path(env.cfg, eid).is_file()
    assert set(IDS[:6]) <= bf.indexed_ids(env.cfg)  # indexed as it goes
    assert any(
        "[1/6]" in ln and "Episode aa" in ln and "ETA" in ln and "GPU left" in ln for ln in lines
    )
    env.tr.calls.clear()
    env.downloads.calls.clear()
    again = make_plan(env)
    assert again.items == [] and again.skipped == {"done": 6}
    summary2, _, _ = run(env, again)
    assert summary2.done == 0 and env.tr.calls == [] and env.downloads.calls == []


def test_summary_report_and_throughput(env):
    seed(env)
    summary, runner_, _ = run(env, make_plan(env, limit=2))
    report = runner_.final_report()
    assert "done 2" in report and "failed 0" in report and "suspect 0" in report
    assert summary.audio_s == pytest.approx(3.0) and summary.speed_x > 0
    assert summary.wall_s >= 0


# ========================================================================= failure isolation


def test_failures_are_isolated_recorded_and_the_run_continues(env):
    seed(env)
    env.downloads.fail = {B}
    env.downloads.boom = {C}
    env.tr.fail_on = {D}
    summary, _, lines = run(env)
    assert summary.done == 3 and summary.failed == 3 and summary.exit_code == 1
    assert [p.stem for p in env.tr.calls] == [A, D, E, F]  # B and C never reached the GPU
    rb, rc, rd = (row(env.cfg, x) for x in (B, C, D))
    assert (rb["attempts"], rb["last_error_stage"], rb["stage"]) == (1, "download", "discovered")
    assert "HTTP 404" in rb["error"]
    assert (rc["attempts"], rc["last_error_stage"]) == (1, "download")
    assert "socket exploded" in rc["error"]
    assert (rd["attempts"], rd["last_error_stage"], rd["stage"]) == (1, "transcribe", "downloaded")
    assert "boom on" in rd["error"]
    now = datetime.now(UTC)
    for r in (rb, rc, rd):  # first failure: cool-down 10 minutes
        wait = datetime.fromisoformat(r["next_retry_at"].replace("Z", "+00:00")) - now
        assert timedelta(minutes=9) < wait < timedelta(minutes=11)
    assert any("download-FAIL" in ln for ln in lines) and any(
        "transcribe-FAIL" in ln for ln in lines
    )
    assert {f[0] for f in summary.failures} == {B, C, D}
    # all three are cooling down: a re-run is a no-op for them
    env.tr.calls.clear()
    env.downloads.calls.clear()
    plan = make_plan(env)
    assert plan.items == [] and plan.skipped == {"done": 3, "cooldown": 3}
    # once the cool-down is over they are tried again; B now works, D fails again
    env.downloads.fail = set()
    past = (datetime.now(UTC) - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with state.connect(env.cfg.state_db) as conn:
        conn.execute("UPDATE episodes SET next_retry_at = ? WHERE attempts > 0", (past,))
    env.downloads.boom = set()
    summary2, _, _ = run(env)
    assert summary2.done == 2 and summary2.failed == 1  # B, C done; D fails again
    rd2 = row(env.cfg, D)
    assert rd2["attempts"] == 2
    wait = datetime.fromisoformat(rd2["next_retry_at"].replace("Z", "+00:00")) - datetime.now(UTC)
    assert timedelta(minutes=55) < wait < timedelta(minutes=65)  # second failure: 60 minutes
    assert row(env.cfg, B)["attempts"] == 0 and row(env.cfg, B)["next_retry_at"] is None


def test_max_attempts_parks_an_episode_and_retry_failed_overrides(env):
    add(env.cfg, A)
    env.tr.fail_on = {A}
    past = (datetime.now(UTC) - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for _ in range(3):
        summary, _, lines = run(env)
        assert summary.failed == 1
        with state.connect(env.cfg.state_db) as conn:
            conn.execute("UPDATE episodes SET next_retry_at = ?", (past,))
    assert row(env.cfg, A)["attempts"] == 3
    assert any("parked" in ln for ln in lines)
    parked = make_plan(env)
    assert parked.items == [] and parked.skipped == {"out-of-attempts": 1}
    forced = make_plan(env, retry_failed=True)
    assert ids(forced) == [A]
    env.tr.fail_on = set()
    summary, _, _ = run(env, forced)
    assert summary.done == 1 and row(env.cfg, A)["attempts"] == 0


def test_backoff_uses_the_last_value_for_later_failures(env):
    add(env.cfg, A)
    env.tr.fail_on = {A}
    for _ in range(4):
        run(env, make_plan(env, retry_failed=True))
    r = row(env.cfg, A)
    assert r["attempts"] == 4
    wait = datetime.fromisoformat(r["next_retry_at"].replace("Z", "+00:00")) - datetime.now(UTC)
    assert timedelta(minutes=355) < wait < timedelta(minutes=365)  # 360, the last step


def test_errors_never_leak_secrets_to_state_log_or_output(env):
    secret = "https://patreon.example/feed?auth=TOPSECRET123"
    cfg = env.cfg.model_copy(update={"secrets": Secrets(patreon_feed_url=secret)})
    env.downloads.cfg = cfg
    add(cfg, A)
    env.downloads.boom = {A}
    plan = make_plan(SimpleNamespace(cfg=cfg, tr=env.tr))
    summary, _, lines = run(SimpleNamespace(**{**env.__dict__, "cfg": cfg}), plan)
    dump = json.dumps(row(cfg, A), default=str) + "\n".join(lines)
    dump += next(cfg.backfill.log_dir.glob("*.jsonl")).read_text()
    assert summary.failed == 1 and "socket exploded" in dump
    assert "TOPSECRET123" not in dump


def test_transcribe_failure_of_a_missing_audio_file_is_a_failure(env):
    add(env.cfg, A)
    env.downloads(env.cfg, A, None)
    (env.cfg.audio_dir / f"{A}.mp3").unlink()  # state says downloaded, the file is gone
    plan = make_plan(env)
    assert plan.items[0].need == "download+transcribe"  # so it is simply fetched again
    summary, _, _ = run(env, plan)
    assert summary.done == 1


# ============================================================================ stop and resume


def test_stop_file_finishes_the_current_episode_and_a_rerun_continues(env):
    seed(env)
    env.tr.hook = lambda stem, n: n == 2 and env.cfg.backfill.stop_file.write_text("stop")
    summary, _, lines = run(env)
    assert summary.done == 2 and summary.stopped.startswith("stop file")
    assert summary.exit_code == 0
    assert [p.stem for p in env.tr.calls] == [A, B]  # B finished although STOP appeared during it
    assert not env.cfg.backfill.stop_file.exists()  # honoured, so removed
    assert row(env.cfg, B)["stage"] == "transcribed" and row(env.cfg, C)["stage"] != "transcribed"
    # re-run: only the rest, nothing redone
    env.tr.hook = None
    env.tr.calls.clear()
    plan = make_plan(env)
    assert ids(plan)[0] in (C, D) and A not in ids(plan) and B not in ids(plan)
    summary2, _, _ = run(env, plan)
    assert summary2.done + summary.done == 6
    assert sorted(p.stem for p in env.tr.calls) == sorted([C, D, E, F])  # no repeats of A, B


def test_prefetched_but_untranscribed_audio_is_reused_by_the_next_run(env):
    seed(env)
    env.tr.hook = lambda stem, n: n == 1 and env.cfg.backfill.stop_file.write_text("stop")
    summary, _, _ = run(env)
    assert summary.done == 1
    prefetched = [i for i in IDS[:6] if row(env.cfg, i)["stage"] == "downloaded"]
    assert prefetched  # the downloader was ahead
    env.downloads.calls.clear()
    plan = make_plan(env)
    needs = {i.episode_id: i.need for i in plan.items}
    assert all(needs[p] == "transcribe" for p in prefetched)
    run(env, plan)
    assert not set(prefetched) & set(env.downloads.calls)  # no re-download


def test_sigint_stops_after_the_current_episode_and_restores_handlers(env):
    seed(env)
    before = signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM)
    env.tr.hook = lambda stem, n: n == 2 and os.kill(os.getpid(), signal.SIGINT)
    summary, _, lines = run(env, signals=True)
    assert summary.done == 2 and summary.stopped == "signal SIGINT"
    assert [p.stem for p in env.tr.calls] == [A, B]
    assert (signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM)) == before
    assert any("finishing the current episode" in ln for ln in lines)
    events = [e["event"] for e in log_events(env.cfg)]
    assert events.index("signal") < events.index("stop") < events.index("run-end")


def test_sigterm_behaves_like_sigint(env):
    seed(env)
    env.tr.hook = lambda stem, n: n == 1 and os.kill(os.getpid(), signal.SIGTERM)
    summary, _, _ = run(env, signals=True)
    assert summary.done == 1 and summary.stopped == "signal SIGTERM"


def test_second_signal_aborts_immediately(env):
    seed(env)
    aborted = []
    plan = make_plan(env)
    runner_ = bf.Backfill(
        env.cfg, plan, transcriber=env.tr, download_fn=env.downloads, echo=lambda s: None,
        install_signals=False, abort_fn=aborted.append,
    )  # fmt: skip
    runner_.handle_signal(signal.SIGINT)
    assert aborted == [] and runner_.stop_reason() == "signal SIGINT"
    runner_.handle_signal(signal.SIGINT)
    assert aborted == [130]
    assert "abort" in [e["event"] for e in log_events(env.cfg)]


def test_max_hours_stops_starting_new_work(env):
    seed(env)
    now = [1000.0]

    def tick(stem, n):
        now[0] += 1800  # each episode "takes" half an hour

    env.tr.hook = tick
    summary, _, _ = run(env, max_hours=1.0, clock=lambda: now[0], started_at=1000.0)
    assert summary.done == 2 and "max-hours" in summary.stopped


def test_stale_stop_file_is_removed_with_a_warning_but_a_fresh_one_is_honoured(env):
    seed(env)
    stop = env.cfg.backfill.stop_file
    stop.write_text("old")
    old = time.time() - 3600
    os.utime(stop, (old, old))
    summary, _, lines = run(env, make_plan(env, limit=2))
    assert summary.done == 2 and summary.stopped is None
    assert any("stale stop file" in ln for ln in lines) and not stop.exists()
    # a stop file written after the run started stops it at the next decision point
    env.tr.hook = lambda stem, n: stop.write_text("now")
    summary, _, _ = run(env)
    assert summary.done == 1 and summary.stopped.startswith("stop file")


def test_disk_guard_failure_stops_cleanly_with_a_nonzero_exit(env):
    seed(env)
    env.downloads.fatal = {C}
    summary, _, lines = run(env)
    assert summary.fatal and "1.0 GiB free" in summary.fatal and summary.exit_code == bf.EXIT_DISK
    assert env.downloads.calls == [A, B, C]  # nothing after the disk guard tripped
    assert summary.done == 2  # what was already downloaded was still transcribed
    assert row(env.cfg, C)["attempts"] == 0  # the disk is not the episode's fault
    assert any("FATAL" in ln for ln in lines)
    assert "fatal" in [e["event"] for e in log_events(env.cfg)]
    assert any("disk" in n[0] for n in env.notes)


# ======================================================================= prefetch pipeline


def test_downloads_run_ahead_but_never_more_than_prefetch(env):
    for n, eid in enumerate(IDS[:7]):
        add(env.cfg, eid, day=n + 1)
    prefetch = env.cfg.backfill.prefetch
    assert prefetch == 2
    started = []
    reached = threading.Event()
    env.downloads.on_call = lambda eid: started.append(eid)
    seen = {}

    def first_transcription(stem, n):
        if n != 1:
            return
        # while the GPU works on the first episode the downloader gets `prefetch` ahead ...
        reached.wait(timeout=0)
        deadline = time.time() + 5
        while len(started) < 1 + prefetch and time.time() < deadline:
            time.sleep(0.01)
        time.sleep(0.4)  # ... and stays there
        seen["started"] = len(started)

    env.tr.hook = first_transcription
    summary, runner_, _ = run(env)
    assert seen["started"] == 1 + prefetch  # one being transcribed + prefetch downloaded
    assert summary.max_ahead <= prefetch
    assert summary.done == 7 and env.downloads.calls == list(reversed(IDS[:7]))


def test_pipeline_order_is_plan_order_and_slow_downloads_do_not_reorder(env):
    for n, eid in enumerate(IDS[:5]):
        add(env.cfg, eid, day=n + 1)  # newest = highest day = IDS[4]
    env.downloads.on_call = lambda eid: time.sleep(0.05)
    run(env)
    expected = list(reversed(IDS[:5]))
    assert [p.stem for p in env.tr.calls] == expected


# ================================================================================== locks


def test_lock_held_by_another_process_gives_a_clear_message(env):
    seed(env)
    with tx.transcribe_lock(env.cfg):
        with pytest.raises(bf.BackfillLocked) as exc:
            run(env)
    assert "transcribe lock is held" in str(exc.value) and "ciw backfill run" in str(exc.value)
    assert env.downloads.calls == []
    with dl.download_lock(env.cfg):
        with pytest.raises(bf.BackfillLocked) as exc:
            run(env)
        assert "download lock is held" in str(exc.value)
        # transcribe-only needs no download lock
        env.downloads(env.cfg, A, None)
        summary, _, _ = run(env, make_plan(env, mode="transcribe-only"))
        assert summary.done == 1
    # both are free again afterwards
    assert run(env, make_plan(env, limit=1))[0].done == 1


def test_cli_lock_contention_exits_3_with_the_message(env):
    seed(env)
    with dl.download_lock(env.cfg):
        res = runner.invoke(app, ["backfill", "run", "--source", "acast", "--limit", "2"])
    assert res.exit_code == 3
    assert "download lock is held" in res.output
    assert env.tr.calls == []


# ============================================================================== suspect flag


def test_suspect_episode_is_flagged_in_state_log_and_report(env):
    add(env.cfg, A)
    add(env.cfg, B, month=2)
    env.downloads.duration = 100.0
    gaps = [(0, 10), (22, 40), (51, 60)]  # 23 s of gaps in 100 s of audio
    env.tr.words = [Word(w=f"w{i}", start=a, end=b) for i, (a, b) in enumerate(gaps)]
    summary, runner_, lines = run(env)
    assert summary.suspects == [B, A]
    r = row(env.cfg, A)
    assert (r["suspect"], r["asr_gap_count"], r["asr_gap_s"]) == (1, 2, 23.0)
    done = [e for e in log_events(env.cfg) if e["event"] == "transcribe-done"]
    assert all(e["suspect"] is True and e["gap_count"] == 2 for e in done)
    assert any("SUSPECT" in ln for ln in lines)
    assert f"{B}, {A}" in runner_.final_report()
    # a normal episode is not flagged
    env.tr.words = [Word(w="a", start=0, end=1), Word(w="b", start=2, end=3)]
    add(env.cfg, C, month=3)
    run(env, make_plan(env))
    assert row(env.cfg, C)["suspect"] == 0


# ============================================================================== event log


def test_jsonl_log_has_one_parseable_event_per_line(env):
    seed(env)
    env.downloads.fail = {B}
    summary, _, _ = run(env, make_plan(env, limit=3))
    path = next(env.cfg.backfill.log_dir.glob("backfill-*.jsonl"))
    assert path.name == f"backfill-{datetime.now(UTC):%Y%m%d}.jsonl"
    events = list(bf.iter_lines(path))
    kinds = [e["event"] for e in events]
    assert kinds[0] == "run-start" and "run-end" in kinds and kinds[-1] == "ntfy"
    start = events[0]
    assert start["episodes"] == 3 and start["pid"] == os.getpid() and start["prefetch"] == 2
    td = [e for e in events if e["event"] == "transcribe-done"]
    assert [e["episode_id"] for e in td] == [A, C]
    assert {
        "elapsed_s",
        "audio_s",
        "x_realtime",
        "words",
        "gap_s",
        "gap_count",
        "title",
        "n",
        "total",
    } <= set(td[0])
    fail = next(e for e in events if e["event"] == "download-failed")
    assert fail["episode_id"] == B and fail["attempts"] == 1 and fail["next_retry_at"]
    end = next(e for e in events if e["event"] == "run-end")
    assert (end["done"], end["failed"], end["exit_code"]) == (2, 1, 1)
    assert all(e["ts"].endswith("Z") for e in events)


def test_event_log_rotates_daily_files_and_keeps_only_the_newest(tmp_path):
    d = tmp_path / "logs"
    d.mkdir()
    for day in ("20200101", "20200102", "20200103", "20200104"):
        (d / f"backfill-{day}.jsonl").write_text("{}\n")
    (d / "other.txt").write_text("keep")
    log = bf.EventLog(d, keep=3)
    log.write("hello")
    names = sorted(p.name for p in d.iterdir())
    assert len(names) == 4 and "other.txt" in names  # 3 jsonl kept (incl. today's) + other.txt
    assert "backfill-20200101.jsonl" not in names and "backfill-20200102.jsonl" not in names
    assert any(
        n.startswith("backfill-2") and n.endswith(f"{datetime.now(UTC):%Y%m%d}.jsonl")
        for n in names
    )


def test_event_log_caps_a_days_file_size(tmp_path):
    log = bf.EventLog(tmp_path / "logs", keep=3, max_bytes=500)
    for i in range(50):
        log.write("tick", i=i, pad="x" * 40)
    (path,) = (tmp_path / "logs").glob("*.jsonl")
    lines = path.read_text().splitlines()
    assert path.stat().st_size < 500 + 200
    assert json.loads(lines[-1])["event"] == "log-capped" and len(lines) < 50
    assert sum('"log-capped"' in ln for ln in lines) == 1


def test_event_log_never_raises_on_an_unwritable_directory(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    log = bf.EventLog(blocker / "logs", keep=3)  # a path below a regular file
    log.write("hello")  # no exception


# ================================================================================== ntfy


def test_one_summary_notification_and_one_alert_after_three_consecutive_failures(env):
    for n, eid in enumerate(IDS[:6]):
        add(env.cfg, eid, day=n + 1)
    env.tr.fail_on = set(IDS[:6])
    summary, _, _ = run(env)
    assert summary.failed == 6
    titles = [n[0] for n in env.notes]
    assert titles.count("Backfill: repeated failures") == 1  # not once per failure
    assert sum(t.startswith("Backfill finished") for t in titles) == 1
    assert env.notes[-1][2]["priority"] == "high"
    for _, body, _ in env.notes:
        assert "topic" not in body.lower()


def test_no_alert_when_failures_are_not_consecutive(env):
    for n, eid in enumerate(IDS[:6]):
        add(env.cfg, eid, day=n + 1)
    env.tr.fail_on = {IDS[5], IDS[3], IDS[1]}  # every other episode fails
    summary, _, _ = run(env)
    assert summary.failed == 3 and summary.done == 3
    assert [n[0] for n in env.notes] == ["Backfill finished: 3 done, 3 failed"]


def test_without_a_topic_the_real_notifier_only_logs(env, caplog):
    seed(env)
    kwargs = dict(
        transcriber=env.tr, download_fn=env.downloads, echo=lambda s: None, install_signals=False
    )
    runner_ = bf.Backfill(env.cfg, make_plan(env, limit=1), **kwargs)
    runner_.run()
    runner_.notify_summary()  # send_ntfy with no NTFY_TOPIC: logs, returns False, never raises
    sent = [e for e in log_events(env.cfg) if e["event"] == "ntfy"]
    assert sent and sent[0]["sent"] is False


# ====================================================================== real downloader path


def test_end_to_end_with_the_real_downloader_and_fake_asr(env, tmp_path, monkeypatch):
    audiofake = pytest.importorskip("audiofake")
    if audiofake.ffmpeg_exe() is None:
        pytest.skip("ffmpeg is not installed")
    mp3 = audiofake.make_mp3(tmp_path / "tone.mp3", seconds=3)
    web = audiofake.AudioWeb(mp3)
    monkeypatch.undo()  # drop the fake downloader patches ...
    monkeypatch.setattr(dl, "make_client", lambda c: web.client(c.secrets.secret_values()))
    cfg = env.cfg
    for n, eid in enumerate((A, B)):
        meta = EpisodeMeta(
            guid=eid, guids={"acast": eid}, source="acast", audio_source="acast", title=f"E{n}",
            pub_date=datetime(2026, 1, 1 + n, 8, tzinfo=UTC), duration_s=3.0,
            enclosure_url=audiofake.START_URL.format(id=eid), enclosure_type="audio/mpeg",
        )  # fmt: skip
        meta.save(meta_path(cfg, eid))
        with state.connect(cfg.state_db) as conn:
            state.upsert_episode(conn, meta)
    plan = make_plan(env)
    lines: list[str] = []
    summary = bf.Backfill(
        cfg,
        plan,
        transcriber=env.tr,
        echo=lines.append,
        install_signals=False,
        notify=lambda *a, **k: True,
    ).run()
    assert (summary.done, summary.downloaded, summary.failed) == (2, 2, 0)
    for eid in (A, B):
        assert (cfg.audio_dir / f"{eid}.mp3").read_bytes() == mp3
        assert row(cfg, eid)["stage"] == "transcribed"
    assert not list(cfg.audio_dir.glob("*.part*"))


def test_download_episode_does_not_hold_a_stale_snapshot_during_the_transfer(
    env, tmp_path, monkeypatch
):
    """Another connection commits while a download is in flight (the transcriber does exactly
    that); the downloader's own write at the end must not fail with a stale snapshot."""
    audiofake = pytest.importorskip("audiofake")
    if audiofake.ffmpeg_exe() is None:
        pytest.skip("ffmpeg is not installed")
    mp3 = audiofake.make_mp3(tmp_path / "tone.mp3", seconds=3)
    web = audiofake.AudioWeb(mp3)
    cfg = env.cfg
    meta = EpisodeMeta(
        guid=A, guids={"acast": A}, source="acast", audio_source="acast", title="E",
        pub_date=datetime(2026, 1, 1, 8, tzinfo=UTC), duration_s=3.0,
        enclosure_url=audiofake.START_URL.format(id=A), enclosure_type="audio/mpeg",
    )  # fmt: skip
    meta.save(meta_path(cfg, A))
    add(cfg, B)
    with state.connect(cfg.state_db) as conn:
        state.upsert_episode(conn, meta)
    real = dl.fetch_to_part

    def fetch_and_commit_elsewhere(*args, **kw):
        with state.connect(cfg.state_db) as other:
            state.set_error(other, B, "unrelated write by another connection")
        return real(*args, **kw)

    monkeypatch.setattr(dl, "fetch_to_part", fetch_and_commit_elsewhere)
    with state.connect(cfg.state_db) as conn:
        with web.client(secrets=[]) as client:
            res = dl.download_episode(cfg, conn, A, client=client)
    assert res.outcome == "downloaded", res.message


# ================================================================================ CLI


def invoke(*args):
    return runner.invoke(app, ["backfill", *args])


def test_cli_plan_prints_the_plan_and_changes_nothing(env):
    seed(env)
    res = invoke("plan", "--first", "3")
    assert res.exit_code == 0, res.output
    assert "episodes needing work: 6 of 6" in res.output and "acast: 4" in res.output
    assert "ESTIMATE" in res.output and "GPU:" in res.output and "disk:" in res.output
    assert "... and 3 more" in res.output
    assert env.downloads.calls == [] and not env.cfg.audio_dir.exists()
    assert not env.cfg.backfill.log_dir.exists()
    res = invoke("plan", "--source", "hrn", "--since", "2026-01-01")
    assert "episodes needing work: 1 of 1" in res.output and "acast" not in res.output.split(
        "sources"
    )[1].split("\n")[0].replace("hrn", "")


def test_cli_rejects_bad_options(env):
    assert invoke("plan", "--source", "spotify").exit_code == 2
    assert invoke("plan", "--since", "yesterday").exit_code == 2
    assert invoke("plan", "--download-only", "--transcribe-only").exit_code == 2


def test_cli_run_without_a_selector_refuses_without_yes(env):
    seed(env)
    res = invoke("run")
    assert res.exit_code == 2
    assert "episodes needing work: 6" in res.output  # the plan is printed first
    assert "no selector" in res.output and "--yes" in res.output
    assert env.downloads.calls == [] and env.tr.calls == []
    res = invoke("run", "--yes")
    assert res.exit_code == 0, res.output
    assert len(env.tr.calls) == 6


def test_cli_run_over_ten_episodes_needs_yes(env):
    for n in range(12):
        add(env.cfg, f"{n:02x}" * 12, day=n + 1)
    res = invoke("run", "--source", "acast")
    assert res.exit_code == 2 and "12 episodes are selected" in res.output
    assert env.downloads.calls == []
    assert invoke("run", "--source", "acast", "--limit", "10").exit_code == 0  # <= 10: no --yes
    assert len(env.tr.calls) == 10
    assert invoke("run", "--source", "acast", "--yes").exit_code == 0
    assert len(env.tr.calls) == 12


def test_cli_run_with_a_small_selection_runs_and_prints_the_summary(env):
    seed(env)
    res = invoke("run", "--source", "acast", "--limit", "3")
    assert res.exit_code == 0, res.output
    assert "backfill summary" in res.output and "done 3" in res.output
    assert [p.stem for p in env.tr.calls] == [A, B, C]
    assert "[1/3]" in res.output and "ETA" in res.output
    again = invoke("run", "--source", "acast", "--limit", "3")  # the planner moves on: D is next
    assert again.exit_code == 0 and [p.stem for p in env.tr.calls][-1] == D
    noop = invoke("run", "--source", "acast", "--limit", "3")
    assert "nothing to do" in noop.output or "done 0" in noop.output
    assert noop.exit_code == 0


def test_cli_dry_run_and_exit_code_on_failures(env):
    seed(env)
    res = invoke("run", "--limit", "2", "--dry-run")
    assert res.exit_code == 0 and "dry run" in res.output and env.downloads.calls == []
    env.tr.fail_on = {A}
    res = invoke("run", "--limit", "2")
    assert res.exit_code == 1 and f"FAILED {A}" in res.output


def test_cli_download_only_and_transcribe_only(env):
    seed(env)
    res = invoke("run", "--source", "acast", "--limit", "2", "--download-only")
    assert res.exit_code == 0, res.output
    assert env.downloads.calls == [A, B] and env.tr.calls == []
    assert row(env.cfg, A)["stage"] == "downloaded"
    res = invoke("run", "--source", "acast", "--transcribe-only")
    assert res.exit_code == 0, res.output
    assert [p.stem for p in env.tr.calls] == [A, B]  # only what has audio
    assert env.downloads.calls == [A, B]  # no further downloads


def test_cli_status_stop_and_retry(env):
    seed(env)
    env.downloads.fail = {B}
    env.tr.words = [Word(w="a", start=0, end=10), Word(w="b", start=40, end=41)]
    env.downloads.duration = 100.0  # a 30 s gap in 100 s: suspect
    assert invoke("run", "--source", "acast", "--limit", "3").exit_code == 1
    res = invoke("status")
    assert res.exit_code == 0, res.output
    out = res.output
    assert "run: not running" in out
    assert "acast" in out and "hrn" in out and "transcribed" in out
    assert "failures (1)" in out and B[:20] in out and "HTTP 404" in out and "attempts 1/3" in out
    assert "suspect episodes (2)" in out and "30 s" in out
    assert "ETA:" in out and "disk:" in out and "audio_dir" in out and "data/" in out
    # stop writes the file; a second stop is harmless
    res = invoke("stop")
    assert res.exit_code == 0 and env.cfg.backfill.stop_file.exists()
    assert "No run is active" in res.output
    assert "STOP file present" in invoke("status").output
    env.cfg.backfill.stop_file.unlink()
    # retry clears the cool-down so the next plan includes B again
    assert B not in ids(make_plan(env))
    res = invoke("retry")
    assert "reset 1 failed" in res.output
    assert B in ids(make_plan(env)) and row(env.cfg, B)["attempts"] == 0
    assert "reset 0" in invoke("retry").output


def test_cli_retry_can_target_one_episode(env):
    seed(env)
    with state.connect(env.cfg.state_db) as conn:
        for eid in (A, B):
            state.record_failure(
                conn, eid, stage="download", error="x", next_retry_at="2999-01-01T00:00:00Z"
            )
    assert "reset 1" in invoke("retry", "--episode", A).output
    assert row(env.cfg, A)["attempts"] == 0 and row(env.cfg, B)["attempts"] == 1


def test_status_shows_an_active_run(env):
    seed(env)
    with tx.transcribe_lock(env.cfg):
        out = invoke("status").output
    assert f"ACTIVE (pid {os.getpid()})" in out
    assert "run: not running" in invoke("status").output


def test_torch_is_not_imported_by_the_backfill_modules():
    import subprocess
    import sys

    code = (
        "import sys, ciw.cli, ciw.backfill, ciw.cli_backfill;"
        "assert 'torch' not in sys.modules and 'whisperx' not in sys.modules"
    )
    env_ = {**os.environ, "PYTEST_CURRENT_TEST": "", "CIW_TESTS_ALLOW_REAL_PATHS": "1"}
    subprocess.run([sys.executable, "-c", code], check=True, env=env_)
