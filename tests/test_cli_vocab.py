"""``ciw vocab correct|corrections|quality``, correction wired into ``ciw transcribe``, and the
vocabulary-staleness policy, all via CliRunner with a tmp config and fake backend."""

import json

import pytest
from asrfake import FakeTranscriber
from conftest import write_config
from test_cli_transcribe import add_episode, invoke, row
from typer.testing import CliRunner

from ciw import state, transcribe
from ciw.asr import ASR_BACKENDS, register_backend
from ciw.cli import app
from ciw.config import load_config
from ciw.models import Transcript, Word

runner = CliRunner()
A, B = "a" * 24, "b" * 24
NOTES = "Searzall demo, Soxhlet extraction, polydextrose."

VOCAB = "Searzall\nSoxhlet\npolydextrose\nkansui\n"
ALIASES = "sockslet => Soxhlet\n"
TEXT = "a Searzall and poly dextrose with a vacuum sockslet and kansui"


def put_transcript(cfg, eid, text=TEXT, **kw):
    ws = [Word(w=t, start=i * 0.4, end=i * 0.4 + 0.3) for i, t in enumerate(text.split())]
    t = Transcript(
        asr_model="fake-model", asr_version="fake 1.0", asr_backend="fake", words=ws,
        runtime_s=30, audio_duration_s=600, **kw,
    )  # fmt: skip
    t.save(transcribe.transcript_path(cfg, eid))
    return t


@pytest.fixture
def env(tmp_cfg, tmp_path):
    """tmp config with a vocabulary, aliases and the fake backend; two transcribed episodes."""
    (tmp_path / "vocab").mkdir()
    (tmp_path / "vocab" / "vocabulary.txt").write_text(VOCAB)
    (tmp_path / "vocab" / "aliases.txt").write_text(ALIASES)
    write_config(
        tmp_path,
        "asr:\n  backend: fake\n  model: fake-model\n  vocabulary_file: vocab/vocabulary.txt\n"
        "  vocabulary_prompt: true\n  aliases_file: vocab/aliases.txt\n",
    )
    cfg = load_config(tmp_path, use_dotenv=False)
    shared = {"fail_on": set(), "version": "fake 1.0", "instances": [], "words": None}

    def factory(c, **kw):
        tr = FakeTranscriber(c, **kw)
        tr.words = shared["words"]
        shared["instances"].append(tr)
        return tr

    register_backend("fake", factory)
    add_episode(cfg, A, (2026, 3, 1))
    add_episode(cfg, B, (2026, 2, 1))
    for eid in (A, B):
        meta_file = cfg.episodes_dir / eid / "meta.json"
        meta = json.loads(meta_file.read_text())
        meta["description"] = NOTES
        meta_file.write_text(json.dumps(meta))
    yield cfg, shared
    ASR_BACKENDS.pop("fake", None)


def vc(*args):
    return runner.invoke(app, ["vocab", *args])


# ----------------------------------------------------------------------- vocab correct


def test_correct_dry_run_lists_changes_and_writes_nothing(env):
    cfg, _ = env
    put_transcript(cfg, A)
    path = transcribe.transcript_path(cfg, A)
    before = path.read_bytes()
    result = vc("correct", A, "--dry-run")
    assert result.exit_code == 0, result.output
    assert "2 corrections (dry run, nothing written)" in result.output
    assert "'poly dextrose' => 'polydextrose'" in result.output
    assert "'sockslet' => 'Soxhlet'" in result.output
    assert path.read_bytes() == before


def test_correct_writes_atomically_updates_state_and_is_idempotent(env):
    cfg, _ = env
    put_transcript(cfg, A)
    assert vc("correct", A).exit_code == 0
    path = transcribe.transcript_path(cfg, A)
    t = Transcript.load(path)
    assert t.text() == "a Searzall and polydextrose with a vacuum Soxhlet and kansui"
    assert len(t.corrections) == 2 and t.correction_hash
    assert not list(path.parent.glob("*.tmp"))
    snapshot = path.read_bytes()
    again = vc("correct", A)
    assert again.exit_code == 0 and "2 corrections" in again.output
    assert path.read_bytes() == snapshot  # byte-identical: idempotent
    with state.connect(cfg.state_db) as conn:
        r = state.get_episode(conn, A)
        assert (r["correction_hash"], r["corrections"]) == (t.correction_hash, 2)


