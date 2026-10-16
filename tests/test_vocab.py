"""Vocabulary mining: extraction heuristics, ranking determinism, file readers, the build CLI."""

import csv
import json
import sqlite3

from conftest import write_config
from typer.testing import CliRunner

from ciw import vocab
from ciw.cli import app
from ciw.vocab import Doc

runner = CliRunner()

NOTES = [
    "Dave makes a Searzall demo. Then Nastassia and Jack argue about polydextrose and kansui.",
    "A rotovap run with Dave Arnold. The Searzall comes back. Sous-vide eggs, CO2 and MSG too.",
    "We talk Searzall, a PolyScience circulator, and Dave Arnold at Heritage Radio Network",
    "Boring sentence about the weather. Heritage Radio Network again, with Dave Arnold.",
]


def docs(texts=NOTES, source="acast"):
    return [Doc(f"d{i}", source, t) for i, t in enumerate(texts)]


def by_key(cands):
    return {c.key: c for c in cands}


def test_extract_finds_jargon_names_acronyms_and_shapes():
    c = by_key(vocab.extract(docs()))
    for key in ("searzall", "polydextrose", "kansui", "rotovap", "polyscience", "msg", "co2"):
        assert key in c, key
    assert "camel" in c["polyscience"].flags
    assert "digit" in c["co2"].flags
    assert "acronym" in c["msg"].flags
    assert c["sous vide"].variants == ["Sous-vide"]  # hyphen/space variants share one key
    assert c["searzall"].doc_freq == 3 and c["searzall"].count == 3


def test_capitalised_runs_become_phrases():
    c = by_key(vocab.extract(docs()))
    assert "dave arnold" in c and "caprun" in c["dave arnold"].flags
    assert c["heritage radio network"].doc_freq == 2


def test_common_words_and_sentence_starts_are_not_candidates():
    c = by_key(vocab.extract(docs()))
    for key in ("boring", "sentence", "weather", "then", "talk", "about"):
        assert key not in c, key


def test_stop_terms_and_urls_are_dropped():
    text = "Subscribe on Patreon at https://patreon.com/x or call 718.497.2128. Searzall rocks."
    c = by_key(vocab.extract(docs([text])))
    assert "patreon" not in c and "subscribe" not in c and "searzall" in c
    assert not any("718" in k or "http" in k for k in c)


def test_repeated_paragraphs_are_boilerplate():
    boiler = "Brought to you by the Fizzbuzzleton Foundation every single week"
    texts = [f"{boiler}\nEpisode {i} covers the Searzall." for i in range(5)]
    c = by_key(vocab.extract(docs(texts)))
    assert not any("fizzbuzzleton" in k for k in c)
    assert "searzall" in c


def test_phrase_candidates_need_repetition_across_documents():
    texts = ["We use a cold plate here.", "A cold plate again.", "The cold plate works.", "x y z"]
    c = by_key(vocab.extract(docs(texts)))
    assert "cold plate" in c and c["cold plate"].doc_freq == 3
    once = by_key(vocab.extract(docs(["One lonely pressure vessel."])))
    assert "pressure vessel" not in once


def test_labels_are_whole_phrase_candidates_and_boost_the_score():
    blog = [
        Doc("b1", "blog", "body text here", ("spin gel clarification", "rotovap")),
        Doc("b2", "blog", "more body", ("rotovap",)),
    ]
    c = by_key(vocab.extract(blog))
    assert c["spin gel clarification"].flags == ["label"]
    assert c["rotovap"].doc_freq == 2


def test_seeds_are_included_even_if_absent_and_rank_first():
    ranked = vocab.extract(docs(), seeds=["Flavacol", "Searzall"])
    c = by_key(ranked)
    assert c["flavacol"].count == 0 and "seed" in c["flavacol"].flags
    assert {ranked[0].key, ranked[1].key} <= {"searzall", "flavacol"}
    assert c["searzall"].term == "Searzall"


def test_corroboration_across_sources_raises_the_score():
    one = vocab.extract([Doc("a", "acast", "A Searzall demo. Another Searzall demo.")])
    two = vocab.extract(
        [Doc("a", "acast", "A Searzall demo."), Doc("b", "hrn", "Another Searzall demo.")]
    )
    assert by_key(two)["searzall"].score > by_key(one)["searzall"].score
    assert by_key(two)["searzall"].sources == {"acast": 1, "hrn": 1}


def test_ranking_is_deterministic_and_input_order_independent():
    a = vocab.extract(docs())
    b = vocab.extract(docs())
    c = vocab.extract(list(reversed(docs())))
    assert [x.row() for x in a] == [x.row() for x in b]
    assert [x.key for x in a] == [x.key for x in c]
    assert all(a[i].score >= a[i + 1].score for i in range(len(a) - 1))


