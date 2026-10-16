"""Transcript/show-notes FTS index: build, incremental updates, rebuild, splitting, safety."""

import sqlite3

import pytest
from webfake import add_episode, make_transcript, make_words

from ciw import search_index as si
from ciw.config import UnsafeTestPathError
from ciw.models import Transcript
from ciw.transcript import split_utterances

TEXT_A = "we talk about carbonation today and the Searzall torch."
TEXT_B = "Zymomonas is a bacterium. Crème brûlée uses a torch too."


@pytest.fixture
def world(tmp_cfg):
    add_episode(tmp_cfg, "ep-a", "Fizz and Fire", "2026-01-24", notes="All about bubbles.",
                transcript=TEXT_A)  # fmt: skip
    add_episode(
        tmp_cfg, "ep-b", "Bacteria", "2026-01-17", notes="Notes on yeast", transcript=TEXT_B
    )
    add_episode(tmp_cfg, "ep-c", "Only Notes", "2026-01-10", notes="Rotary evaporator news")
    return tmp_cfg


def counts(cfg):
    with si.open_readonly(cfg.search_db) as c:
        return (
            c.execute("SELECT COUNT(*) FROM utterances_fts").fetchone()[0],
            c.execute("SELECT COUNT(*) FROM episodes_fts").fetchone()[0],
            c.execute("SELECT COUNT(*) FROM indexed").fetchone()[0],
        )


def hits(cfg, fts):
    with si.open_readonly(cfg.search_db) as c:
        return [(h.episode_id, h.utt_idx) for h in si.search_utterances(c, fts)]


def test_build_indexes_transcripts_and_all_show_notes(world):
    rep = si.update_index(world)
    assert (rep.transcripts_indexed, rep.notes_indexed, rep.errors) == (2, 3, [])
    utts, notes, ep = counts(world)
    assert utts == 2 and notes == 3 and ep == 3  # one utterance per short transcript
    assert hits(world, '"carbonation"*') == [("ep-a", 0)]
    assert hits(world, '"cr*"') == []  # nothing matches a bare star
    assert hits(world, '"creme"*') == [("ep-b", 0)]  # diacritics removed
    assert hits(world, '"brulee"*') == [("ep-b", 0)]


def test_prefix_and_notes_search(world):
    si.update_index(world)
    with si.open_readonly(world.search_db) as c:
        assert [h.episode_id for h in si.search_utterances(c, '"carb"*')] == ["ep-a"]
        assert [h.episode_id for h in si.search_notes(c, '"evaporator"*')] == ["ep-c"]
        assert [h.episode_id for h in si.search_notes(c, '"fizz"*')] == ["ep-a"]  # title
        assert si.count_utterance_hits(c, '"torch"*') == (2, 2)


def test_second_run_is_a_no_op(world):
    si.update_index(world)
    rep = si.update_index(world)
    assert (rep.transcripts_indexed, rep.notes_indexed, rep.changed) == (0, 0, False)
    assert (rep.transcripts_unchanged, rep.notes_unchanged) == (2, 3)
    assert counts(world) == (2, 3, 3)


def test_changed_transcript_is_reindexed_only_for_that_episode(world):
    si.update_index(world)
    new = make_transcript(make_words("completely different words about gellan gum"))
    new.save(world.episodes_dir / "ep-a" / "transcript.json")
    rep = si.update_index(world)
    assert (rep.transcripts_indexed, rep.transcripts_unchanged) == (1, 1)
    assert hits(world, '"carbonation"*') == []
    assert hits(world, '"gellan"*') == [("ep-a", 0)]
    assert hits(world, '"zymomonas"*') == [("ep-b", 0)]  # untouched
    assert counts(world) == (2, 3, 3)


def test_touched_but_identical_transcript_is_not_reindexed(world):
    import os

    si.update_index(world)
    path = world.episodes_dir / "ep-a" / "transcript.json"
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    rep = si.update_index(world)
    assert (rep.transcripts_indexed, rep.transcripts_unchanged) == (0, 2)
    assert si.update_index(world).changed is False  # signature was refreshed


def test_deleted_transcript_rows_are_removed(world):
    si.update_index(world)
    (world.episodes_dir / "ep-a" / "transcript.json").unlink()
    rep = si.update_index(world)
    assert rep.transcripts_removed == 1
    assert hits(world, '"carbonation"*') == []
    assert hits(world, '"zymomonas"*') == [("ep-b", 0)]
    assert counts(world) == (1, 3, 3)  # show notes of ep-a are still searchable
    with si.open_readonly(world.search_db) as c:
        assert [h.episode_id for h in si.search_notes(c, '"bubbles"*')] == ["ep-a"]


def test_show_notes_change_updates_notes_only(world):
    si.update_index(world)
    add_episode(world, "ep-c", "Only Notes", "2026-01-10", notes="Now about pacojet")
    rep = si.update_index(world)
    assert (rep.notes_indexed, rep.transcripts_indexed) == (1, 0)
    with si.open_readonly(world.search_db) as c:
        assert si.search_notes(c, '"evaporator"*') == []
        assert [h.episode_id for h in si.search_notes(c, '"pacojet"*')] == ["ep-c"]


def test_episode_gone_from_state_is_dropped(world):
    from ciw import state

    si.update_index(world)
    with state.connect(world.state_db) as conn:
        state.delete_episode(conn, "ep-b")
    rep = si.update_index(world)
    assert rep.episodes_removed == 1
    assert hits(world, '"zymomonas"*') == []
    assert counts(world) == (1, 2, 2)


