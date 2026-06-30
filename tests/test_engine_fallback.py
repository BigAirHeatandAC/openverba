"""
Tests for the engine's CPU-fallback ladder (``voiceflow.engine``).

The plan's safety net (section 3.1) is "never hard-fail for lack of GPU":
CUDA float16 -> CUDA int8_float16 -> CPU int8. The engine implements this in
``load_model`` (load-time ladder, with a smaller-GPU-model VRAM-relief attempt)
and ``_transcribe_with_fallback`` (inference-time reload-on-CPU). Both are tested
here with the CTranslate2/faster-whisper layer fully mocked, so no GPU, no model
download, and no real transcription happen.

Mocking seams:
  * ``voiceflow.engine.WhisperModel`` -- the constructor used by load_model.
  * ``DictationEngine._warmup`` -- patched to a no-op (a real warmup would run a
    transcription on a freshly built (mock) model).
  * the platform factory (clipboard + triggers) via the ``make_engine`` fixture,
    so constructing the engine installs no hook and touches no clipboard.
"""

from __future__ import annotations

from unittest import mock

import numpy as np
import pytest

import voiceflow.engine as engine_mod


@pytest.fixture(autouse=True)
def _noop_warmup():
    """A built model is a MagicMock; the real _warmup would call .transcribe on
    it. Make warmup inert for every test in this module."""
    with mock.patch.object(engine_mod.DictationEngine, "_warmup",
                           lambda self, m: None):
        yield


def _patch_whisper(side_effect=None, return_value=None):
    """Patch the WhisperModel symbol the engine imported. Returns the mock."""
    kw = {}
    if side_effect is not None:
        kw["side_effect"] = side_effect
    else:
        kw["return_value"] = return_value if return_value is not None \
            else mock.MagicMock(name="WhisperModel-instance")
    return mock.patch.object(engine_mod, "WhisperModel", **kw)


# ---------------------------------------------------------------------------
# load_model(): device='cpu' goes straight to CPU int8, never touches CUDA
# ---------------------------------------------------------------------------
def test_load_model_cpu_only(make_engine):
    eng, _clip, _trig = make_engine({"device": "cpu", "model": "small.en"})
    with _patch_whisper() as WM:
        info = eng.load_model()
    assert eng.device == "cpu"
    assert info["device"] == "cpu"
    assert info["loaded"] is True
    # exactly one construction, on CPU with the configured cpu_compute_type
    assert WM.call_count == 1
    _, kwargs = WM.call_args
    assert kwargs["device"] == "cpu"
    assert kwargs["compute_type"] == "int8"     # DEFAULT_CONFIG cpu_compute_type


# ---------------------------------------------------------------------------
# load_model(): device='auto', GPU works -> stays on CUDA, no CPU attempt
# ---------------------------------------------------------------------------
def test_load_model_auto_gpu_success(make_engine):
    eng, _clip, _trig = make_engine({"device": "auto", "model": "small.en"})
    with _patch_whisper() as WM, \
         mock.patch.object(engine_mod.cuda, "supported_cuda_compute",
                           return_value="float16"):
        info = eng.load_model()
    assert eng.device == "cuda"
    assert info["device"] == "gpu"
    assert eng.compute_type == "float16"
    # only the first (configured-model) GPU attempt; no smaller-model, no CPU
    assert WM.call_count == 1
    _, kwargs = WM.call_args
    assert kwargs["device"] == "cuda"
    assert kwargs["compute_type"] == "float16"


# ---------------------------------------------------------------------------
# load_model(): the full ladder -- GPU(model) fail -> GPU(base.en) fail -> CPU
# ---------------------------------------------------------------------------
def test_load_model_full_fallback_ladder(make_engine):
    eng, _clip, _trig = make_engine({"device": "auto", "model": "small.en"})

    def side(name, device=None, compute_type=None, **kw):
        if device == "cuda":
            raise RuntimeError("CUDA failed: cublas64_12.dll not found")
        return mock.MagicMock(name="cpu-model")

    with _patch_whisper(side_effect=side) as WM, \
         mock.patch.object(engine_mod.cuda, "supported_cuda_compute",
                           return_value="int8_float16"):
        info = eng.load_model()

    assert eng.device == "cpu"
    assert info["device"] == "cpu"
    # 3 attempts in order: small.en@cuda, base.en@cuda (VRAM relief), small.en@cpu
    attempts = [(c.args[0], c.kwargs["device"], c.kwargs["compute_type"])
                for c in WM.call_args_list]
    assert attempts == [
        ("small.en", "cuda", "int8_float16"),
        ("base.en", "cuda", "int8_float16"),
        ("small.en", "cpu", "int8"),
    ]


def test_load_model_small_gpu_model_succeeds_after_big_fails(make_engine):
    """If the configured model OOMs on the GPU but the smaller base.en fits, we
    stay on CUDA with base.en (the VRAM-relief rung), not CPU."""
    eng, _clip, _trig = make_engine({"device": "auto", "model": "small.en"})

    def side(name, device=None, compute_type=None, **kw):
        if device == "cuda" and name == "small.en":
            raise RuntimeError("CUDA out of memory")
        if device == "cuda" and name == "base.en":
            return mock.MagicMock(name="gpu-base")
        return mock.MagicMock(name="cpu-model")

    with _patch_whisper(side_effect=side) as WM, \
         mock.patch.object(engine_mod.cuda, "supported_cuda_compute",
                           return_value="int8_float16"):
        info = eng.load_model()

    assert eng.device == "cuda"
    assert eng.model_name == "base.en"
    assert info["device"] == "gpu"
    # never reached the CPU rung
    assert all(c.kwargs["device"] == "cuda" for c in WM.call_args_list)


