"""Synthetic podcast data for the transcript browser tests: episodes, transcripts and fake audio.

Everything is written under a tmp config (``tmp_cfg``): state rows, ``meta.json``, optional
``transcript.json`` and an audio file inside ``cfg.audio_dir``. Never the real ``data/``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from ciw import state
from ciw.config import Config
from ciw.models import Correction, EpisodeMeta, Transcript, Word

AUDIO_BYTES = bytes(range(256)) * 8  # 2048 bytes, every offset distinguishable


def make_words(
    text: str,
    start: float = 0.0,
    step: float = 0.4,
    gaps: dict[int, float] | None = None,
    speaker: str | None = None,
) -> list[Word]:
    """One word per whitespace token, ``step`` seconds each; ``gaps[i]`` adds silence before
    word ``i``."""
    out, t = [], start
    for i, tok in enumerate(text.split()):
        t += (gaps or {}).get(i, 0.0)
        out.append(Word(w=tok, start=round(t, 3), end=round(t + step - 0.05, 3), speaker=speaker))
        t += step
    return out


def make_transcript(words: list[Word], **kw) -> Transcript:
    base = dict(
        asr_model="large-v3",
        asr_version="fake 1",
        words=words,
        runtime_s=100.0,
        audio_duration_s=max((w.end for w in words), default=0) + 1,
    )
    base.update(kw)
    return Transcript(**base)


def corrected(words: list[Word], index: int, heard: str) -> list[Word]:
    """Mark ``words[index]`` as corrected from ``heard`` (what the ASR said)."""
    w = words[index]
    words[index] = w.model_copy(update={"orig": heard})
    return words


def correction_for(words: list[Word], index: int, heard: str, rule: str = "alias") -> Correction:
    w = words[index]
    return Correction(
        index=index,
        original=heard,
        corrected=w.w,
        start=w.start,
        end=w.end,
        rule=rule,
        confidence=1.0,
        term=w.w,
        original_words=[Word(w=heard, start=w.start, end=w.end)],
    )


def add_episode(
    cfg: Config,
    episode_id: str,
    title: str,
    pub: str = "2026-01-24",
    *,
    source: str = "acast",
    notes: str = "",
    notes_html: str = "",
    duration_s: float | None = 3600.0,
    transcript: Transcript | str | None = None,
    audio: bool | bytes = False,
    audio_name: str | None = None,
    audio_path: str | None = None,
    audio_duration_s: float | None = None,
    meta: bool = True,
) -> Path:
    """Create an episode in tmp state. ``transcript`` may be a Transcript or plain text;
    ``audio`` True writes AUDIO_BYTES to ``<audio_dir>/<id>.mp3`` and records it."""
    stamp = datetime.fromisoformat(pub).replace(tzinfo=UTC)
    em = EpisodeMeta(
        guid=episode_id,
        guids={source: episode_id},
        source=source,
        audio_source=source,
        title=title,
        pub_date=stamp,
        description=notes,
        description_html=notes_html,
        duration_s=duration_s,
        enclosure_url="https://example.invalid/x.mp3",
    )
    edir = cfg.episodes_dir / episode_id
    if meta:
        em.save(edir / "meta.json")
    with state.connect(cfg.state_db) as conn:
        state.upsert_episode(conn, em)
        if audio or audio_path:
            data = AUDIO_BYTES if audio is True else (audio or AUDIO_BYTES)
            if audio_path is None:
                cfg.audio_dir.mkdir(parents=True, exist_ok=True)
                path = cfg.audio_dir / (audio_name or f"{episode_id}.mp3")
                path.write_bytes(data)
                audio_path = str(path)
            state.mark_downloaded(
                conn,
                episode_id,
                audio_path=audio_path,
                audio_bytes=len(data) if isinstance(data, bytes) else 0,
                audio_duration_s=audio_duration_s,
                audio_sha256=None,
                audio_downloaded_from="test",
            )
        if transcript is not None:
            if isinstance(transcript, str):
                transcript = make_transcript(make_words(transcript))
            transcript.save(edir / "transcript.json")
            state.mark_transcribed(
                conn,
                episode_id,
                asr_model=transcript.asr_model,
                asr_version=transcript.asr_version,
                asr_runtime_s=transcript.runtime_s,
                diarization=transcript.diarization,
                correction_hash=transcript.correction_hash,
                corrections=len(transcript.corrections),
            )
    return edir
