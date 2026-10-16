"""Match the same episode across the Acast and Patreon feeds.

The two feeds use different GUIDs, so an Acast item and a Patreon item are *candidates* for the
same episode when all of these hold:

* normalized titles are equal, or very similar (``TITLE_SIMILARITY``) with identical numbers;
* publish dates are within ``DATE_WINDOW_DAYS`` (inclusive);
* durations agree within ``max(DURATION_TOLERANCE_S, DURATION_TOLERANCE_PCT * longer)``
  (an unknown duration does not veto a match).

Only an unambiguous 1:1 candidate relationship becomes a match. If an item has two or more
candidates (or two items compete for one) nothing is guessed: the whole group is reported as
ambiguous and every member stays unmatched. HRN never takes part.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import timedelta
from difflib import SequenceMatcher

from ciw.models import FeedItem

DATE_WINDOW_DAYS = 3
DURATION_TOLERANCE_PCT = 0.10
DURATION_TOLERANCE_S = 120.0
TITLE_SIMILARITY = 0.90


def _compact(norm: str) -> str:
    return norm.replace(" ", "")


def titles_match(a: FeedItem, b: FeedItem) -> bool:
    na, nb = a.title_norm, b.title_norm
    if not na or not nb:
        return False
    if _compact(na) == _compact(nb):
        return True
    if re.findall(r"\d+", na) != re.findall(r"\d+", nb):
        return False
    return SequenceMatcher(None, na, nb).ratio() >= TITLE_SIMILARITY


def dates_match(a: FeedItem, b: FeedItem) -> bool:
    return abs(a.pub_date - b.pub_date) <= timedelta(days=DATE_WINDOW_DAYS)


def durations_match(a: FeedItem, b: FeedItem) -> bool:
    if a.duration_s is None or b.duration_s is None:
        return True
    tol = max(DURATION_TOLERANCE_S, DURATION_TOLERANCE_PCT * max(a.duration_s, b.duration_s))
    return abs(a.duration_s - b.duration_s) <= tol


def is_candidate(acast: FeedItem, patreon: FeedItem) -> bool:
    return (
        titles_match(acast, patreon)
        and dates_match(acast, patreon)
        and durations_match(acast, patreon)
    )


@dataclass
class Ambiguity:
    acast: list[FeedItem]
    patreon: list[FeedItem]

    def describe(self) -> str:
        a = "; ".join(f"{i.title!r} ({i.guid})" for i in self.acast)
        p = "; ".join(f"{i.title!r} ({i.pub_date:%Y-%m-%d})" for i in self.patreon)
        return f"acast [{a}] vs patreon [{p}]"


@dataclass
class MatchResult:
    pairs: list[tuple[FeedItem, FeedItem]] = field(default_factory=list)
    ambiguous: list[Ambiguity] = field(default_factory=list)
    acast_only: list[FeedItem] = field(default_factory=list)
    patreon_only: list[FeedItem] = field(default_factory=list)


def match_feeds(
    acast_items: list[FeedItem],
    patreon_items: list[FeedItem],
    locked: Iterable[tuple[str, str]] = (),
) -> MatchResult:
    """Pair Acast and Patreon items. ``locked`` are (acast_guid, patreon_guid) pairs already
    recorded in state; they stay paired whatever the rules now say (identity is sticky)."""
    result = MatchResult()
    a_by = {i.guid: i for i in acast_items}
    p_by = {i.guid: i for i in patreon_items}
    used_a: set[str] = set()
    used_p: set[str] = set()
    for ag, pg in locked:
        if ag in a_by and pg in p_by and ag not in used_a and pg not in used_p:
            result.pairs.append((a_by[ag], p_by[pg]))
            used_a.add(ag)
            used_p.add(pg)

    free_a = [i for i in acast_items if i.guid not in used_a]
    free_p = [i for i in patreon_items if i.guid not in used_p]
    cand_a: dict[str, list[str]] = {i.guid: [] for i in free_a}
    cand_p: dict[str, list[str]] = {i.guid: [] for i in free_p}
    for a in free_a:
        for p in free_p:
            if is_candidate(a, p):
                cand_a[a.guid].append(p.guid)
                cand_p[p.guid].append(a.guid)

    contested_a: set[str] = set()
    contested_p: set[str] = set()
    for a in free_a:
        c = cand_a[a.guid]
        if len(c) == 1 and len(cand_p[c[0]]) == 1:
            result.pairs.append((a, p_by[c[0]]))
            used_a.add(a.guid)
            used_p.add(c[0])
        elif c:
            contested_a.add(a.guid)
            contested_p.update(c)

    # group the contested items into connected components, one Ambiguity per component
    seen_a: set[str] = set()
    for start in sorted(contested_a):
        if start in seen_a:
            continue
        comp_a, comp_p, stack = set(), set(), [("a", start)]
        while stack:
            kind, guid = stack.pop()
            if kind == "a" and guid not in comp_a:
                comp_a.add(guid)
                stack.extend(("p", g) for g in cand_a[guid])
            elif kind == "p" and guid not in comp_p:
                comp_p.add(guid)
                stack.extend(("a", g) for g in cand_p[guid])
        seen_a |= comp_a
        result.ambiguous.append(
            Ambiguity(
                acast=[a_by[g] for g in sorted(comp_a, key=lambda g: a_by[g].pub_date)],
                patreon=[p_by[g] for g in sorted(comp_p, key=lambda g: p_by[g].pub_date)],
            )
        )

    result.acast_only = [i for i in acast_items if i.guid not in used_a]
    result.patreon_only = [i for i in patreon_items if i.guid not in used_p]
    return result