def test_correct_picks_up_a_changed_vocabulary_without_asr(env, tmp_path):
    cfg, _ = env
    put_transcript(cfg, A)
    vc("correct", A)
    (tmp_path / "vocab" / "aliases.txt").write_text("")  # drop the alias
    result = vc("correct", A)
    assert "1 corrections" in result.output
    t = Transcript.load(transcribe.transcript_path(cfg, A))
    assert t.text().endswith("a vacuum sockslet and kansui")  # restored from original_words
    assert len(t.corrections) == 1


def test_correct_revert_restores_the_asr_words(env):
    cfg, _ = env
    original = put_transcript(cfg, A)
    vc("correct", A)
    result = vc("correct", A, "--revert")
    assert result.exit_code == 0 and "reverted 2 corrections" in result.output
    t = Transcript.load(transcribe.transcript_path(cfg, A))
    assert t.words == original.words and t.corrections == [] and t.correction_hash is None


def test_correct_all_and_errors(env):
    cfg, _ = env
    put_transcript(cfg, A)
    put_transcript(cfg, B)
    result = vc("correct", "--all")
    assert result.exit_code == 0 and "4 corrections in 2 transcripts" in result.output
    assert vc("correct").exit_code == 2  # nothing selected
    missing = vc("correct", "c" * 24)
    assert missing.exit_code == 1 and "no transcript" in missing.output
    assert vc("correct", A, B, "--transcript", "x.json").exit_code != 0


def test_correct_without_any_vocabulary_is_an_error_and_changes_nothing(tmp_cfg, tmp_path):
    write_config(tmp_path)
    cfg = load_config(tmp_path, use_dotenv=False)
    add_episode(cfg, A, (2026, 3, 1))
    put_transcript(cfg, A)
    before = transcribe.transcript_path(cfg, A).read_bytes()
    result = vc("correct", A)
    assert result.exit_code == 1 and "no vocabulary" in result.output
    assert transcribe.transcript_path(cfg, A).read_bytes() == before


def test_corrections_command_shows_the_audit_list(env):
    cfg, _ = env
    put_transcript(cfg, A)
    vc("correct", A)
    out = vc("corrections", A)
    assert out.exit_code == 0 and "2 corrections" in out.output
    assert "alias" in out.output and "'sockslet' => 'Soxhlet'" in out.output
    data = json.loads(vc("corrections", A, "--json").output)
    assert [c["rule"] for c in data["corrections"]] == ["merge", "alias"]
    assert data["corrections"][0]["original"] == "poly dextrose"
    assert "original_words" not in data["corrections"][0]
    assert vc("corrections", "c" * 24).exit_code == 1


# ----------------------------------------------------------------------- vocab quality


def test_quality_reports_recall_before_after_and_runtime(env):
    cfg, _ = env
    put_transcript(cfg, A)
    raw = vc("quality", A)
    assert raw.exit_code == 0, raw.output
    assert "term recall: 1/3 = 33% after correction" in raw.output  # only Searzall exact
    assert "RTF 0.050" in raw.output and "20x realtime" in raw.output
    vc("correct", A)
    fixed = vc("quality", A)
    assert (
        "term recall: 3/3 = 100% after correction, 1/3 = 33% before (2 corrections" in fixed.output
    )


def test_quality_json_and_aggregate(env):
    cfg, _ = env
    put_transcript(cfg, A)
    put_transcript(cfg, B)
    vc("correct", A)
    out = vc("quality", A, B, "--json")
    data = json.loads(out.output)
    assert [e["episode_id"] for e in data["episodes"]] == [A, B]
    assert (
        data["episodes"][0]["recall_after"] == 1.0 and data["episodes"][1]["recall_after"] == 1 / 3
    )
    assert data["aggregate"]["terms"] == 6 and data["aggregate"]["found_after"] == 4
    text = vc("quality", A, B, "--aggregate")
    assert "aggregate over 2 episodes: 4/6 terms = 67% after correction" in text.output


