"""WhisperX backend logic without a GPU, models, network or HF token.

``torch`` and ``whisperx`` are replaced by in-memory fakes so the whole ``transcribe`` flow
(stage order, OOM back-off, speakers, graceful degradation, redaction) is exercised.
"""

import os
import sys
import types
from pathlib import Path

import pytest

from ciw.asr import whisperx_backend as wb
from ciw.asr.base import SKIP_DISABLED, SKIP_ERROR, SKIP_GATED, SKIP_NO_TOKEN
from ciw.config import Secrets

TOKEN = "hf_SUPERSECRETTOKEN123456"


# ---------------------------------------------------------------- back-off ladder


def test_backoff_plan_halves_to_the_minimum_then_changes_compute_type():
    assert wb.backoff_plan(8, 2, ["float16", "int8_float16"]) == [
        ("float16", 8), ("float16", 4), ("float16", 2),
        ("int8_float16", 8), ("int8_float16", 4), ("int8_float16", 2),
    ]  # fmt: skip
    assert wb.backoff_plan(3, 2, ["float16"]) == [("float16", 3), ("float16", 2)]
    assert wb.backoff_plan(1, 2, ["float16"]) == [("float16", 2)]
    assert wb.backoff_plan(2, 2, ["a", "b"]) == [("a", 2), ("b", 2)]


class FakeOom(RuntimeError):
    pass


def oom():
    return RuntimeError("CUDA failed with error out of memory")


@pytest.mark.parametrize(
    "exc, expected",
    [
        (RuntimeError("CUDA failed with error out of memory"), True),
        (RuntimeError("CUDA out of memory. Tried to allocate 2.00 GiB"), True),
        (RuntimeError("CUBLAS_STATUS_ALLOC_FAILED when calling cublasCreate"), True),
        (type("OutOfMemoryError", (RuntimeError,), {})("anything"), True),
        (RuntimeError("something else broke"), False),
        (ValueError("out of memory"), False),
        (KeyboardInterrupt(), False),
    ],
)
def test_is_oom(exc, expected):
    assert wb.is_oom(exc) is expected


def run_ladder(fail_when, plan=None):
    """Run the ladder where ``fail_when(compute_type, batch)`` says if an attempt OOMs."""
    tried, cleanups, notes = [], [], []

    def attempt(compute_type, batch):
        tried.append((compute_type, batch))
        if fail_when(compute_type, batch):
            raise oom()
        return f"ok-{compute_type}-{batch}"

    plan = plan or wb.backoff_plan(8, 2, ["float16", "int8_float16"])
    out = wb.run_with_oom_backoff(
        attempt, plan, cleanup=lambda drop: cleanups.append(drop), notes=notes
    )
    return out, tried, cleanups, notes


def test_no_oom_means_one_attempt_and_no_cleanup():
    out, tried, cleanups, notes = run_ladder(lambda c, b: False)
    assert out == ("ok-float16-8", "float16", 8)
    assert tried == [("float16", 8)] and cleanups == [] and notes == []


def test_oom_halves_the_batch_and_reloads_the_model():
    out, tried, cleanups, notes = run_ladder(lambda c, b: b > 2)
    assert out[1:] == ("float16", 2)
    assert tried == [("float16", 8), ("float16", 4), ("float16", 2)]
    assert cleanups == [True, True]  # a CTranslate2 model is unusable after an OOM: reload
    assert len(notes) == 2 and "batch_size=8" in notes[0] and "batch_size=4" in notes[0]


def test_oom_at_every_float16_batch_falls_back_to_int8_float16_and_retries_from_8():
    out, tried, cleanups, notes = run_ladder(lambda c, b: c == "float16")
    assert out == ("ok-int8_float16-8", "int8_float16", 8)
    assert tried == [("float16", 8), ("float16", 4), ("float16", 2), ("int8_float16", 8)]
    assert cleanups == [True, True, True]
    assert "compute_type=int8_float16" in notes[2]