def test_candidates_csv_columns_and_atomic_write(tmp_path):
    path = tmp_path / "v" / "candidates.csv"
    vocab.write_candidates_csv(path, vocab.extract(docs()))
    rows = list(csv.DictReader(path.open()))
    assert list(rows[0]) == vocab.CANDIDATE_COLUMNS
    assert {"term", "variants", "count", "doc_freq", "sources", "score"} <= set(rows[0])
    assert not list(path.parent.glob("*.tmp"))
    assert path.read_text().endswith("\n") and "\r" not in path.read_text()


def test_read_terms_aliases_and_hashing(tmp_path):
    f = tmp_path / "v.txt"
    f.write_text("# header\nSearzall\n\nsous vide  # trailing comment\nSearzall\nSous-Vide\n")
    assert vocab.read_terms(f) == ["Searzall", "sous vide"]  # dupes by normalised form dropped
    assert vocab.read_terms(tmp_path / "missing.txt") == [] and vocab.read_terms(None) == []
    a = tmp_path / "a.txt"
    a.write_text("# c\nsoxlet => Soxhlet\nbad line\n  Polly Dexter=>polydextrose  \n=> x\ny =>\n")
    assert vocab.read_aliases(a) == [("soxlet", "Soxhlet"), ("Polly Dexter", "polydextrose")]
    assert vocab.hash_text("a", "b") == vocab.hash_text("a", "b") != vocab.hash_text("ab", "")


def test_norm_and_squash():
    assert vocab.norm_term("Sous-Vide") == vocab.norm_term("sous vide") == "sous vide"
    assert vocab.norm_term("Ashmead’s") == "ashmeads"
    assert vocab.squash("Pu-erh") == vocab.squash("puerh") == "puerh"


def test_english_helpers():
    assert vocab.is_common_word("kitchen") and vocab.is_common_word("poly")
    assert not vocab.is_common_word("polydextrose") and not vocab.is_common_word("kansui")
    assert vocab.zipf("the") > 7 and vocab.zipf("searzall") == 0


def test_episode_and_blog_loaders(tmp_path):
    for eid, source, desc in (("e1", "acast", "A Searzall demo."), ("e2", "hrn", "")):
        d = tmp_path / "episodes" / eid
        d.mkdir(parents=True)
        (d / "meta.json").write_text(
            json.dumps({"source": source, "title": "T", "description": desc})
        )
    (tmp_path / "episodes" / "bad").mkdir()
    (tmp_path / "episodes" / "bad" / "meta.json").write_text("{not json")
    got = vocab.episode_docs(tmp_path / "episodes")
    assert [(d.doc_id, d.source) for d in got] == [("e1", "acast"), ("e2", "hrn")]
    assert vocab.episode_docs(tmp_path / "nope") == []

    db = tmp_path / "blog.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE posts (id INTEGER PRIMARY KEY, title TEXT, categories TEXT, tags TEXT, "
        "body_text TEXT)"
    )
    conn.execute("INSERT INTO posts VALUES (1, 'Spin Gel', 'Agar, Uncategorized', 'rotovap', 'b')")
    conn.commit()
    conn.close()
    (blog,) = vocab.blog_docs(db)
    assert blog.source == "blog" and blog.labels == ("Spin Gel", "Agar", "rotovap")
    assert vocab.blog_docs(tmp_path / "missing.db") == []


def test_build_cli_writes_candidates_but_never_the_curated_vocabulary(tmp_cfg, tmp_path):
    write_config(tmp_path)
    for i, text in enumerate(NOTES):
        d = tmp_cfg.episodes_dir / f"ep{i}"
        d.mkdir(parents=True)
        (d / "meta.json").write_text(
            json.dumps({"source": "acast", "title": f"Ep {i}", "description": text})
        )
    out = tmp_path / "vocab"
    out.mkdir()
    (out / "vocabulary.txt").write_text("hand curated\n")
    (out / "seeds.txt").write_text("Flavacol\n")
    result = runner.invoke(app, ["vocab", "build", "--no-blog", "--show", "3"])
    assert result.exit_code == 0, result.output
    rows = list(csv.DictReader((out / "candidates.csv").open()))
    assert "Flavacol" in {r["term"] for r in rows} and "Searzall" in {r["term"] for r in rows}
    assert (out / "vocabulary.txt").read_text() == "hand curated\n"
    assert "mined 4 documents" in result.output and "seeds: 1" in result.output


def test_build_cli_without_show_notes_is_an_error(tmp_cfg):
    result = runner.invoke(app, ["vocab", "build", "--no-blog"])
    assert result.exit_code == 1 and "no show notes" in result.output
