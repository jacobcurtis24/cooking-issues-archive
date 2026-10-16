"""Sanitise untrusted HTML (podcast show notes from a feed) with a strict allow-list.

The output is rebuilt from the parsed tree, never copied: only the tags and attributes below
survive, text is re-escaped, comments and processing instructions are dropped, and a link keeps
its ``href`` only if it is ``http(s)``.

Policy
- kept: ``p br ul ol li a strong em b i h2 h3 h4 blockquote``;
- ``h1`` becomes ``h2``, ``h5``/``h6`` become ``h4``; ``div`` becomes ``p`` when it holds no block
  element; every other unknown tag is unwrapped (its text is kept);
- dropped together with their content: ``script style iframe object embed noscript template svg
  math textarea select option title head frame frameset applet canvas audio video source track``
  and a few legacy ones;
- attributes: only ``a[href]`` (``http://`` or ``https://`` after removing control characters and
  whitespace tricks such as ``java&#9;script:``) and the added
  ``rel="noopener nofollow noreferrer" target="_blank"``. No ``style``, ``class``, ``id``,
  ``on*``, ``src``, ``srcset``, ``data:`` or ``javascript:`` URLs;
- input is capped (500 kB) and absurdly deep nesting degrades to plain escaped text.
"""

from __future__ import annotations

import html
import re

from bs4 import BeautifulSoup, NavigableString, Tag

ALLOWED = frozenset("p br ul ol li a strong em b i h2 h3 h4 blockquote".split())
RENAMED = {"h1": "h2", "h5": "h4", "h6": "h4"}
DROP = frozenset(
    "script style iframe object embed noscript template svg math textarea select option title "
    "head frame frameset applet canvas audio video source track noembed noframes xmp plaintext "
    "datalist dialog".split()
)
BLOCKS = frozenset(
    "p ul ol li h1 h2 h3 h4 h5 h6 blockquote div table pre section article header footer "
    "form hr".split()
)
VOID = frozenset({"br"})
MAX_INPUT = 500_000

_CONTROL_OR_SPACE = re.compile(r"[\x00-\x20\x7f-\x9f​-‏  ﻿]")
_HTTP = re.compile(r"(?i)^https?://[^/?#\s]")


def safe_href(value: str | None) -> str | None:
    """The link target if it is an http(s) URL, else None."""
    if not value:
        return None
    compact = _CONTROL_OR_SPACE.sub("", value)
    if not _HTTP.match(compact):
        return None
    return compact


def sanitize_html(markup: str | None) -> str:
    """Allow-listed HTML for ``markup`` (see the module docstring); '' for empty input."""
    if not markup or not markup.strip():
        return ""
    markup = markup[:MAX_INPUT]
    soup = BeautifulSoup(markup, "html.parser")
    out: list[str] = []
    try:
        _walk(soup, out)
    except RecursionError:
        return html.escape(soup.get_text(" ", strip=True), quote=False)
    return "".join(out).strip()


def _walk(node: Tag | BeautifulSoup, out: list[str]) -> None:
    for child in node.children:
        if type(child) is NavigableString:
            out.append(html.escape(str(child), quote=False))
            continue
        if not isinstance(child, Tag):
            continue  # comment, doctype, CDATA, processing instruction
        name = (child.name or "").lower()
        if name in DROP:
            continue
        name = RENAMED.get(name, name)
        if name == "div":
            name = "p" if not child.find(list(BLOCKS)) else ""
        if name == "a":
            href = safe_href(child.get("href") if isinstance(child.get("href"), str) else None)
            if href is None:
                name = ""  # a link without a safe target is just its text
            else:
                out.append(
                    f'<a href="{html.escape(href, quote=True)}" '
                    'rel="noopener nofollow noreferrer" target="_blank">'
                )
                _walk(child, out)
                out.append("</a>")
                continue
        if name in ALLOWED:
            if name in VOID:
                out.append("<br>")
                continue
            out.append(f"<{name}>")
            _walk(child, out)
            out.append(f"</{name}>")
        else:
            _walk(child, out)  # unwrap: keep the text, lose the tag


def plain_to_html(text: str | None) -> str:
    """Paragraphs for plain-text show notes: blank lines split paragraphs, newlines become br."""
    if not text or not text.strip():
        return ""
    paras = [p for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    return "".join(
        "<p>"
        + "<br>".join(html.escape(line.strip(), quote=False) for line in p.split("\n"))
        + "</p>"
        for p in paras
    )
