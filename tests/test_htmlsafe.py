"""The show-notes sanitiser: allow-list, hostile input, structure of the output."""

import pytest
from bs4 import BeautifulSoup

from ciw.htmlsafe import ALLOWED, plain_to_html, safe_href, sanitize_html

HOSTILE = [
    "<script>alert(1)</script>",
    "<SCRIPT SRC=//evil.example/x.js></SCRIPT>",
    "<scr<script>ipt>alert(1)</scr</script>ipt>",
    "<img src=x onerror=alert(1)>",
    "<img src=x onerror=alert(1)//",
    "<svg onload=alert(1)><script>alert(2)</script></svg>",
    '<svg><a xlink:href="javascript:alert(1)"><text>x</text></a></svg>',
    "<math><mi//xlink:href='data:x,<script>alert(1)</script>'>",
    '<iframe src="javascript:alert(1)"></iframe>',
    '<iframe srcdoc="<script>alert(1)</script>"></iframe>',
    '<object data="data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg=="></object>',
    '<embed src="x.swf">',
    '<a href="javascript:alert(1)">click</a>',
    '<a href=" JaVaScRiPt:alert(1)">click</a>',
    '<a href="java&#x09;script:alert(1)">click</a>',
    '<a href="java&#10;script:alert(1)">click</a>',
    '<a href="&#106;avascript:alert(1)">click</a>',
    '<a href="\x01javascript:alert(1)">click</a>',
    '<a href="data:text/html,<script>alert(1)</script>">click</a>',
    '<a href="vbscript:msgbox(1)">click</a>',
    '<a href="//evil.example/x">click</a>',
    '<a href="mailto:a@b.c">click</a>',
    '<a href="http://ok.example/" onclick="alert(1)" onmouseover=alert(2) style="x:y">ok</a>',
    '<p onclick="alert(1)" style="background:url(javascript:alert(1))" class="x" id="y">hi</p>',
    '<style>body{background:url("javascript:alert(1)")}</style>text',
    '<link rel=stylesheet href="http://evil.example/x.css">',
    '<meta http-equiv="refresh" content="0;url=http://evil.example">',
    '<base href="http://evil.example/">',
    '<form action="http://evil.example"><input name=pw><button>go</button></form>',
    "<textarea><script>alert(1)</script></textarea>",
    '<noscript><p title="</noscript><img src=x onerror=alert(1)>"></noscript>',
    "<template><script>alert(1)</script></template>",
    "<!--<script>alert(1)</script>--><b>x</b>",
    "<![CDATA[<script>alert(1)</script>]]>",
    "<?php echo 1; ?>",
    "<video><source onerror=alert(1)></video><audio src=x onerror=alert(1)>",
    "<details open ontoggle=alert(1)>",
    '<b style="position:fixed;top:0;left:0;width:100%;height:100%">overlay</b>',
    '<a href="http://x.example/" target="_self" rel="opener">x</a>',
    "<p>&lt;script&gt;alert(1)&lt;/script&gt;</p>",
    "<<script>script>alert(1)<</script>/script>",
    '<a href="http://x.example/"onclick=alert(1)>x</a>',
    '"><script>alert(1)</script>',
    "<body onload=alert(1)><div onmouseover=alert(1)>x</div></body>",
    "<h1 onclick=1>t</h1><h6>u</h6>",
]


def parsed(out: str) -> BeautifulSoup:
    return BeautifulSoup(out, "html.parser")


@pytest.mark.parametrize("markup", HOSTILE)
def test_hostile_input_yields_only_allowed_tags_and_attributes(markup):
    out = sanitize_html(markup)
    low = out.lower()
    for bad in ("<script", "onerror", "onclick", "onload", "onmouseover", "javascript:", "<iframe",
                "<style", "<img", "<svg", "<object", "<embed", "<form", "<input", "style=",
                "srcdoc", "data:", "vbscript:", "ontoggle", "<link", "<meta", "<base",
                "<template", "<noscript", "<textarea"):  # fmt: skip
        assert bad not in low, (markup, out)
    for tag in parsed(out).find_all(True):
        assert tag.name in ALLOWED, (markup, out)
        allowed_attrs = {"href", "rel", "target"} if tag.name == "a" else set()
        assert set(tag.attrs) <= allowed_attrs, (markup, out)
        if tag.name == "a":
            assert tag["href"].lower().startswith(("http://", "https://")), (markup, out)
            assert "nofollow" in tag["rel"] and "noopener" in tag["rel"]
    # re-sanitising its own output changes nothing (so re-parsing cannot resurrect anything)
    assert sanitize_html(out) == out