def test_quality_errors(env, tmp_path):
    cfg, _ = env
    assert vc("quality").exit_code == 2
    assert vc("quality", A).exit_code == 1  # no transcript yet
    put_transcript(cfg, A)
    (tmp_path / "vocab" / "vocabulary.txt").write_text("")
    empty = vc("quality", A)
    assert empty.exit_code == 1 and "no vocabulary terms" in empty.output


def test_quality_of_a_scratch_transcript_uses_the_episodes_show_notes(env, tmp_path):
    cfg, _ = env
    put_transcript(cfg, A)
    scratch = tmp_path / "turbo.json"
    scratch.write_bytes(transcribe.transcript_path(cfg, A).read_bytes())
    out = vc("quality", A, "--transcript", str(scratch))
    assert out.exit_code == 0 and "term recall: 1/3" in out.output


# ------------------------------------------------------ correction wired into transcribe


def test_transcribe_runs_the_correction_pass_and_records_it(env):
    cfg, shared = env
    shared["words"] = [
        Word(w="Searzol", start=0, end=0.4),
        Word(w="poly", start=0.5, end=0.8),
        Word(w="dextrose.", start=0.9, end=1.4),
    ]
    result = invoke("--episode", A)
    assert result.exit_code == 0, result.output
    assert "vocabulary: whisper prompt " in result.output
    assert "correction on (4 terms, 1 aliases)" in result.output
    assert "2 corrections" in result.output
    t = Transcript.load(transcribe.transcript_path(cfg, A))
    assert t.text() == "Searzall polydextrose." and t.words[1].orig == "poly dextrose."
    assert t.vocab_hash and t.correction_hash and len(t.corrections) == 2
    r = row(cfg, A)
    assert (r["vocab_hash"], r["correction_hash"], r["corrections"]) == (
        t.vocab_hash, t.correction_hash, 2,
    )  # fmt: skip


def test_no_correct_flag_and_config_switch(env, tmp_path):
    cfg, shared = env
    shared["words"] = [Word(w="sockslet", start=0, end=0.5)]
    result = invoke("--episode", A, "--no-correct")
    assert result.exit_code == 0 and "correction off" in result.output
    t = Transcript.load(transcribe.transcript_path(cfg, A))
    assert t.text() == "sockslet" and t.correction_hash is None and t.corrections == []
    write_config(
        tmp_path,
        "asr:\n  backend: fake\n  model: fake-model\n  correct: false\n"
        "  vocabulary_file: vocab/vocabulary.txt\n  aliases_file: vocab/aliases.txt\n",
    )
    result = invoke("--episode", B)
    assert "correction off" in result.output
    assert (
        Transcript.load(transcribe.transcript_path(load_config(tmp_path), B)).text() == "sockslet"
    )


def test_transcribe_without_a_vocabulary_does_not_touch_the_words(tmp_cfg, tmp_path):
    write_config(tmp_path, "asr:\n  backend: fake\n  model: fake-model\n")
    cfg = load_config(tmp_path, use_dotenv=False)
    register_backend("fake", lambda c, **kw: FakeTranscriber(c, **kw))
    try:
        add_episode(cfg, A, (2026, 3, 1))
        result = invoke("--episode", A)
        assert result.exit_code == 0 and "correction off" in result.output
        t = Transcript.load(transcribe.transcript_path(cfg, A))
        assert t.vocab_hash is None and t.correction_hash is None and t.corrections == []
    finally:
        ASR_BACKENDS.pop("fake", None)


def test_audio_mode_applies_correction_without_show_notes(env, tmp_path):
    _, shared = env
    shared["words"] = [Word(w="sockslet", start=0, end=0.5)]
    audio, out = tmp_path / "a.mp3", tmp_path / "out.json"
    audio.write_bytes(b"x")
    result = invoke("--audio", str(audio), "--out", str(out))
    assert result.exit_code == 0, result.output
    assert Transcript.load(out).text() == "Soxhlet"


