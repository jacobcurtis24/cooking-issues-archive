"""Episode page, audio streaming, transcript download and the HTML contract the JS relies on."""

import os
import re
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from webfake import (
    AUDIO_BYTES,
    add_episode,
    corrected,
    correction_for,
    make_transcript,
    make_words,
)

from ciw import state
from ciw.web import create_app

STATIC = Path(__file__).resolve().parent.parent / "src" / "ciw" / "web" / "static"

SENTENCE_1 = "welcome to the show today we talk about carbonation and polydextrose in baking."
SENTENCE_2 = "then a long pause and we continue with the Searzall torch for the crust."


def make_tr():
    # utterance 1 at 0s; a 3 s pause; utterance 2; a 30 s silent gap; utterance 3
    first = make_words(SENTENCE_1)
    second = make_words(SENTENCE_2, start=first[-1].end + 3.0)
    third = make_words("and the final bit.", start=second[-1].end + 30.0)
    words = corrected(first + second + third, 10, "poly dextrose")
    tr = make_transcript(words, runtime_s=104.0)
    tr.corrections = [correction_for(words, 10, "poly dextrose")]
    return tr


@pytest.fixture
def cfg(blog_cfg):
    add_episode(
        blog_cfg, "full", "Biscuits & Bouillon", "2026-03-01",
        notes="Plain notes", notes_html='<p>Hello <a href="https://example.com/x">link</a>'
        "<script>alert(1)</script></p>", transcript=make_tr(), audio=True,
    )  # fmt: skip
    add_episode(blog_cfg, "audio", "Audio Only", "2026-02-01", notes="Only audio.", audio=True)
    add_episode(
        blog_cfg, "notes", "Notes Only", "2026-01-01", source="hrn", notes="Just notes.\n\nTwo."
    )
    return blog_cfg


@pytest.fixture
def web(cfg):
    app = create_app(cfg)
    app.config["TESTING"] = True
    return app.test_client()


def soup(resp) -> BeautifulSoup:
    return BeautifulSoup(resp.get_data(as_text=True), "html.parser")


# --- stages ----------------------------------------------------------------------------------


def test_transcribed_episode_page(web):
    r = web.get("/episode/full")
    assert r.status_code == 200
    s = soup(r)
    assert s.select_one("h1").text == "Biscuits & Bouillon"
    assert "Transcribed" in s.select_one(".stage-transcribed").text
    audio = s.select_one("audio#player")
    assert audio["src"] == "/audio/full" and audio.has_attr("controls")
    assert audio["preload"] == "metadata"
    utts = s.select("#transcript .utt")
    assert len(utts) == 3
    assert utts[0].select_one(".utt-text").get_text().startswith("welcome to the show")
    assert s.select_one('a[href="/episode/full/transcript.txt"]')
    head = s.select_one("#transcript-info").get_text(" ", strip=True)
    assert "ASR: large-v3" in head and "1 vocabulary correction" in head
    assert "transcribed in 104 s" in head and "no speaker labels" in head
    assert "1 untranscribed gap" in head


def test_show_notes_are_sanitised_on_the_page(web):
    s = soup(web.get("/episode/full"))
    notes = s.select_one(".show-notes")
    assert notes.select_one("a")["href"] == "https://example.com/x"
    assert "alert(1)" not in str(notes) and "<script" not in str(notes)


def test_hostile_show_notes_never_reach_the_page(blog_cfg):
    hostile = '<img src=x onerror=alert(1)><script>boom()</script><a href="javascript:x()">j</a>'
    add_episode(blog_cfg, "bad", "<b>Evil</b> & Title", notes_html=hostile, notes="plain")
    t = create_app(blog_cfg).test_client().get("/episode/bad").get_data(as_text=True)
    assert "onerror" not in t and "boom()" not in t and "javascript:" not in t
    assert "&lt;b&gt;Evil&lt;/b&gt; &amp; Title" in t  # titles are escaped by the template


def test_plain_text_notes_used_when_there_is_no_html(web):
    s = soup(web.get("/episode/notes"))
    assert [p.get_text() for p in s.select(".show-notes p")] == ["Just notes.", "Two."]


def test_audio_only_episode(web):
    s = soup(web.get("/episode/audio"))
    assert s.select_one("audio#player") is not None
    assert s.select_one("#transcript") is None
    note = s.select_one("#transcript-status").text
    assert "not transcribed yet" in note and "ciw transcribe --episode audio" in note
    assert s.select_one('script[src*="episode.js"]') is None  # no transcript, no JS needed


