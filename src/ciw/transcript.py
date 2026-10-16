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

from collections.abc import Iterable
from dataclasses import dataclass, field

from ciw.models import Transcript, Word

UNKNOWN_SPEAKER = "SPEAKER_UNKNOWN"
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