def test_fallback_ladder_also_halves():
    out, tried, _, _ = run_ladder(lambda c, b: c == "float16" or b > 4)
    assert out[1:] == ("int8_float16", 4)
    assert tried[-2:] == [("int8_float16", 8), ("int8_float16", 4)]


def test_exhausted_ladder_raises_with_every_setting_listed():
    with pytest.raises(wb.OutOfMemoryExhausted, match=r"float16/8.*int8_float16/2") as info:
        run_ladder(lambda c, b: True)
    assert isinstance(info.value.__cause__, RuntimeError)


def test_non_oom_errors_are_not_retried():
    calls = []

    def attempt(compute_type, batch):
        calls.append(batch)
        raise ValueError("not memory")

    with pytest.raises(ValueError):
        wb.run_with_oom_backoff(attempt, wb.backoff_plan(8, 2, ["float16"]))
    assert calls == [8]


# ----------------------------------------------------------------------- word handling


def seg(words, start=None, end=None, text="", speaker=None):
    d = {"start": start, "end": end, "text": text, "words": words}
    if speaker:
        d["speaker"] = speaker
    return d


def test_unaligned_words_are_interpolated_between_their_neighbours():
    segs = [
        seg(
            [
                {"word": "In", "start": 1.0, "end": 1.2},
                {"word": "2024"},  # a number: wav2vec2 cannot align it
                {"word": "it", "start": 2.0, "end": 2.2},
            ],
            start=1.0,
            end=2.2,
        )
    ]
    words, n = wb.words_from_segments(segs)
    assert n == 1
    mid = words[1]
    assert (mid.w, mid.start, mid.end) == ("2024", 1.2, 2.0)
    assert [w.w for w in words] == ["In", "2024", "it"]


def test_run_of_unaligned_words_is_spread_by_length():
    segs = [
        seg(
            [
                {"word": "a", "start": 0.0, "end": 1.0},
                {"word": "12"},
                {"word": "3456"},
                {"word": "z", "start": 7.0, "end": 8.0},
            ],
            start=0,
            end=8,
        )  # fmt: skip
    ]
    words, n = wb.words_from_segments(segs)
    assert n == 2
    assert (words[1].start, words[1].end) == (1.0, 3.0)  # 2 of 6 characters of the 6 s gap
    assert (words[2].start, words[2].end) == (3.0, 7.0)


def test_leading_and_trailing_unaligned_words_use_the_segment_bounds():
    segs = [
        seg(
            [{"word": "10"}, {"word": "x", "start": 2.0, "end": 3.0}, {"word": "11"}],
            start=1.0,
            end=5.0,
        )
    ]
    words, _ = wb.words_from_segments(segs)
    assert (words[0].start, words[0].end) == (1.0, 2.0)
    assert (words[2].start, words[2].end) == (3.0, 5.0)


def test_segment_without_any_words_is_split_on_whitespace_and_spread():
    segs = [seg([], start=10.0, end=14.0, text=" 5 4 3 2 ")]
    words, n = wb.words_from_segments(segs)
    assert n == 4 and [w.w for w in words] == ["5", "4", "3", "2"]
    assert words[0].start == 10.0 and words[-1].end == 14.0


def test_timestamps_are_forced_monotonic_and_never_none():
    segs = [
        seg([{"word": "a", "start": 5.0, "end": 6.0}, {"word": "b", "start": 4.0, "end": 4.5}]),
        seg([{"word": "c", "start": 3.0, "end": 9.0}, {"word": "d", "start": 9.5, "end": 9.1}]),
    ]
    words, _ = wb.words_from_segments(segs)
    for prev, cur in zip(words, words[1:], strict=False):
        assert cur.start >= prev.start and cur.end >= prev.end
    assert all(w.end >= w.start for w in words)