def test_allowed_markup_is_kept():
    src = (
        "<h2>Title</h2><p>Hello <strong>bold</strong>, <em>it</em>, <b>b</b> <i>i</i><br>"
        'and <a href="https://example.com/a?b=1&amp;c=2">a link</a>.</p>'
        "<ul><li>one</li><li>two</li></ul><ol><li>x</li></ol><blockquote>q</blockquote>"
        "<h3>h3</h3><h4>h4</h4>"
    )
    out = sanitize_html(src)
    soup = parsed(out)
    assert [t.name for t in soup.find_all(True)] == [
        "h2", "p", "strong", "em", "b", "i", "br", "a", "ul", "li", "li", "ol", "li",
        "blockquote", "h3", "h4",
    ]  # fmt: skip
    a = soup.find("a")
    assert a["href"] == "https://example.com/a?b=1&c=2"
    assert a["rel"] == ["noopener", "nofollow", "noreferrer"] and a["target"] == "_blank"
    assert "bold" in out and "a link" in out


def test_text_of_unknown_tags_is_kept_and_dangerous_content_is_dropped():
    out = sanitize_html("<div>one <span>two</span> <u>three</u></div><script>secret()</script>")
    assert parsed(out).get_text() == "one two three"
    assert "secret" not in out and "<span" not in out
    assert sanitize_html("<style>.x{}</style>kept") == "kept"
    assert "x" in sanitize_html('<a href="javascript:alert(1)">x</a>')  # link text survives
    assert "<a" not in sanitize_html('<a href="javascript:alert(1)">x</a>')


def test_headings_are_normalised_and_divs_become_paragraphs():
    out = sanitize_html("<h1>a</h1><h5>b</h5><div>c</div><div><p>d</p></div>")
    assert out == "<h2>a</h2><h4>b</h4><p>c</p><p>d</p>"


def test_text_is_reescaped():
    out = sanitize_html("<p>1 < 2 & 3 > 2 &lt;b&gt;</p>")
    assert "<b>" not in out and "&amp;" in out and "&lt;" in out
    assert parsed(out).get_text() == "1 < 2 & 3 > 2 <b>"


def test_href_rules():
    assert safe_href("https://a.example/x") == "https://a.example/x"
    assert safe_href("HTTP://A.example") == "HTTP://A.example"
    assert safe_href(" http://a.example/ x") == "http://a.example/x"
    for bad in (None, "", "/relative", "//host/x", "javascript:1", "ftp://a", "http:", "https://",
                "java\tscript:1", "data:text/html,x", "mailto:a@b"):  # fmt: skip
        assert safe_href(bad) is None, bad


def test_empty_and_comment_only_input():
    assert sanitize_html(None) == "" and sanitize_html("") == "" and sanitize_html("  \n") == ""
    assert sanitize_html("<!-- nothing -->") == ""


def test_deep_nesting_and_huge_input_do_not_crash():
    deep = "<div>" * 5000 + "x" + "</div>" * 5000
    out = sanitize_html(deep)
    assert "x" in out and "<script" not in out
    big = "<p>a</p>" * 200_000
    assert len(sanitize_html(big)) < 600_000
    assert sanitize_html("<b>" * 3000 + "<script>x</script>").count("script") == 0


def test_plain_to_html():
    assert plain_to_html("") == "" and plain_to_html(None) == ""
    assert (
        plain_to_html("a <b>\nsecond\n\nnext & more")
        == "<p>a &lt;b&gt;<br>second</p><p>next &amp; more</p>"
    )