def test_show_notes_only_episode(web):
    r = web.get("/episode/notes")
    s = soup(r)
    assert r.status_code == 200
    assert s.select_one("audio") is None and s.select_one("#transcript") is None
    note = s.select_one("#transcript-status").text
    assert "ciw download --episode notes" in note and "ciw transcribe --episode notes" in note
    assert "Just notes." in s.select_one(".show-notes").text


def test_unknown_episode_is_404(web):
    assert web.get("/episode/does-not-exist").status_code == 404
    assert web.get("/episode/does-not-exist/transcript.txt").status_code == 404
    assert web.get("/audio/does-not-exist").status_code == 404


def test_missing_meta_json_still_renders(blog_cfg):
    add_episode(blog_cfg, "nm", "No meta", meta=False)
    r = create_app(blog_cfg).test_client().get("/episode/nm")
    assert r.status_code == 200 and "No show notes" in r.get_data(as_text=True)


def test_state_says_transcribed_but_file_is_gone_or_corrupt(cfg, web):
    (cfg.episodes_dir / "full" / "transcript.json").write_text("{broken")
    r = web.get("/episode/full")
    assert r.status_code == 200
    assert "could not be read" in soup(r).select_one("#transcript-status").text
    (cfg.episodes_dir / "full" / "transcript.json").unlink()
    assert (
        "missing or unreadable"
        in soup(web.get("/episode/full")).select_one("#transcript-status").text
    )
    assert web.get("/episode/full/transcript.txt").status_code == 404


def test_recorded_audio_that_is_gone_shows_a_notice_without_a_player(cfg, web):
    (cfg.audio_dir / "full.mp3").unlink()
    s = soup(web.get("/episode/full"))
    assert s.select_one("audio") is None
    assert "ciw download --episode full --redownload" in s.select_one("#audio-problem").text
    assert len(s.select("#transcript .utt")) == 3  # the transcript is still shown


# --- transcript rendering ---------------------------------------------------------------------


def test_corrections_are_shown_with_what_was_heard(web):
    s = soup(web.get("/episode/full"))
    spans = s.select("#transcript span.corr")
    assert len(spans) == 1
    assert spans[0].text == "polydextrose" and spans[0]["title"] == "heard: poly dextrose"


def test_gap_marker_between_utterances(web):
    s = soup(web.get("/episode/full"))
    gaps = s.select("#transcript .gap-marker")
    assert len(gaps) == 1
    g = gaps[0]
    assert float(g["data-gap-end"]) - float(g["data-gap-start"]) >= 30
    assert "Untranscribed gap" in g.text and "30 s" in g.text
    kids = [c for c in s.select_one("#transcript").children if getattr(c, "name", None)]
    assert [("gap" if "gap-marker" in c["class"] else "utt") for c in kids] == [
        "utt", "utt", "gap", "utt"
    ]  # fmt: skip
    assert g.select_one("a.gap-seek")["data-seek"] == g["data-gap-start"]


def test_a_gap_below_ten_seconds_is_not_a_marker(blog_cfg):
    ws = make_words("one two three.") + make_words("four five six.", start=9.0)
    add_episode(blog_cfg, "g9", "G", transcript=make_transcript(ws))
    s = soup(create_app(blog_cfg).test_client().get("/episode/g9"))
    assert not s.select(".gap-marker") and len(s.select(".utt")) == 2


def test_query_highlights_matches_and_counts_them(web):
    r = web.get("/episode/full?q=carbonation")
    s = soup(r)
    marks = s.select("#transcript mark.hit")
    assert [m.text for m in marks] == ["carbonation"]
    assert s.select_one("#find")["value"] == "carbonation"
    assert s.select_one("#find-count").text.strip() == "1 match"
    assert s.select_one("#find-count")["data-server-hits"] == "1"
    prefix = soup(web.get("/episode/full?q=carb+the")).select("#transcript mark.hit")
    assert {m.text.lower() for m in prefix} >= {"carbonation", "the"}
    assert soup(web.get("/episode/full?q=zzzz")).select("mark.hit") == []
    assert "0 matches" in soup(web.get("/episode/full?q=zzzz")).select_one("#find-count").text


def test_query_is_escaped_everywhere(web):
    t = web.get('/episode/full?q="><script>alert(1)</script>').get_data(as_text=True)
    assert "<script>alert(1)" not in t


def test_query_highlight_works_on_corrected_words_and_diacritics(blog_cfg):
    ws = make_words("a creme brulee and polydextrose here")
    corrected(ws, 4, "poly dextrose")
    add_episode(blog_cfg, "d", "D", transcript=make_transcript(ws))
    s = soup(create_app(blog_cfg).test_client().get("/episode/d?q=crème+polydex"))
    assert {m.text for m in s.select("mark.hit")} == {"creme", "polydextrose"}
    assert s.select_one("span.corr mark.hit") is not None