def test_transcribe_quality_flag_prints_recall(env):
    _, shared = env
    shared["words"] = [Word(w="sockslet", start=0, end=0.5), Word(w="Searzall", start=0.6, end=1)]
    result = invoke("--episode", A, "--quality")
    assert result.exit_code == 0
    assert "term recall: 2/3 = 67% after correction, 1/3 = 33% before" in result.output


# ----------------------------------------------------------------------------- staleness


def test_changed_vocabulary_prompt_makes_a_transcript_stale(env, tmp_path):
    cfg, _ = env
    assert invoke("--episode", A).exit_code == 0
    assert "skipped" in invoke("--episode", A).output  # unchanged vocabulary: up to date
    (tmp_path / "vocab" / "vocabulary.txt").write_text(VOCAB + "Pacojet\n")
    stale = invoke("--episode", A)
    assert stale.exit_code == 0 and "STALE" in stale.output and "vocabulary prompt" in stale.output
    assert transcribe.transcript_path(cfg, A).is_file()
    dry = invoke("--episode", A, "--dry-run", "--stale")
    assert "would retranscribe" in dry.output
    redo = invoke("--episode", A, "--stale")
    assert "transcribed" in redo.output and "transcribed 1" in redo.output
    assert "skipped" in invoke("--episode", A).output


def test_removing_the_vocabulary_also_makes_it_stale(env, tmp_path):
    assert invoke("--episode", A).exit_code == 0
    (tmp_path / "vocab" / "vocabulary.txt").write_text("")
    assert "STALE" in invoke("--episode", A).output


def test_force_still_redoes_a_vocab_current_transcript(env):
    assert invoke("--episode", A).exit_code == 0
    assert "transcribed" in invoke("--episode", A, "--force").output


def test_episodes_list_flags_vocabulary_status(env, tmp_path):
    cfg, _ = env
    assert invoke("--episode", A).exit_code == 0

    def listing(*args):
        return runner.invoke(app, ["episodes", "list", *args]).output

    line = [ln for ln in listing().splitlines() if ln.startswith(A)][0]
    assert line.split()[4:6] == ["transcribed", "ok"]
    assert [ln.split()[5] for ln in listing().splitlines() if ln.startswith(B)] == ["-"]
    assert A not in listing("--stale")
    (tmp_path / "vocab" / "aliases.txt").write_text("sockslet => Soxhlet\nfoo bar => kansui\n")
    assert "recorrect" in listing() and A in listing("--stale")
    (tmp_path / "vocab" / "vocabulary.txt").write_text(VOCAB + "Pacojet\n")
    assert "STALE" in listing() and A in listing("--stale")
    assert B not in listing("--stale")  # nothing transcribed, nothing stale


def test_prompt_is_off_by_default_so_vocabulary_edits_never_make_transcripts_stale(
    tmp_cfg, tmp_path
):
    (tmp_path / "vocab").mkdir()
    (tmp_path / "vocab" / "vocabulary.txt").write_text(VOCAB)
    write_config(
        tmp_path,
        "asr:\n  backend: fake\n  model: fake-model\n  vocabulary_file: vocab/vocabulary.txt\n",
    )
    cfg = load_config(tmp_path, use_dotenv=False)
    register_backend("fake", lambda c, **kw: FakeTranscriber(c, **kw))
    try:
        add_episode(cfg, A, (2026, 3, 1))
        first = invoke("--episode", A)
        assert first.exit_code == 0
        assert "no whisper prompt (asr.vocabulary_prompt is off)" in first.output
        assert "correction on (4 terms" in first.output  # the vocabulary still corrects
        t = Transcript.load(transcribe.transcript_path(cfg, A))
        assert t.vocab_hash is None and t.initial_prompt is None
        (tmp_path / "vocab" / "vocabulary.txt").write_text(VOCAB + "Pacojet\n")
        again = invoke("--episode", A)
        assert "skipped" in again.output and "STALE" not in again.output
        listing = runner.invoke(app, ["episodes", "list"]).output
        assert "recorrect" in listing and "STALE" not in listing  # only the corrections are old
    finally:
        ASR_BACKENDS.pop("fake", None)
