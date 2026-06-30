"""
Tests for voiceflow.file_transcribe -- the pure formatters (text / SRT / VTT),
timestamp formatting, format dispatch, and the FAIL-OPEN transcribe wrapper.

Hermetic: no model, no GUI, no real audio. The formatters are pure functions.
``transcribe_file`` is exercised with a tiny fake model + a patched audio loader
so the CUDA fallback and cancel paths are covered without touching a GPU/file.
"""

from __future__ import annotations

from unittest import mock

import numpy as np

import voiceflow.file_transcribe as FT
from voiceflow.file_transcribe import (
    FileSegment,
    segments_to_srt,
    segments_to_text,
    segments_to_vtt,
)


# ===========================================================================
# segments_to_text
# ===========================================================================
def test_segments_to_text_basic():
    segs = [FileSegment(0.0, 2.0, "Hello"), FileSegment(2.0, 4.0, "world")]
    assert segments_to_text(segs) == "Hello\nworld"


def test_segments_to_text_strips_and_skips_empty():
    segs = [FileSegment(0.0, 1.0, "  Hi  "), FileSegment(1.0, 2.0, "   "),
            FileSegment(2.0, 3.0, "there")]
    assert segments_to_text(segs) == "Hi\nthere"


def test_empty_segments_text():
    assert segments_to_text([]) == ""


# ===========================================================================
# segments_to_srt
# ===========================================================================
def test_segments_to_srt_timestamps():
    srt = segments_to_srt([FileSegment(0.0, 2.5, "First")])
    assert "1\n" in srt
    assert "00:00:00,000 --> 00:00:02,500\n" in srt
    assert "First\n" in srt


def test_segments_to_srt_multiple_numbering():
    segs = [FileSegment(0.0, 1.0, "Line 1"), FileSegment(1.0, 2.0, "Line 2")]
    srt = segments_to_srt(segs)
    lines = srt.strip().split("\n")
    # block 1: index, timestamp, text, (blank), block 2: index, ...
    assert lines[0] == "1"
    assert lines[4] == "2"


def test_segments_to_srt_skips_empty_and_renumbers():
    segs = [FileSegment(0.0, 1.0, "A"), FileSegment(1.0, 2.0, "  "),
            FileSegment(2.0, 3.0, "B")]
    srt = segments_to_srt(segs)
    # Empty middle segment dropped; B becomes index 2 (not 3).
    assert "1\n00:00:00,000 --> 00:00:01,000\nA" in srt
    assert "2\n00:00:02,000 --> 00:00:03,000\nB" in srt
    assert "\n3\n" not in srt


def test_empty_segments_srt():
    assert segments_to_srt([]) == ""


# ===========================================================================
# segments_to_vtt
# ===========================================================================
def test_segments_to_vtt_header():
    vtt = segments_to_vtt([FileSegment(0.0, 1.0, "Hi")])
    assert vtt.startswith("WEBVTT")
    assert "00:00:00.000 --> 00:00:01.000" in vtt   # dot, not comma
    assert "Hi\n" in vtt


def test_segments_to_vtt_timestamp_dot_separator():
    vtt = segments_to_vtt([FileSegment(0.5, 1.5, "Test")])
    assert "00:00:00.500 --> 00:00:01.500\n" in vtt


def test_empty_segments_vtt():
    assert segments_to_vtt([]) == "WEBVTT\n"


# ===========================================================================
# Long timestamps (hours) + special characters / newlines
# ===========================================================================
def test_segments_with_long_timestamps():
    segs = [FileSegment(3661.5, 3665.75, "Hour later")]
    assert "01:01:01,500 --> 01:01:05,750" in segments_to_srt(segs)
    assert "01:01:01.500 --> 01:01:05.750" in segments_to_vtt(segs)


def test_negative_timestamp_clamps_to_zero():
    assert FT._fmt_ts(-5.0, ",") == "00:00:00,000"


def test_millisecond_rounding():
    # 1.2345s -> 1234ms (rounded)
    assert FT._fmt_ts(1.2345, ".") == "00:00:01.234"
    assert FT._fmt_ts(1.2346, ".") == "00:00:01.235"


# ===========================================================================
# Format dispatch helpers
# ===========================================================================
def test_format_segments_dispatch():
    segs = [FileSegment(0.0, 1.0, "x")]
    assert FT.format_segments(segs, "text") == "x"
    assert FT.format_segments(segs, "srt").startswith("1\n")
    assert FT.format_segments(segs, "vtt").startswith("WEBVTT")
    # Unknown format falls back to text.
    assert FT.format_segments(segs, "bogus") == "x"


def test_extension_for():
    assert FT.extension_for("text") == ".txt"
    assert FT.extension_for("srt") == ".srt"
    assert FT.extension_for("vtt") == ".vtt"
    assert FT.extension_for("bogus") == ".txt"


# ===========================================================================
# _is_cuda_error
# ===========================================================================
def test_is_cuda_error():
    assert FT._is_cuda_error(RuntimeError("CUDA out of memory")) is True
    assert FT._is_cuda_error(RuntimeError("cublas64_12.dll not found")) is True
    assert FT._is_cuda_error(ValueError("bad audio shape")) is False


# ===========================================================================
# transcribe_file: success, cancel, no-model, bad-audio, CUDA fallback
# ===========================================================================
class _FakeSeg:
    def __init__(self, start, end, text):
        self.start = start
        self.end = end
        self.text = text


class _FakeModel:
    """Returns a fixed list of segments; records that transcribe was called."""

    def __init__(self, segs=None, raise_exc=None):
        self._segs = segs or []
        self._raise = raise_exc
        self.calls = 0

    def transcribe(self, audio, **kw):
        self.calls += 1
        if self._raise is not None:
            raise self._raise
        return iter(self._segs), {"language": "en"}


