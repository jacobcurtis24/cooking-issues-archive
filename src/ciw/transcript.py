"""Turn a word list into readable utterance lines: ``[mm:ss] SPEAKER_01: text``.

The same rendering feeds the LLM prompts (T9), the transcript browser (T7) and the quality check
(T6), so the grouping rules live here, once.

Grouping rules (:func:`utterances`): a new utterance starts when

- the speaker changes (``None`` counts as its own "speaker");
- the silence before a word is longer than ``pause_s`` (default 1.5 s); or
- the previous word ended a sentence (``. ? !``, optionally followed by closing quotes or
  brackets) and the utterance is already ``sentence_break_s`` (default 15 s) long. Without that
  length floor a monologue would turn into one line per sentence.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from ciw.models import Transcript, Word

UNKNOWN_SPEAKER = "SPEAKER_UNKNOWN"
# A stretch this long with no words between two words is suspicious: dropped audio (T6).
GAP_S = 10.0
# Search index and transcript page cut utterances at this many words (see split_utterances).
UTTERANCE_MAX_WORDS = 50
MIN_CUT_PAUSE_S = 0.2
_SENTENCE_END_CHARS = ".?!"
_CLOSERS = "\"')]}”’»"


@dataclass
class Utterance:
    start: float
    end: float
    speaker: str | None
    words: list[Word] = field(default_factory=list)

    @property
    def text(self) -> str:
        return " ".join(w.w for w in self.words)


def ends_sentence(token: str) -> bool:
    """True when ``token`` ends with ., ? or ! (ignoring trailing quotes/brackets)."""
    token = token.rstrip(_CLOSERS)
    return bool(token) and token[-1] in _SENTENCE_END_CHARS


def format_timestamp(seconds: float) -> str:
    """``mm:ss`` with unbounded minutes (``61:25``), so a line never needs an hour field."""
    total = max(int(seconds), 0)
    return f"{total // 60:02d}:{total % 60:02d}"


def utterances(
    words: Iterable[Word],
    *,
    pause_s: float = 1.5,
    sentence_break_s: float = 15.0,
) -> list[Utterance]:
    """Group consecutive words into utterances (see the module docstring for the rules)."""
    out: list[Utterance] = []
    current: Utterance | None = None
    for word in words:
        if current is not None:
            prev = current.words[-1]
            split = (
                word.speaker != current.speaker
                or word.start - prev.end > pause_s
                or (ends_sentence(prev.w) and prev.end - current.start >= sentence_break_s)
            )
            if split:
                current = None
        if current is None:
            current = Utterance(start=word.start, end=word.end, speaker=word.speaker)
            out.append(current)
        current.words.append(word)
        current.end = max(current.end, word.end)
    return out


def split_utterances(
    words: Iterable[Word],
    *,
    max_words: int = 50,
    pause_s: float = 1.5,
    sentence_break_s: float = 15.0,
) -> list[Utterance]:
    """:func:`utterances`, with any utterance of more than ``max_words`` words cut into pieces.

    Whisper often gives a long unpunctuated monologue; one block of 300 words is a poor search
    hit and a poor seek target. A cut goes after the last sentence end in the second half of the
    window, else at the longest pause there, else at ``max_words``.
    """
    out: list[Utterance] = []
    for utt in utterances(words, pause_s=pause_s, sentence_break_s=sentence_break_s):
        rest = utt.words
        while len(rest) > max_words:
            lo = max(1, max_words // 2)
            cut = max_words
            sentence_ends = [i for i in range(lo, max_words + 1) if ends_sentence(rest[i - 1].w)]
            if sentence_ends:
                cut = sentence_ends[-1]
            else:
                gaps = [(rest[i].start - rest[i - 1].end, i) for i in range(lo, max_words + 1)]
                best_gap, best_i = max(gaps)
                if best_gap >= MIN_CUT_PAUSE_S:  # a real pause, not timestamp jitter
                    cut = best_i
            out.append(_piece(rest[:cut], utt.speaker))
            rest = rest[cut:]
        out.append(_piece(rest, utt.speaker))
    return out


def _piece(words: list[Word], speaker: str | None) -> Utterance:
    return Utterance(
        start=words[0].start, end=max(w.end for w in words), speaker=speaker, words=words
    )


def silent_gaps(words: Sequence[Word], min_s: float = GAP_S) -> list[tuple[float, float]]:
    """``(start, end)`` of every stretch of at least ``min_s`` seconds between two words."""
    return [
        (a.end, b.start) for a, b in zip(words, words[1:], strict=False) if b.start - a.end >= min_s
    ]


@dataclass(frozen=True)
class GapStats:
    """How much of an episode is silent gaps (>= ``GAP_S`` s between words)."""

    gap_s: float
    count: int
    suspect: bool  # the gaps add up to more than ``suspect_pct`` percent of the audio


def gap_stats(
    transcript: Transcript, audio_duration_s: float | None, suspect_pct: float
) -> GapStats:
    """Total silent-gap seconds and count, and whether they exceed ``suspect_pct`` percent of the
    audio (``audio_duration_s``, else the transcript's own, else its last word's end). Gaps are
    signals, not errors: music and ads are legitimately wordless."""
    gaps = silent_gaps(transcript.words)
    total = sum(b - a for a, b in gaps)
    duration = (
        audio_duration_s
        or transcript.audio_duration_s
        or (transcript.words[-1].end if transcript.words else 0.0)
    )
    return GapStats(
        round(total, 2), len(gaps), bool(duration) and total > suspect_pct / 100 * duration
    )


def render_utterances(
    source: Transcript | Iterable[Word],
    *,
    pause_s: float = 1.5,
    sentence_break_s: float = 15.0,
) -> str:
    """One ``[mm:ss] SPEAKER_01: text`` line per utterance, newline separated.

    If no word has a speaker at all (no diarization) the speaker label is left out
    (``[mm:ss] text``); if only some words lack one they are labelled ``SPEAKER_UNKNOWN``.
    """
    words = list(source.words if isinstance(source, Transcript) else source)
    has_speakers = any(w.speaker for w in words)
    lines = []
    for utt in utterances(words, pause_s=pause_s, sentence_break_s=sentence_break_s):
        stamp = f"[{format_timestamp(utt.start)}]"
        if has_speakers:
            lines.append(f"{stamp} {utt.speaker or UNKNOWN_SPEAKER}: {utt.text}")
        else:
            lines.append(f"{stamp} {utt.text}")
    return "\n".join(lines)
