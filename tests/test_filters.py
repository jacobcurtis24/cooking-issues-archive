"""Template filters registered in app.py: fmt_date and short_excerpt."""

import pytest

import app as app_module
from app import fmt_date, short_excerpt


def test_filters_are_registered_under_their_names():
    filters = app_module.app.jinja_env.filters
    assert filters["fmt_date"] is fmt_date
    assert filters["short_excerpt"] is short_excerpt


@pytest.mark.parametrize(
    "value, expected",
    [
        ("2013-11-29T11:28:51-05:00", "29 November 2013"),
        ("2009-04-30T02:09:35-05:00", "30 April 2009"),
        ("2013-08-03T07:35:05", "3 August 2013"),  # no tz, day without leading zero
        ("2013-08-03 07:35:05", "3 August 2013"),  # space separator accepted
        ("2013-08-03", "3 August 2013"),  # date only
        ("", ""),
        (None, ""),
        ("not a date at all", "not a date"),  # fallback: first 10 chars
        ("2013-13-45T00:00:00", "2013-13-45"),  # invalid month/day -> fallback
    ],
)
def test_fmt_date(value, expected):
    assert fmt_date(value) == expected


@pytest.mark.parametrize("value", ["", None])
def test_short_excerpt_empty(value):
    assert short_excerpt(value) == ""


def test_short_excerpt_short_text_unchanged_and_stripped():
    assert short_excerpt("  hello world  ") == "hello world"


def test_short_excerpt_exactly_n_is_unchanged():
    text = "a" * 220
    assert short_excerpt(text) == text
    assert short_excerpt("abcde", 5) == "abcde"


def test_short_excerpt_truncates_at_word_boundary_with_ellipsis():
    # n=10 cuts "the quick " -> rsplit drops the trailing empty piece
    assert short_excerpt("the quick brown fox", 10) == "the quick…"
    # cut mid-word: last partial word is dropped
    assert short_excerpt("the quick brown fox", 12) == "the quick…"


def test_short_excerpt_default_length():
    text = "word " * 100
    out = short_excerpt(text)
    assert out.endswith("…")
    assert len(out) <= 221
    assert out == ("word " * 44).rstrip() + "…"


def test_short_excerpt_single_long_word_without_spaces():
    # rsplit(" ", 1)[0] on text with no space returns the n-char prefix
    assert short_excerpt("x" * 50, 10) == "x" * 10 + "…"
