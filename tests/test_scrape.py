"""scrape.py helpers: fix_mojibake (and friends). Pure functions, no network."""

import pytest
from bs4 import BeautifulSoup

import scrape


@pytest.mark.parametrize(
    "broken, fixed",
    [
        ("donâ€™t", "don’t"),            # right single quote / apostrophe
        ("â€œhelloâ€\x9d", "“hello”"),  # curly double quotes (0x9d is undefined in cp1252)
        ("â€“", "–"),                      # en dash
        ("x â€” y", "x — y"),               # em dash
        ("waitâ€¦", "wait…"),               # ellipsis
        ("cafÃ©", "café"),                 # 2-byte sequence (e-acute)
        ("Â°", "°"),                       # degree sign
        ("200Â°C", "200°C"),
        ("Â\xa0x", "\xa0x"),                    # mojibake of NBSP
    ],
)
def test_fix_mojibake_known_cases(broken, fixed):
    assert scrape.fix_mojibake(broken) == fixed


@pytest.mark.parametrize(
    "clean",
    [
        "plain ascii text",
        "already fine: don’t, café, naïve, “quoted”, 200°C",
        "— em dash … and nbsp\xa0here",
        "日本語",  # non-Latin text passes through untouched
    ],
)
def test_fix_mojibake_leaves_clean_text_unchanged(clean):
    assert scrape.fix_mojibake(clean) == clean


def test_fix_mojibake_mixed_clean_and_broken():
    # a correct curly quote adjacent to mojibake must survive (see _fix_chunk docs)
    assert scrape.fix_mojibake("“quotedâ€\u009d and itâ€™s") == "“quoted” and it’s"
    assert scrape.fix_mojibake("“quoted”â€™") == "“quoted”’"
    assert scrape.fix_mojibake("Ã©â€™") == "é’"
    assert scrape.fix_mojibake("a â€™ b cafÃ© c") == "a ’ b café c"


def test_fix_mojibake_is_idempotent():
    once = scrape.fix_mojibake("itâ€™s â€œniceâ€\x9d cafÃ©")
    assert scrape.fix_mojibake(once) == once


@pytest.mark.parametrize("value, expected", [("", ""), (None, None)])
def test_fix_mojibake_falsy_passthrough(value, expected):
    assert scrape.fix_mojibake(value) == expected


def test_fix_mojibake_lone_lead_byte_chars_are_left_alone():
    # Not valid mojibake sequences: nothing decodes, text is kept as-is.
    assert scrape.fix_mojibake("Ã") == "Ã"
    assert scrape.fix_mojibake("â") == "â"


def test_cp1252_reverse_map_is_complete():
    assert len(scrape._CP1252_REVERSE) == 256
    assert scrape._CP1252_REVERSE["’"] == 0x92
    assert scrape._CP1252_REVERSE["€"] == 0x80
    assert scrape._CP1252_REVERSE["\x81"] == 0x81  # undefined cp1252 byte -> latin-1


def test_fix_encoding_module_agrees_with_scrape():
    """fix_encoding.py carries its own copy of fix_mojibake; they must agree."""
    import fix_encoding

    for s in ["donâ€™t", "â€œhiâ€\x9d", "cafÃ©", "clean ’ text", "Ã", "", "x â€” y"]:
        assert fix_encoding.fix_mojibake(s) == scrape.fix_mojibake(s)


def test_clean_text_collapses_whitespace():
    soup = BeautifulSoup("<div><p>a</p>\n\n<p>b   c</p></div>", "html.parser")
    assert scrape.clean_text(soup) == "a b c"


def test_parse_post_offline_fixture():
    html = """
    <article>
      <h1 class="entry-title">Don&acirc;&#8364;&trade;t Panic</h1>
      <time class="entry-date" datetime="2012-05-01T10:00:00-05:00">May 1</time>
      <span class="byline"><a rel="author">Dave Arnold</a></span>
      <span class="cat-links"><a>Techniques</a><a>Recipes</a></span>
      <span class="tags-links"><a>agar</a><a>sous vide</a></span>
      <div class="entry-content"><p>Hello   <b>world</b></p></div>
    </article>"""
    post = scrape.parse_post(
        BeautifulSoup(html, "html.parser"),
        "https://cookingissues.com/2012/05/01/x/",
    )
    assert post["title"] == "Don’t Panic"
    assert post["date"] == "2012-05-01T10:00:00-05:00"
    assert post["author"] == "Dave Arnold"
    assert post["categories"] == "Techniques, Recipes"
    assert post["tags"] == "agar, sous vide"
    assert post["body_text"] == "Hello world"
    assert post["post_type"] == "post"
    assert post["url"] == "https://cookingissues.com/2012/05/01/x/"
    assert post["scraped_at"]


def test_parse_post_date_falls_back_to_url_and_missing_parts_are_empty():
    post = scrape.parse_post(
        BeautifulSoup("<html></html>", "html.parser"),
        "https://cookingissues.com/2011/03/09/slug/",
        "primer",
    )
    assert post["date"] == "2011-03-09"
    assert post["title"] == post["author"] == post["tags"] == post["body_text"] == ""
    assert post["post_type"] == "primer"