def test_load_model_no_smaller_gpu_attempt_when_already_base(make_engine):
    """When the configured model is already base.en/tiny.en there is no smaller
    GPU rung: GPU(base.en) fail -> straight to CPU."""
    eng, _clip, _trig = make_engine({"device": "auto", "model": "base.en"})

    def side(name, device=None, compute_type=None, **kw):
        if device == "cuda":
            raise RuntimeError("no CUDA device")
        return mock.MagicMock(name="cpu-model")

    with _patch_whisper(side_effect=side) as WM, \
         mock.patch.object(engine_mod.cuda, "supported_cuda_compute",
                           return_value="int8_float16"):
        eng.load_model()

    attempts = [(c.args[0], c.kwargs["device"]) for c in WM.call_args_list]
    assert attempts == [("base.en", "cuda"), ("base.en", "cpu")]


def test_load_model_device_cuda_requested_still_falls_back(make_engine):
    """Even if the user explicitly requests device='cuda', a broken CUDA stack
    must not hard-fail: it falls back to CPU (never hard-fail)."""
    eng, _clip, _trig = make_engine({"device": "cuda", "model": "small.en"})

    def side(name, device=None, compute_type=None, **kw):
        if device == "cuda":
            raise RuntimeError("CUDA driver version is insufficient")
        return mock.MagicMock(name="cpu-model")

    with _patch_whisper(side_effect=side), \
         mock.patch.object(engine_mod.cuda, "supported_cuda_compute",
                           return_value="float16"):
        info = eng.load_model()
    assert eng.device == "cpu"
    assert info["loaded"] is True


# ---------------------------------------------------------------------------
# _transcribe_with_fallback(): inference-time CUDA error -> reload CPU + retry
# ---------------------------------------------------------------------------
def test_transcribe_fallback_reloads_cpu_on_cuda_error(make_engine):
    eng, _clip, _trig = make_engine({"device": "auto"})
    eng.device = "cuda"            # pretend a GPU model is loaded

    calls = {"n": 0}

    def transcribe_side(audio, override_prompt=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("CUDA out of memory")   # CUDA-class error
        return "hello world"

    with mock.patch.object(eng, "_transcribe", side_effect=transcribe_side), \
         mock.patch.object(eng, "_load_cpu_model",
                           return_value=mock.MagicMock(name="cpu-reload")):
        out = eng._transcribe_with_fallback(np.zeros(16, dtype=np.float32))

    assert out == "hello world"
    assert eng.device == "cpu"     # stuck to CPU for the rest of the session
    assert calls["n"] == 2         # retried once after the reload


def test_transcribe_fallback_non_cuda_error_propagates(make_engine):
    """A non-CUDA error is NOT swallowed (we don't pointlessly reload on CPU)."""
    eng, _clip, _trig = make_engine({"device": "auto"})
    eng.device = "cuda"

    with mock.patch.object(eng, "_transcribe",
                           side_effect=ValueError("bad audio shape")), \
         mock.patch.object(eng, "_load_cpu_model") as reload_cpu:
        with pytest.raises(ValueError):
            eng._transcribe_with_fallback(np.zeros(16, dtype=np.float32))
    reload_cpu.assert_not_called()


def test_transcribe_fallback_cpu_error_not_reloaded(make_engine):
    """On CPU already, a CUDA-looking error must just propagate (no reload)."""
    eng, _clip, _trig = make_engine({"device": "cpu"})
    eng.device = "cpu"

    with mock.patch.object(eng, "_transcribe",
                           side_effect=RuntimeError("cuda mention but on cpu")), \
         mock.patch.object(eng, "_load_cpu_model") as reload_cpu:
        with pytest.raises(RuntimeError):
            eng._transcribe_with_fallback(np.zeros(16, dtype=np.float32))
    reload_cpu.assert_not_called()


# ---------------------------------------------------------------------------
# _is_cuda_error(): the classifier that gates the reload
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("msg", [
    "cublas64_12.dll not found",
    "Could not locate cudnn_ops64_9.dll",
    "CUDA out of memory",
    "no GPU device available",
])
def test_is_cuda_error_true(msg):
    assert engine_mod._is_cuda_error(RuntimeError(msg)) is True


@pytest.mark.parametrize("msg", [
    "bad audio shape",
    "tokenizer.json missing",
    "",
])
def test_is_cuda_error_false(msg):
    assert engine_mod._is_cuda_error(ValueError(msg)) is False


# ---------------------------------------------------------------------------
# current_model_info(): reflects loaded state + reports missing CUDA pkgs
# ---------------------------------------------------------------------------
def test_current_model_info_before_load(make_engine):
    eng, _clip, _trig = make_engine({"model": "small.en"})
    info = eng.current_model_info()
    assert info["loaded"] is False
    assert info["model"] == "small.en"
    assert info["device"] is None
    assert isinstance(info["cuda_missing"], list)


def test_start_requires_load_model_first(make_engine):
    eng, _clip, _trig = make_engine()
    with pytest.raises(RuntimeError):
        eng.start()