def test_transcript_text_download(web):
    r = web.get("/episode/full/transcript.txt")
    assert r.status_code == 200
    assert r.content_type.startswith("text/plain") and "charset=utf-8" in r.content_type
    assert "attachment" in r.headers["Content-Disposition"]
    lines = r.get_data(as_text=True).splitlines()
    assert lines[0].startswith("[00:00] welcome to the show")
    assert any("polydextrose" in line for line in lines) and len(lines) == 3
    assert web.get("/episode/audio/transcript.txt").status_code == 404


# --- HTML contract for episode.js --------------------------------------------------------------


def test_html_contract_for_the_javascript(web):
    s = soup(web.get("/episode/full?q=carbonation"))
    assert s.select_one("audio#player") and s.select_one("#transcript")
    for el_id in ("follow", "find", "find-prev", "find-next", "find-count"):
        assert s.select_one(f"#{el_id}"), el_id
    utts = s.select("#transcript .utt")
    ids = [u["id"] for u in utts]
    assert ids == [f"u{i}" for i in range(len(utts))]
    prev_end = -1.0
    for u in utts:
        start, end = float(u["data-start"]), float(u["data-end"])
        assert 0 <= start < end and start >= prev_end
        prev_end = end
        ts = u.select_one("a.ts")
        assert ts["href"] == f"#t={int(start)}"
        assert re.fullmatch(r"\[\d+:\d{2}\]", ts.text)
        assert u.select_one(".utt-text")
    script = s.select_one('script[src*="episode.js"]')
    assert script and script["src"] == "/static/episode.js"


def test_the_javascript_uses_exactly_the_hooks_the_html_provides():
    js = (STATIC / "episode.js").read_text()
    for needle in ("getElementById('player')", "getElementById('transcript')",
                   "getElementById('follow')", "getElementById('find')",
                   "getElementById('find-count')", "getElementById('find-prev')",
                   "getElementById('find-next')", "dataset.start", "dataset.end",
                   "dataset.seek", "'.utt'", ".utt-text", "mark.hit", "mark.flt", "a.gap-seek",
                   "a.ts", "#t=", "timeupdate", "loadedmetadata", "scrollIntoView"):  # fmt: skip
        assert needle in js, needle
    assert "http://" not in js and "https://" not in js  # no CDN, no network
    assert "eval(" not in js and "innerHTML" not in js  # nothing that parses strings as HTML


def test_episode_js_and_css_are_served(web):
    assert web.get("/static/episode.js").status_code == 200
    assert "javascript" in web.get("/static/episode.js").content_type


# --- audio ------------------------------------------------------------------------------------


def test_audio_full_response(web):
    r = web.get("/audio/full")
    assert r.status_code == 200
    assert r.data == AUDIO_BYTES
    assert r.headers["Content-Type"] == "audio/mpeg"
    assert r.headers["Accept-Ranges"] == "bytes"
    assert r.headers["Cache-Control"] == "private, no-cache"
    assert r.headers["Content-Length"] == str(len(AUDIO_BYTES))
    assert r.headers.get("ETag") and r.headers.get("Last-Modified")
    assert r.headers["X-Content-Type-Options"] == "nosniff"


@pytest.mark.parametrize(
    "rng, lo, hi",
    [("bytes=0-99", 0, 99), ("bytes=100-199", 100, 199), ("bytes=2000-", 2000, 2047),
     ("bytes=-48", 2000, 2047), ("bytes=2040-99999", 2040, 2047), ("bytes=0-0", 0, 0)],
)  # fmt: skip
def test_audio_range_requests(web, rng, lo, hi):
    r = web.get("/audio/full", headers={"Range": rng})
    assert r.status_code == 206
    assert r.data == AUDIO_BYTES[lo : hi + 1]
    assert r.headers["Content-Range"] == f"bytes {lo}-{hi}/{len(AUDIO_BYTES)}"
    assert r.headers["Content-Length"] == str(hi - lo + 1)
    assert r.headers["Accept-Ranges"] == "bytes" and r.headers["Content-Type"] == "audio/mpeg"
    assert r.headers["Cache-Control"] == "private, no-cache"


@pytest.mark.parametrize("rng", ["bytes=5000-6000", "bytes=2048-", "bytes=9999-"])
def test_unsatisfiable_ranges_are_416(web, rng):
    r = web.get("/audio/full", headers={"Range": rng})
    assert r.status_code == 416
    assert r.headers["Content-Range"] == f"bytes */{len(AUDIO_BYTES)}"


@pytest.mark.parametrize("rng", ["bytes=abc", "items=0-5", "bytes=5-2", "garbage", "bytes="])
def test_malformed_ranges_never_500(web, rng):
    r = web.get("/audio/full", headers={"Range": rng})
    assert r.status_code in (200, 416)