def test_empty_words_dropped_and_speakers_taken_from_word_then_segment():
    segs = [
        seg(
            [
                {"word": " ", "start": 0, "end": 1},
                {"word": "hi", "start": 0, "end": 1, "speaker": "SPEAKER_01"},
                {"word": "there", "start": 1, "end": 2},
            ],
            speaker="SPEAKER_00",
        )
    ]
    words, _ = wb.words_from_segments(segs)
    assert [(w.w, w.speaker) for w in words] == [("hi", "SPEAKER_01"), ("there", "SPEAKER_00")]


def test_no_segments_no_words():
    assert wb.words_from_segments([]) == ([], 0)


# ----------------------------------------------------------- vocabulary / environment


def test_vocabulary_prompt(tmp_path):
    f = tmp_path / "vocab.txt"
    f.write_text("# jargon\nSearzall\n\nSpinzall  # tool\nkansui\n", encoding="utf-8")
    assert wb.load_vocabulary_prompt(f) == "Searzall, Spinzall, kansui"
    assert wb.load_vocabulary_prompt(f, max_chars=20) == "Searzall, Spinzall"  # whole terms only
    assert wb.load_vocabulary_prompt(None) is None
    assert wb.load_vocabulary_prompt(tmp_path / "missing.txt") is None
    (tmp_path / "empty.txt").write_text("# nothing\n")
    assert wb.load_vocabulary_prompt(tmp_path / "empty.txt") is None
    assert wb.prompt_hash(None) is None and len(wb.prompt_hash("x")) == 12


def test_prepare_environment_puts_the_interpreter_bin_first_and_sets_caches(tmp_cfg):
    env = {"PATH": "/usr/bin:/bin"}
    changed = wb.prepare_environment(tmp_cfg.asr, env)
    assert env["PATH"].split(os.pathsep)[0] == os.path.dirname(sys.executable)
    assert env["PATH"].endswith("/usr/bin" + os.pathsep + "/bin")
    assert env["HF_HOME"] == str(tmp_cfg.asr.hf_home)
    assert env["TORCH_HOME"] == str(Path(tmp_cfg.asr.hf_home) / "torch")
    assert {"PATH", "HF_HOME", "TORCH_HOME"} <= set(changed)
    assert wb.prepare_environment(tmp_cfg.asr, env) == {}  # idempotent


def test_prepare_environment_respects_an_existing_hf_home(tmp_cfg):
    env = {"PATH": os.path.dirname(sys.executable), "HF_HOME": "/elsewhere"}
    wb.prepare_environment(tmp_cfg.asr, env)
    assert env["HF_HOME"] == "/elsewhere" and env["TORCH_HOME"] == "/elsewhere/torch"


def test_version_comes_from_metadata_without_importing_heavy_modules(tmp_cfg):
    tr = wb.WhisperXTranscriber(tmp_cfg)
    assert tr.version.startswith("whisperx ") and "ctranslate2" in tr.version
    assert tr.model == "large-v3" and "whisperx" in tr.describe()
    assert wb.WhisperXTranscriber(tmp_cfg, model="large-v3-turbo").model == "large-v3-turbo"


# ----------------------------------------------------------------- the fake GPU stack


class FakeCuda:
    def __init__(self):
        self.cache_emptied = 0
        self.used = 1_000_000_000
        self.total = 10_000_000_000

    def is_available(self):
        return True

    def get_device_name(self, i=0):
        return "Fake RTX"

    def mem_get_info(self):
        return self.total - self.used, self.total

    def reset_peak_memory_stats(self):
        pass

    def max_memory_allocated(self):
        return 3 * 1024 * 1024

    def empty_cache(self):
        self.cache_emptied += 1


