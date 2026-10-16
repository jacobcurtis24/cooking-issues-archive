"""Real-model test on the GPU. Skipped by default; run with ``pytest -m gpu``.

Needs the RTX 3080, the cached large-v3 / alignment models (downloaded on first use) and the
60 s excerpt at /mnt/ssd0/ciw/audio/test_excerpt_60s.mp3. Never needs an HF token: it
transcribes without speakers. Writes only to tmp_path.
"""

from pathlib import Path

import pytest

from ciw import transcribe
from ciw.config import load_config
from ciw.models import Transcript

EXCERPT = Path("/mnt/ssd0/ciw/audio/test_excerpt_60s.mp3")

pytestmark = pytest.mark.gpu


def test_excerpt_transcribes_end_to_end_on_the_gpu(tmp_path, monkeypatch):
    if not EXCERPT.is_file():
        pytest.skip("test excerpt not available")
    monkeypatch.delenv("HF_TOKEN", raising=False)
    from ciw.asr import get_transcriber

    cfg = load_config(use_dotenv=False)
    tr = get_transcriber(cfg, diarize=False)
    out = tmp_path / "excerpt.json"
    result = transcribe.transcribe_file(EXCERPT, out, tr)
    assert result.outcome == "transcribed", result.message

    t = Transcript.load(out)
    assert 80 < len(t.words) < 400  # about a minute of speech
    assert t.language == "en" and (t.asr_model, t.compute_type) == ("large-v3", "float16")
    assert t.batch_size in (8, 4, 2) and not t.diarization and t.diarization_skipped
    assert 55 <= t.audio_duration_s <= 65 and t.duration_s <= t.audio_duration_s + 1
    for a, b in zip(t.words, t.words[1:], strict=False):
        assert b.start >= a.start and b.end >= a.end and a.end >= a.start
    assert [s.name for s in t.stages] == ["decode", "asr", "align", "diarize"]
    asr = next(s for s in t.stages if s.name == "asr")
    assert asr.peak_vram_mb and asr.peak_vram_mb > 500  # large-v3 weights alone are ~3 GB
    assert "quiche" in t.text().lower()
    assert tr.matches(t)
    assert transcribe.transcribe_file(EXCERPT, out, tr).outcome == "skipped"