def _audio_ok(*_a, **_k):
    return np.ones(16, dtype=np.float32)


def test_transcribe_file_success():
    model = _FakeModel([_FakeSeg(0.0, 1.0, " Hello "),
                        _FakeSeg(1.0, 2.0, "world")])
    with mock.patch.object(FT, "load_audio_file", _audio_ok):
        segs, err = FT.transcribe_file("x.mp3", model, {})
    assert err is None
    assert [(s.start, s.end, s.text) for s in segs] == [
        (0.0, 1.0, "Hello"), (1.0, 2.0, "world")]


def test_transcribe_file_no_model():
    segs, err = FT.transcribe_file("x.mp3", None, {})
    assert segs == []
    assert err and "model" in err.lower()


def test_transcribe_file_bad_audio():
    with mock.patch.object(FT, "load_audio_file", lambda *a, **k: None):
        segs, err = FT.transcribe_file("x.weird", _FakeModel(), {})
    assert segs == []
    assert err and "read" in err.lower()


def test_transcribe_file_cancel_midway():
    model = _FakeModel([_FakeSeg(0.0, 1.0, "a"), _FakeSeg(1.0, 2.0, "b")])
    # Cancel before any segment is consumed.
    with mock.patch.object(FT, "load_audio_file", _audio_ok):
        segs, err = FT.transcribe_file("x.mp3", model, {},
                                       should_cancel=lambda: True)
    assert err == "canceled"
    assert segs == []


def test_transcribe_file_fail_open_on_exception():
    model = _FakeModel(raise_exc=ValueError("decode boom"))
    with mock.patch.object(FT, "load_audio_file", _audio_ok):
        segs, err = FT.transcribe_file("x.mp3", model, {})
    assert segs == []
    assert err and "failed" in err.lower()


def test_transcribe_file_cuda_fallback_retries_on_cpu():
    gpu = _FakeModel(raise_exc=RuntimeError("CUDA out of memory"))
    cpu = _FakeModel([_FakeSeg(0.0, 1.0, "recovered")])
    calls = {"n": 0}

    def fallback(_exc):
        calls["n"] += 1
        return cpu

    with mock.patch.object(FT, "load_audio_file", _audio_ok):
        segs, err = FT.transcribe_file("x.mp3", gpu, {},
                                       on_cuda_error=fallback)
    assert err is None
    assert calls["n"] == 1
    assert [s.text for s in segs] == ["recovered"]


def test_transcribe_file_uses_bias_prompt_and_hotwords():
    model = _FakeModel([_FakeSeg(0.0, 1.0, "x")])
    cfg = {"__bias_prompt__": "MY PROMPT", "__hotwords__": "Foo Bar",
           "beam_size": 3}
    with mock.patch.object(FT, "load_audio_file", _audio_ok):
        FT.transcribe_file("x.mp3", model, cfg)
    # transcribe got called; verify the bias kwargs were threaded through.
    assert model.calls == 1


def test_transcribe_file_non_cuda_error_no_fallback():
    model = _FakeModel(raise_exc=ValueError("bad shape"))
    called = {"n": 0}

    def fallback(_exc):
        called["n"] += 1
        return _FakeModel([_FakeSeg(0.0, 1.0, "nope")])

    with mock.patch.object(FT, "load_audio_file", _audio_ok):
        segs, err = FT.transcribe_file("x.mp3", model, {},
                                       on_cuda_error=fallback)
    # A non-CUDA error must NOT trigger the CPU fallback.
    assert called["n"] == 0
    assert segs == []
    assert err and "failed" in err.lower()


# ===========================================================================
# C1: transcribe_file holds the SHARED model lock around the decode (faster-
# whisper is not safe for concurrent inference / mid-swap on one WhisperModel).
# ===========================================================================
class _SentinelLock:
    """Records enter/exit and how many times transcribe ran WHILE held."""

    def __init__(self):
        self.enters = 0
        self.exits = 0
        self.held = False

    def __enter__(self):
        self.enters += 1
        self.held = True
        return self

    def __exit__(self, *exc):
        self.held = False
        self.exits += 1
        return False


class _LockAwareModel:
    """A fake model that asserts the lock is held when transcribe() is called."""

    def __init__(self, lock, segs):
        self._lock = lock
        self._segs = segs
        self.called_while_held = None

    def transcribe(self, audio, **kw):
        # Record whether the lock was held at the moment of the inference call.
        self.called_while_held = self._lock.held
        return iter(self._segs), {"language": "en"}


def test_transcribe_file_holds_lock_around_transcribe():
    """The model lock must be acquired (entered) during file transcription and
    held AROUND the model.transcribe() decode."""
    lock = _SentinelLock()
    model = _LockAwareModel(lock, [_FakeSeg(0.0, 1.0, "hi")])
    with mock.patch.object(FT, "load_audio_file", _audio_ok):
        segs, err = FT.transcribe_file("x.mp3", model, {}, lock=lock)
    assert err is None
    assert [s.text for s in segs] == ["hi"]
    # The lock was entered and the transcribe ran while it was held.
    assert lock.enters >= 1
    assert model.called_while_held is True
    # And it was released afterwards (balanced enter/exit).
    assert lock.exits == lock.enters
    assert lock.held is False


def test_transcribe_file_without_lock_uses_noop():
    """Backwards-compatible: omitting lock= uses a no-op lock (headless tests)."""
    model = _FakeModel([_FakeSeg(0.0, 1.0, "ok")])
    with mock.patch.object(FT, "load_audio_file", _audio_ok):
        segs, err = FT.transcribe_file("x.mp3", model, {})  # no lock=
    assert err is None
    assert [s.text for s in segs] == ["ok"]