class FakeStack:
    """Installs fake torch + whisperx in sys.modules and records what was called."""

    def __init__(self, monkeypatch, *, oom_when=lambda ct, bs: False, diarize_error=None):
        self.log = []
        self.oom_when = oom_when
        self.diarize_error = diarize_error
        self.diarize_kwargs = None
        self.cuda = FakeCuda()
        torch = types.ModuleType("torch")
        torch.cuda = self.cuda
        stack = self

        class Pipeline:
            def __init__(self, compute_type, prompt):
                self.compute_type, self.prompt = compute_type, prompt

            def transcribe(self, audio, batch_size, language, progress_callback=None):
                stack.log.append(("asr", self.compute_type, batch_size, self.prompt))
                if stack.oom_when(self.compute_type, batch_size):
                    raise RuntimeError("CUDA failed with error out of memory")
                progress_callback(100.0)
                return {
                    "language": language,
                    "segments": [
                        {"start": 0.0, "end": 2.0, "text": " Hello 2024 world."},
                        {"start": 3.0, "end": 4.0, "text": " Bye."},
                    ],
                }

        wx = types.ModuleType("whisperx")
        wx.load_audio = lambda path: [0.0] * (16000 * 10)  # 10 s
        wx.load_model = lambda name, device, **kw: (
            stack.log.append(("load_model", kw["compute_type"], name, kw["vad_method"]))
            or Pipeline(kw["compute_type"], kw["asr_options"]["initial_prompt"])
        )
        wx.load_align_model = lambda lang, device: (
            stack.log.append(("load_align", lang)) or ("align-model", {"language": lang})
        )

        def align(segments, model, meta, audio, device, **kw):
            stack.log.append(("align", len(segments)))
            out = []
            for s in segments:
                toks = s["text"].split()
                words, t = [], s["start"]
                for tok in toks:
                    w = {"word": tok}
                    if tok.isalpha() or tok[:-1].isalpha():
                        w.update(start=round(t, 2), end=round(t + 0.4, 2))
                        t += 0.5
                    words.append(w)
                out.append({**s, "words": words})
            return {"segments": out, "word_segments": []}

        wx.align = align

        def assign(frame, aligned, fill_nearest=False):
            stack.log.append(("assign", fill_nearest))
            for s in aligned["segments"]:
                for w in s["words"]:
                    w["speaker"] = "SPEAKER_01" if w["word"].startswith("Bye") else "SPEAKER_00"
            return aligned

        wx.assign_word_speakers = assign
        diarize = types.ModuleType("whisperx.diarize")

        class DiarizationPipeline:
            def __init__(self, model_name=None, token=None, device="cpu"):
                stack.log.append(("load_diarize", model_name, token))
                if stack.diarize_error:
                    raise stack.diarize_error

            def __call__(self, audio, **kw):
                stack.log.append(("diarize",))
                return "frame"

        diarize.DiarizationPipeline = DiarizationPipeline
        wx.diarize = diarize
        for name, mod in (("torch", torch), ("whisperx", wx), ("whisperx.diarize", diarize)):
            monkeypatch.setitem(sys.modules, name, mod)
        monkeypatch.setattr(wb, "prepare_environment", lambda asr, environ=None: {})

    def events(self, kind):
        return [e for e in self.log if e[0] == kind]


@pytest.fixture
def audio(tmp_path):
    f = tmp_path / "ep.mp3"
    f.write_bytes(b"x")
    return f


def make(tmp_cfg, token=None, **kw):
    cfg = tmp_cfg.model_copy(update={"secrets": Secrets(hf_token=token)})
    return wb.WhisperXTranscriber(cfg, check_access=kw.pop("check_access", lambda m, t: None), **kw)


