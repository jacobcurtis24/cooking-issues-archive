"""cp1252 mojibake repair (the single source used by scrape.py and fix_encoding.py).

UTF-8 text decoded as cp1252 shows up as e.g. ``â€™`` instead of ``’``; this rebuilds the
original bytes and re-decodes them as UTF-8.
"""

import re

# Build complete cp1252 reverse map: Unicode char → byte value.
# For the 5 undefined cp1252 bytes, Python's cp1252 codec yields the same
# code point as the byte value (Latin-1 fallback), so ord(char) == byte.
_CP1252_REVERSE: dict[str, int] = {}
for _b in range(256):
    try:
        _char = bytes([_b]).decode("cp1252")
    except UnicodeDecodeError:
        _char = chr(_b)          # undefined byte → Latin-1 equivalent char
    _CP1252_REVERSE[_char] = _b


def _fix_chunk(chunk: str) -> str:
    """Convert one run of non-ASCII mojibake chars back to UTF-8 text.

    Handles two tricky cases:
    1. Adjacent mojibake sequences whose combined bytes form invalid UTF-8
       (e.g. â€\x9d followed by Â whose continuation byte is ASCII): falls
       back to greedy prefix matching.
    2. A correctly-decoded Unicode char (e.g. " U+201D → cp1252 byte 0x94)
       sitting adjacent to mojibake: its cp1252 byte is a UTF-8 continuation
       byte, so no prefix decode can start there. Detected by checking whether
       the first byte would be a UTF-8 continuation byte (0x80–0xBF), in which
       case we leave that char in place and recurse on the rest.
    """
    if not chunk:
        return chunk
    raw = bytes(_CP1252_REVERSE.get(c, ord(c) & 0xFF) for c in chunk)
    # If the first byte is a UTF-8 continuation byte, the leading character is
    # already a correctly-stored Unicode scalar — pass it through and recurse.
    if 0x80 <= raw[0] <= 0xBF:
        return chunk[0] + _fix_chunk(chunk[1:])
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    # Greedy: find the longest valid UTF-8 prefix, fix it, recurse on remainder
    for n in range(len(raw) - 1, 0, -1):
        try:
            head = raw[:n].decode("utf-8")
            tail = _fix_chunk(chunk[n:])
            return head + tail
        except UnicodeDecodeError:
            continue
    return chunk  # nothing decoded — leave unchanged


def fix_mojibake(text: str) -> str:
    """Fix all mojibake runs in a string. Idempotent: already-correct text is unchanged."""
    if not text:
        return text
    return re.sub(r"[^\x00-\x7f]+", lambda m: _fix_chunk(m.group()), text)