def test_selected_episodes_only_and_force(world):
    rep = si.update_index(world, episode_ids=["ep-a"])
    assert (rep.transcripts_indexed, rep.notes_indexed) == (1, 1)
    assert counts(world) == (1, 1, 1)
    rep = si.update_index(world, episode_ids=["ep-a", "nope"], force=True)
    assert (rep.transcripts_indexed, rep.errors) == (1, [])
    assert counts(world) == (1, 1, 1)  # replaced, not duplicated


def test_rebuild_matches_incremental_and_replaces_the_file(world):
    si.update_index(world)
    before = counts(world)
    junk = world.search_db.parent / "search.sqlite-wal"
    junk.write_bytes(b"stale")  # a leftover side file must not corrupt the new index
    rep = si.rebuild_index(world)
    assert rep.transcripts_indexed == 2
    assert counts(world) == before
    assert hits(world, '"carbonation"*') == [("ep-a", 0)]
    assert not list(world.search_db.parent.glob("search.sqlite.new*"))
    assert not junk.exists()


def test_bad_transcript_is_an_error_not_a_crash(world):
    si.update_index(world)
    (world.episodes_dir / "ep-b" / "transcript.json").write_text("{not json")
    rep = si.update_index(world)
    assert len(rep.errors) == 1 and rep.errors[0].startswith("ep-b")
    assert hits(world, '"zymomonas"*') == [("ep-b", 0)]  # old rows kept


def test_no_state_db_is_an_empty_index(tmp_cfg):
    rep = si.update_index(tmp_cfg)
    assert (rep.total_episodes, rep.total_utterances) == (0, 0)


def test_index_best_effort_never_raises(world, monkeypatch):
    msgs = []
    monkeypatch.setattr(si, "update_index", lambda *a, **k: 1 / 0)
    assert si.index_best_effort(world, ["ep-a"], echo=msgs.append) is None
    assert "search index not updated" in msgs[0] and "ZeroDivisionError" in msgs[0]


def test_snippets_are_escaped_and_marked(tmp_cfg):
    add_episode(tmp_cfg, "ep-x", "X", transcript="use <script>alert(1)</script> carbonation & more")
    si.update_index(tmp_cfg)
    with si.open_readonly(tmp_cfg.search_db) as c:
        (hit,) = si.search_utterances(c, '"carbonation"*')
    assert "<mark>carbonation</mark>" in hit.snippet
    assert "<script>" not in hit.snippet and "&lt;script&gt;" in hit.snippet
    assert "&amp; more" in hit.snippet


def test_missing_or_foreign_index_raises_sqlite_error(tmp_path):
    with pytest.raises(sqlite3.Error), si.open_readonly(tmp_path / "nope.sqlite"):
        pass
    other = tmp_path / "other.sqlite"
    sqlite3.connect(other).execute("CREATE TABLE t(x)").connection.commit()
    with pytest.raises(sqlite3.Error), si.open_readonly(other):
        pass


def test_open_index_refuses_a_newer_schema(tmp_cfg):
    si.update_index(tmp_cfg)
    conn = sqlite3.connect(tmp_cfg.search_db)
    conn.execute("PRAGMA user_version = 99")
    conn.close()
    with pytest.raises(RuntimeError, match="rebuild"):
        si.update_index(tmp_cfg)


def test_guard_blocks_the_real_search_db(tmp_cfg):
    cfg = tmp_cfg.model_copy(update={"search_db": tmp_cfg.root.parent / "x"})
    from ciw.config import code_root

    with pytest.raises(UnsafeTestPathError):
        si.open_index(code_root() / "data" / "search.sqlite")
    assert cfg.search_db.name == "x"


# --- utterance splitting -------------------------------------------------------------------


def long_words(n, **kw):
    return make_words(" ".join(f"w{i}" for i in range(n)), **kw)


def test_split_cuts_long_utterances_to_at_most_max_words():
    utts = split_utterances(long_words(130), max_words=50)
    assert [len(u.words) for u in utts] == [50, 50, 30]
    assert utts[0].start == 0 and utts[1].start == utts[0].words[-1].end + 0.05
    assert sum(len(u.words) for u in utts) == 130


def test_split_prefers_a_sentence_end_then_the_longest_pause():
    ws = long_words(120)
    ws[39] = ws[39].model_copy(update={"w": "end."})  # sentence end inside [25, 50]
    utts = split_utterances(ws, max_words=50)
    assert len(utts[0].words) == 40 and utts[0].words[-1].w == "end."
    ws = long_words(120, gaps={33: 1.0})  # a pause < 1.5 s (no utterance break) before word 33
    utts = split_utterances(ws, max_words=50)
    assert len(utts[0].words) == 33


def test_split_respects_pauses_and_speakers_and_short_utterances():
    ws = make_words("a b c", speaker="S1") + make_words("d e", start=20, speaker="S2")
    utts = split_utterances(ws, max_words=50)
    assert [(u.speaker, u.text) for u in utts] == [("S1", "a b c"), ("S2", "d e")]
    assert split_utterances([]) == []


def test_indexed_utterances_are_the_displayed_ones(tmp_cfg):
    ws = long_words(130)
    add_episode(tmp_cfg, "ep-l", "Long", transcript=make_transcript(ws))
    si.update_index(tmp_cfg)
    with si.open_readonly(tmp_cfg.search_db) as c:
        rows = c.execute(
            "SELECT utt_idx, start_s, end_s, text FROM utterances_fts ORDER BY rowid"
        ).fetchall()
    expect = si.build_utterances(Transcript.load(tmp_cfg.episodes_dir / "ep-l" / "transcript.json"))
    assert [r["utt_idx"] for r in rows] == [0, 1, 2] == [u.idx for u in expect]
    assert [r["text"] for r in rows] == [u.text for u in expect]