def test_full_run_with_speakers(monkeypatch, tmp_cfg, audio):
    stack = FakeStack(monkeypatch)
    progress = []
    t = make(tmp_cfg, TOKEN).transcribe(audio, progress=lambda s, f: progress.append((s, f)))
    assert [e[0] for e in stack.log] == [
        "load_model", "asr", "load_align", "align", "load_diarize", "diarize", "assign",
    ]  # fmt: skip
    assert ("assign", True) in stack.log  # fill_nearest: every word gets a speaker
    assert t.diarization and t.num_speakers == 2 and t.diarization_skipped is None
    assert t.diarization_model == "pyannote/speaker-diarization-community-1"
    assert [w.speaker for w in t.words] == ["SPEAKER_00"] * 3 + ["SPEAKER_01"]
    assert (t.compute_type, t.batch_size, t.vad, t.language) == ("float16", 8, "pyannote", "en")
    assert t.asr_backend == "whisperx" and t.asr_model == "large-v3"
    assert t.audio_duration_s == 10.0 and t.runtime_s >= 0
    assert [s.name for s in t.stages] == ["decode", "asr", "align", "diarize"]
    assert all(s.peak_vram_mb is not None and s.peak_torch_mb == 3.0 for s in t.stages)
    assert {s for s, _ in progress} >= {"asr", "align", "diarize"}
    assert stack.cuda.cache_emptied >= 3  # freed after asr, align and diarize
    # "2024" had no alignment and was interpolated between "Hello" and "world."
    assert [w.w for w in t.words] == ["Hello", "2024", "world.", "Bye."]
    assert any("interpolated" in n for n in t.notes)
    assert all(a.end <= b.end for a, b in zip(t.words, t.words[1:], strict=False))
    # the token reached the diarization model but is nowhere in the saved metadata
    assert ("load_diarize", "pyannote/speaker-diarization-community-1", TOKEN) in stack.log
    assert TOKEN not in t.to_json()


def test_no_token_skips_speakers_cleanly(monkeypatch, tmp_cfg, audio, caplog):
    stack = FakeStack(monkeypatch)
    caplog.set_level("WARNING", logger="ciw.asr.whisperx")
    t = make(tmp_cfg, None).transcribe(audio)
    assert stack.events("load_diarize") == [] and stack.events("diarize") == []
    assert not t.diarization and t.diarization_skipped == SKIP_NO_TOKEN
    assert t.num_speakers is None and all(w.speaker is None for w in t.words)
    assert [s.name for s in t.stages][-1] == "diarize" and t.stages[-1].skipped == SKIP_NO_TOKEN
    assert any("HF_TOKEN" in r.message and ".env" in r.message for r in caplog.records)


def test_diarize_false_is_not_a_warning(monkeypatch, tmp_cfg, audio, caplog):
    FakeStack(monkeypatch)
    caplog.set_level("WARNING", logger="ciw.asr.whisperx")
    t = make(tmp_cfg, TOKEN, diarize=False).transcribe(audio)
    assert t.diarization_skipped == SKIP_DISABLED and not caplog.records


def test_gated_model_at_preflight_degrades_without_speakers(monkeypatch, tmp_cfg, audio):
    stack = FakeStack(monkeypatch)
    t = make(tmp_cfg, TOKEN, check_access=lambda m, tok: SKIP_GATED).transcribe(audio)
    assert t.diarization_skipped == SKIP_GATED and not t.diarization
    assert stack.events("load_diarize") == [] and t.words


def test_gate_error_while_loading_the_pipeline_keeps_the_transcript(monkeypatch, tmp_cfg, audio):
    class GatedRepoError(Exception):
        pass

    stack = FakeStack(
        monkeypatch,
        diarize_error=GatedRepoError(f"401 Client Error. Cannot access gated repo {TOKEN}"),
    )
    t = make(tmp_cfg, TOKEN).transcribe(audio)
    assert stack.events("load_diarize") and not stack.events("diarize")
    assert not t.diarization and t.diarization_skipped == SKIP_GATED
    assert all(w.speaker is None for w in t.words) and len(t.words) == 4
    assert t.stages[-1].name == "diarize" and t.stages[-1].skipped == SKIP_GATED
    assert TOKEN not in t.to_json()


def test_other_diarization_failure_is_recorded_and_the_token_is_redacted(
    monkeypatch, tmp_cfg, audio, caplog
):
    FakeStack(monkeypatch, diarize_error=ValueError(f"weird failure token={TOKEN} and {TOKEN}"))
    caplog.set_level("WARNING", logger="ciw.asr.whisperx")
    t = make(tmp_cfg, TOKEN).transcribe(audio)
    assert t.diarization_skipped.startswith(SKIP_ERROR) and not t.diarization
    assert TOKEN not in t.to_json()
    assert TOKEN not in caplog.text and "***" in t.diarization_skipped