def test_conditional_requests(web):
    first = web.get("/audio/full")
    etag, modified = first.headers["ETag"], first.headers["Last-Modified"]
    assert web.get("/audio/full", headers={"If-None-Match": etag}).status_code == 304
    assert web.get("/audio/full", headers={"If-Modified-Since": modified}).status_code == 304
    # If-Range with the right validator honours the range, with a stale one sends everything
    ok = web.get("/audio/full", headers={"Range": "bytes=0-9", "If-Range": etag})
    assert ok.status_code == 206
    stale = web.get("/audio/full", headers={"Range": "bytes=0-9", "If-Range": '"old"'})
    assert stale.status_code == 200 and len(stale.data) == len(AUDIO_BYTES)
    head = web.head("/audio/full")
    assert head.status_code == 200 and head.data == b""


@pytest.mark.parametrize(
    "name, mime",
    [("a.mp3", "audio/mpeg"), ("a.m4a", "audio/mp4"), ("a.MP3", "audio/mpeg"),
     ("a.bin", "application/octet-stream")],
)  # fmt: skip
def test_audio_mime_types(blog_cfg, name, mime):
    add_episode(blog_cfg, "m", "M", audio=True, audio_name=name)
    r = create_app(blog_cfg).test_client().get("/audio/m")
    assert r.status_code == 200 and r.headers["Content-Type"] == mime


def test_episode_without_audio_or_with_deleted_file_is_404(cfg, web):
    assert web.get("/audio/notes").status_code == 404
    (cfg.audio_dir / "audio.mp3").unlink()
    assert web.get("/audio/audio").status_code == 404


# --- path safety: crafted state rows and URLs -------------------------------------------------


def set_audio_path(cfg, episode_id, value):
    with state.connect(cfg.state_db) as conn:
        conn.execute("UPDATE episodes SET audio_path=? WHERE episode_id=?", (value, episode_id))


def test_audio_outside_audio_dir_is_never_served(cfg, web, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET")
    sibling = tmp_path / (cfg.audio_dir.name + "_evil")  # shares the audio_dir name as a prefix
    sibling.mkdir()
    (sibling / "x.mp3").write_bytes(b"evil")
    (cfg.audio_dir / "link.mp3").symlink_to(secret)  # symlink inside audio_dir, target outside
    (cfg.audio_dir / "subdir").mkdir()
    crafted = [
        str(secret),
        "/etc/passwd",
        str(cfg.audio_dir / ".." / "secret.txt"),
        str(cfg.audio_dir) + "/../../etc/passwd",
        str(sibling / "x.mp3"),
        str(cfg.audio_dir / "link.mp3"),
        str(cfg.audio_dir / "subdir"),  # a directory
        str(cfg.audio_dir),
        "relative/audio.mp3",
        "../../etc/passwd",
        "",
        "\x00",
        str(cfg.audio_dir / "does-not-exist.mp3"),
    ]
    for value in crafted:
        set_audio_path(cfg, "full", value)
        r = web.get("/audio/full")
        assert r.status_code == 404, value
        assert b"TOP SECRET" not in r.data and b"evil" not in r.data
        page = web.get("/episode/full")  # the page must not offer a player for it either
        assert page.status_code == 200 and soup(page).select_one("audio") is None, value


def test_a_symlink_inside_audio_dir_to_a_file_inside_is_fine(cfg, web):
    (cfg.audio_dir / "alias.mp3").symlink_to(cfg.audio_dir / "full.mp3")
    set_audio_path(cfg, "full", str(cfg.audio_dir / "alias.mp3"))
    assert web.get("/audio/full").status_code == 200


@pytest.mark.parametrize(
    "url",
    ["/audio/../../etc/passwd", "/audio/%2e%2e%2f%2e%2e%2fetc/passwd", "/audio/..%2fsecret",
     "/audio/full/../audio", "/audio//etc/passwd", "/audio/%00", "/audio/full%00.mp3",
     "/audio/..\\..\\x"],
)  # fmt: skip
def test_urls_cannot_name_a_path(web, url):
    r = web.get(url)
    assert r.status_code in (404, 308, 301), url
    assert b"root:" not in r.data


def test_the_audio_route_takes_only_an_episode_id(web):
    rules = {r.rule: r for r in web.application.url_map.iter_rules()}
    assert "/audio/<episode_id>" in rules and "path" not in str(rules["/audio/<episode_id>"])


def test_audio_dir_must_exist(cfg, web):
    import shutil

    shutil.rmtree(cfg.audio_dir)
    assert web.get("/audio/full").status_code == 404
    assert os.path.isdir(cfg.root)