def test_oom_backoff_inside_the_real_flow_records_what_was_used(monkeypatch, tmp_cfg, audio):
    stack = FakeStack(monkeypatch, oom_when=lambda ct, bs: ct == "float16" or bs > 4)
    t = make(tmp_cfg, None).transcribe(audio)
    asr_calls = [(e[1], e[2]) for e in stack.events("asr")]
    assert asr_calls == [
        ("float16", 8), ("float16", 4), ("float16", 2), ("int8_float16", 8), ("int8_float16", 4),
    ]  # fmt: skip
    assert [e[1] for e in stack.events("load_model")] == ["float16"] * 3 + ["int8_float16"] * 2
    assert (t.compute_type, t.batch_size) == ("int8_float16", 4)
    assert len([n for n in t.notes if "out of memory" in n]) == 4


def test_initial_prompt_reaches_the_model_and_is_recorded(monkeypatch, tmp_cfg, audio):
    stack = FakeStack(monkeypatch)
    t = make(tmp_cfg, None).transcribe(audio, initial_prompt="Searzall, kansui")
    assert stack.events("asr")[0][3] == "Searzall, kansui"
    assert t.initial_prompt == "Searzall, kansui" and t.vocab_hash == wb.prompt_hash(
        "Searzall, kansui"
    )


def test_vocabulary_file_from_config_is_used_when_no_prompt_is_passed(
    monkeypatch, tmp_cfg, tmp_path, audio
):
    vocab = tmp_path / "vocab.txt"
    vocab.write_text("Searzall\nkansui\n")
    cfg = tmp_cfg.model_copy(
        update={"asr": tmp_cfg.asr.model_copy(update={"vocabulary_file": vocab})}
    )
    stack = FakeStack(monkeypatch)
    wb.WhisperXTranscriber(cfg, diarize=False).transcribe(audio)
    assert stack.events("asr")[0][3] == "Searzall, kansui"
    stack2 = FakeStack(monkeypatch)
    make(tmp_cfg, None).transcribe(audio)  # no vocabulary_file configured
    assert stack2.events("asr")[0][3] is None


def test_missing_audio_and_missing_cuda_fail_loudly(monkeypatch, tmp_cfg, tmp_path, audio):
    stack = FakeStack(monkeypatch)
    with pytest.raises(FileNotFoundError):
        make(tmp_cfg).transcribe(tmp_path / "nope.mp3")
    stack.cuda.is_available = lambda: False
    with pytest.raises(RuntimeError, match="CUDA is not available"):
        make(tmp_cfg).transcribe(audio)


def test_exhausted_oom_ladder_propagates(monkeypatch, tmp_cfg, audio):
    stack = FakeStack(monkeypatch, oom_when=lambda ct, bs: True)
    with pytest.raises(wb.OutOfMemoryExhausted):
        make(tmp_cfg).transcribe(audio)
    assert stack.cuda.cache_emptied >= 1


# ------------------------------------------------------------------ HF error handling


@pytest.mark.parametrize(
    "exc, gated",
    [
        (Exception("401 Client Error: Unauthorized for url"), True),
        (Exception("Cannot access gated repo for url ... Access to model is restricted"), True),
        (type("GatedRepoError", (Exception,), {})("x"), True),
        (Exception("403 Forbidden"), True),
        (AttributeError("'NoneType' object has no attribute 'to'"), True),  # pyannote returns None
        (ConnectionError("Name or service not known"), False),
        (RuntimeError("CUDA out of memory"), False),
    ],
)
def test_classify_hf_error(exc, gated):
    assert (wb.classify_hf_error(exc) == SKIP_GATED) is gated


def test_classify_uses_a_response_status_code_when_present():
    err = Exception("boom")
    err.response = types.SimpleNamespace(status_code=403)
    assert wb.classify_hf_error(err) == SKIP_GATED
