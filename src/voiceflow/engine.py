"""
voiceflow.engine - the DictationEngine: state machine + mic capture + worker
threads + VAD + hallucination filter + clipboard paste.

Ported faithfully from main.py's DictationApp/load_model/transcribe pipeline,
refactored into a reusable class with GUI callbacks.

ORDERING (critical): this module registers the CUDA DLL dirs (cuda.register_cuda_dlls)
BEFORE importing faster_whisper, or CTranslate2 dies with
"cublas64_12.dll not found". Do not move the faster_whisper import above it.

PLATFORM ABSTRACTION: the engine obtains its clipboard/paste and trigger
registration via the platform factory (voiceflow.platform) instead of importing
the OS modules directly. On Windows the factory returns the verified
ClipboardManager (the verbatim paste cycle) and the WindowsTriggers backend
(keyboard combos AND mouse buttons AND the left+right chord behind one stable
register()), so Windows behaviour is byte-for-byte preserved.

State machine: IDLE -> RECORDING -> TRANSCRIBING -> IDLE. The trigger callback
runs on a low-level hook listener thread and MUST stay microscopic (flip state,
enqueue a command, return) -- if it blocks past ~300ms Windows silently removes
the hook and the trigger dies with no error. All heavy work (stream teardown,
np.concatenate, transcription, paste) happens on the control/worker threads.
"""

import os
import re
import time
import queue
import logging
import threading
import traceback

import numpy as np
import sounddevice as sd

from . import cuda
from . import config as _config
from .constants import (
    IDLE, RECORDING, TRANSCRIBING, CMD_STOP, CMD_STOP_STREAM, CMD_STOP_COMMAND,
    CMD_STOP_PREVIEW, STATE_LABELS,
)
from . import platform as _platform
from . import streaming as _streaming
from . import transcribe as _transcribe
from . import commands as _commands
from . import ai as _ai
from . import debuglog as _debuglog
from . import history as _history
from . import learn as _learn
from . import snippets as _snippets
from . import modes as _modes

# ---- CUDA DLLs MUST be registered before faster_whisper imports. ----
_CUDA_DLL_DIRS, _CUDA_MISSING = cuda.register_cuda_dlls()

from faster_whisper import WhisperModel  # noqa: E402  (after DLL registration)

try:
    import winsound
    _HAVE_WINSOUND = True
except Exception:
    _HAVE_WINSOUND = False

log = logging.getLogger("voiceflow.engine")


# ---------------------------------------------------------------------------
# Transcript cleanup + hallucination filter (ported verbatim).
# ---------------------------------------------------------------------------
HALLUCINATION_BLOCKLIST = {
    "thank you", "thank you.", "thanks for watching", "thanks for watching!",
    "thank you for watching", "thank you for watching!",
    "please subscribe", "like and subscribe", ".", ". .", "...",
}
HALLUCINATION_STRONG_SUBSTR = (
    "subtitles by", "amara.org", "transcription by",
)
HALLUCINATION_WEAK_SUBSTR = (
    "subscribe to", "thanks for watching", "thank you for watching",
)
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean_transcript(text, cfg):
    """Returns (clean_text, was_filtered). was_filtered=True means a real
    transcript was intentionally dropped by the hallucination filter."""
    if cfg.get("strip_whitespace", True):
        text = text.strip()
    if not text:
        return "", False

    # Sanitize newlines/control chars: a stray newline in a chat/terminal/
    # single-line field acts as Enter and can submit in the focused window.
    if not cfg.get("allow_multiline", False):
        text = re.sub(r"[\r\n]+", " ", text)
    text = _CONTROL_CHARS_RE.sub("", text)
    text = re.sub(r"[ \t]{2,}", " ", text).strip()
    if not text:
        return "", False

    if cfg.get("filter_hallucinations", True):
        norm = re.sub(r"[^\w\s]", "", text).strip().lower()
        if norm in HALLUCINATION_BLOCKLIST:
            return "", True
        low = text.lower()
        if any(s in low for s in HALLUCINATION_STRONG_SUBSTR):
            return "", True
        compact = re.sub(r"[^\w]", "", low)
        for s in HALLUCINATION_WEAK_SUBSTR:
            scompact = re.sub(r"[^\w]", "", s)
            if scompact and compact and scompact in compact and \
                    len(scompact) >= 0.6 * len(compact):
                return "", True

    if text and cfg.get("add_trailing_space", True):
        text = text + " "
    return text, False


def _is_cuda_error(exc):
    s = (str(exc) or "").lower()
    return any(k in s for k in
               ("cuda", "cublas", "cudnn", "out of memory", "gpu", "device"))


def _is_mouse_chord(trigger):
    t = (trigger or "").replace(" ", "").lower()
    return t.startswith("mouse:") and "+" in t


# ---------------------------------------------------------------------------
# Audio feedback (beeps). Always fired off a throwaway thread (Beep blocks).
# ---------------------------------------------------------------------------
class Beeper:
    """Soft, pleasant UI sounds: smooth sine chimes (not harsh square-wave
    beeps), synthesized once and played async from an in-memory WAV via winsound.
    Falls back to winsound.Beep, then silence."""
    _SR = 44100

    def __init__(self, enabled):
        self.enabled = bool(enabled) and _HAVE_WINSOUND
        self._cache = {}

    def _wav(self, segments):
        """segments: list of (freq|[freqs], seconds, volume) played in sequence;
        each note is a sine (or summed-sine chord) with a smooth attack/decay."""
        parts = []
        for freqs, dur, vol in segments:
            n = max(4, int(self._SR * dur))
            t = np.arange(n) / self._SR
            if isinstance(freqs, (int, float)):
                freqs = [freqs]
            w = sum(np.sin(2 * np.pi * f * t) for f in freqs) / len(freqs)
            a = min(max(1, int(self._SR * 0.006)), n // 2)   # ~6ms attack
            d = min(max(1, int(self._SR * 0.10)), n - a)      # up to 100ms decay
            env = np.ones(n)
            env[:a] = np.linspace(0.0, 1.0, a)
            env[n - d:] = np.linspace(1.0, 0.0, d)
            parts.append(w * env * vol)
        sig = np.concatenate(parts) if parts else np.zeros(1)
        pcm = (np.clip(sig, -1.0, 1.0) * 32767).astype("<i2").tobytes()
        import io
        import wave
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(self._SR)
            wf.writeframes(pcm)
        return buf.getvalue()

    def _play(self, name, segments, fallback_freq):
        if not self.enabled:
            return

        def go():
            try:
                data = self._cache.get(name)
                if data is None:
                    data = self._wav(segments)
                    self._cache[name] = data
                winsound.PlaySound(data, winsound.SND_MEMORY | winsound.SND_ASYNC)
            except Exception:
                try:
                    winsound.Beep(fallback_freq, 110)
                except Exception:
                    pass

        threading.Thread(target=go, daemon=True).start()

    def start(self):      # gentle rising "di-dit"
        self._play("start", [(587.33, 0.06, 0.22), (880.0, 0.075, 0.22)], 880)

    def stop(self):       # gentle falling "dit-du"
        self._play("stop", [(880.0, 0.06, 0.20), (587.33, 0.085, 0.20)], 660)

    def done(self):       # soft bell chord (success)
        self._play("done", [([784.0, 1046.5], 0.20, 0.26)], 1175)

    def error(self):      # soft low double-tone (not a harsh buzz)
        self._play("error", [(311.13, 0.10, 0.20), (233.08, 0.13, 0.20)], 300)

    def filtered(self):   # neutral soft blip
        self._play("filtered", [(659.25, 0.09, 0.18)], 520)


# ---------------------------------------------------------------------------
# DictationEngine
# ---------------------------------------------------------------------------
class DictationEngine:
    """The reusable dictation runtime.

    Construct with a config dict (from voiceflow.config.load_config) and optional
    callbacks. Then: load_model() -> start(). The trigger toggles recording.

    Callbacks (all optional, may be invoked from background threads -- a GUI must
    marshal them onto its UI thread, e.g. customtkinter .after):
      on_state(state_label: str)         "idle" | "recording" | "transcribing"
      on_transcript(text: str)           the last pasted transcript (no trailer)
      on_log(message: str)               human-readable status/log line
      on_level(rms: float)               mic VU level 0..1 (only while recording)
    """

    def __init__(self, config, on_state=None, on_transcript=None,
                 on_log=None, on_level=None,
                 on_preview_show=None, on_preview_text=None,
                 on_preview_hide=None):
        self.cfg = config
        self.on_state = on_state
        self.on_transcript = on_transcript
        self.on_log = on_log
        self.on_level = on_level
        # Live-preview ("preview" mode) overlay callbacks. Fire from engine
        # background threads; a GUI marshals them onto its UI thread.
        self.on_preview_show = on_preview_show
        self.on_preview_text = on_preview_text
        self.on_preview_hide = on_preview_hide

        self.sr = int(config.get("sample_rate", 16000))
        self.beeper = Beeper(config.get("beep", True))
        # Clipboard + paste come from the platform factory. On Windows this is
        # the verified ClipboardManager (verbatim all-format save/restore +
        # SendInput Ctrl+V paste cycle).
        self.clip = _platform.make_clipboard(
            config.get("clipboard_restore_delay_ms", 200),
            config.get("clipboard_read_timeout_ms", 2500))
        # The single stable trigger API (keyboard combos AND mouse buttons AND
        # the left+right chord behind one register()).
        self._triggers = _platform.make_trigger_backend()

        self.state = IDLE
        self.state_lock = threading.Lock()
        self._stop_in_progress = False

        self._frames = []
        self._frames_lock = threading.Lock()
        self._stream = None
        self._stream_lock = threading.Lock()
        self._auto_stop_timer = None
        self._max_frames = 0

        self.ctrl_q = queue.Queue()
        self.work_q = queue.Queue()

        self.model = None
        self.device = None          # "cuda" | "cpu"
        self.model_name = None      # the underlying repo/model name actually loaded
        self.compute_type = None
        # Serializes ALL access to self.model (inference + reassignment).
        # faster-whisper / CTranslate2 is NOT safe for concurrent inference on a
        # single WhisperModel instance, and the model may be swapped on a CUDA->
        # CPU fallback. File transcription runs model.transcribe() from a daemon
        # thread while the live dictation trigger stays armed, so every
        # transcribe() call and every model reassignment must hold this lock to
        # prevent a use-after-swap / concurrent-inference crash. Created BEFORE
        # any model is loaded or used.
        self._model_lock = threading.Lock()

        self._trigger = config.get("trigger", "ctrl+shift+space")
        self._command_trigger = config.get("command_trigger", "") or ""
        self._trigger_handles = []
        self._active = None          # "dictation" | "command" | None

        # Streaming ("words as you speak") mode. Lazily-created Typer + session.
        self.mode = config.get("mode", "batch")
        self._typer = None
        self._stream_session = None
        self._stream_orig_clip = None   # clipboard saved during a paste-mode stream
        # Live-preview ("preview" mode): when the preview session can't start we
        # fall back to a plain batch recording; this flag tells _stop_preview to
        # use the batch teardown (_teardown_stream_and_get_audio) in that case.
        self._preview_fellback = False

        self._threads_started = False
        self._running = False
        self.last_transcript = ""

        # ---- learned recognition data (vocabulary biasing + corrections) ----
        self._corrections = []      # list[rule] applied before paste (batch)
        self._hotwords = ""         # faster-whisper hotwords string (TIER 0)
        self._bias_prompt = config.get("initial_prompt")  # prompt + vocab list
        self._reload_learned()

        # ---- user text-expansion snippets (TIER 1.5; batch only) ----
        self._snippets = []         # list[dict] of enabled snippets
        self._reload_snippets()

        # ---- per-app context modes (contextual cleanup/biasing) ----
        self._modes_path = _modes.modes_path()
        self._modes = []            # list[dict] of modes (loaded lazily/on demand)
        self._reload_modes()

    # ---- learned data (personal vocabulary + correction map) ----
    def _reload_learned(self):
        """(Re)load corrections.json + personal_vocab.json into the engine so the
        next utterance uses them. Called at construct and after each learn event.
        Best-effort: never raises."""
        try:
            base_prompt = self.cfg.get("initial_prompt")
            if self.cfg.get("personal_vocab_enabled", True):
                vocab = _learn.load_vocab()
                self._hotwords = _learn.build_hotwords(vocab)
                self._bias_prompt = _learn.augmented_prompt(base_prompt, vocab)
            else:
                self._hotwords = ""
                self._bias_prompt = base_prompt
            if self.cfg.get("corrections_enabled", True):
                self._corrections = _learn.load_corrections()
            else:
                self._corrections = []
            # Expose to the streaming fallback path (StreamingSession reads cfg).
            self.cfg["__hotwords__"] = self._hotwords or ""
            self.cfg["__bias_prompt__"] = self._bias_prompt or ""
        except Exception:
            self._hotwords = ""
            self._bias_prompt = self.cfg.get("initial_prompt")
            self._corrections = []

    # ---- user text-expansion snippets (TIER 1.5; batch only) ----
    def _reload_snippets(self):
        """(Re)load snippets.json into the engine. Best-effort: never raises."""
        try:
            if self.cfg.get("snippets_enabled", True):
                self._snippets = _snippets.load_snippets()
            else:
                self._snippets = []
        except Exception:
            self._snippets = []

    def reload_snippets(self):
        """Public hook: reload snippets.json (called from the UI after the user
        edits or toggles snippets) so the next utterance uses them. Never raises."""
        self._reload_snippets()

    # ---- per-app context modes (contextual cleanup/biasing) ----
    def _reload_modes(self):
        """(Re)load modes.json into the engine. Called at construct and after
        each mode edit. Best-effort: never raises."""
        try:
            # seed=False: never write modes.json just by starting the engine
            # (the file is created on first use from the Settings dialog).
            self._modes = _modes.load_modes(self._modes_path, seed=False)
        except Exception:
            log.debug("_reload_modes failed", exc_info=True)
            self._modes = []

    def reload_modes(self):
        """Public hook: reload modes.json (called from the UI after the user
        edits or toggles modes) so the next utterance uses them immediately.
        Never raises."""
        self._reload_modes()
        self._log("Modes reloaded.")

    def _get_current_mode_prompt(self):
        """Resolve the active per-app mode and return its biasing prompt, or
        None if per_app_modes is off or resolution fails. Used to override the
        Whisper initial_prompt for the current utterance. Never raises -> on any
        failure we return None and the caller falls back to the static prompt."""
        if not self.cfg.get("per_app_modes", False):
            return None
        try:
            exe = _modes.get_foreground_exe()
            mode = _modes.resolve_mode(exe, self._modes)
            prompt = mode.get("prompt") if isinstance(mode, dict) else None
            return prompt or None
        except Exception:
            log.debug("_get_current_mode_prompt failed", exc_info=True)
            return None

    def _record_history(self, text, mode):
        """Append a committed transcript to local history (gated by the
        transcript_history flag). Best-effort: never raises into the engine."""
        try:
            if not text or not self.cfg.get("transcript_history", True):
                return
            _history.append(text, mode)
        except Exception:
            pass

    # ---- small helpers ----
    def _log(self, msg, *args):
        text = msg % args if args else msg
        log.info(text)
        if self.on_log:
            try:
                self.on_log(text)
            except Exception:
                pass

    def _emit_state(self, state):
        self.state = state
        if self.on_state:
            try:
                self.on_state(STATE_LABELS.get(state, state.lower()))
            except Exception:
                pass

    # ---- live-preview overlay emit helpers (best-effort, fail-open) ----
    def _preview_show(self):
        cb = self.on_preview_show
        if cb:
            try:
                cb()
            except Exception:
                pass

    def _preview_text(self, t):
        cb = self.on_preview_text
        if cb:
            try:
                cb(t)
            except Exception:
                pass

    def _preview_hide(self):
        cb = self.on_preview_hide
        if cb:
            try:
                cb()
            except Exception:
                pass

    # ---- model load (GPU -> CPU fallback) + warmup ----
    def _download_root(self):
        return _config.resolve_download_root(self.cfg)

    def _warmup(self, model):
        """First real transcription is slow (CUDA/cuDNN JIT + load). Run a short
        silent buffer so the first dictation isn't sluggish, and surface a broken
        CUDA build at startup (so CPU fallback kicks in) rather than later."""
        silent = np.zeros(int(self.sr), dtype=np.float32)
        kwargs = _transcribe.transcribe_kwargs(self.cfg)
        # Graceful degradation: translate-to-English needs a multilingual model;
        # on an English-only model fall back to plain transcribe so warmup (and
        # every later transcription) can't fail on an incompatible task.
        ok, warn = _transcribe.validate_task_for_model(
            self.cfg.get("model"), kwargs.get("task"))
        if not ok:
            self._log("Translate-to-English disabled: %s", warn)
            kwargs["task"] = "transcribe"
        # Warmup never auto-detects on silence (it'd hang); force a concrete
        # language so the JIT path is exercised quickly.
        if kwargs.get("language") is None:
            kwargs["language"] = "en"
        # Serialize against any concurrent inference / model swap (C1).
        with self._model_lock:
            segs, _ = model.transcribe(silent, beam_size=1, vad_filter=False,
                                       **kwargs)
            for _ in segs:
                pass
        self._log("Warmup transcription complete.")

    def load_model(self, progress_cb=None):
        """Load the configured model with a GPU->CPU fallback + warmup. Returns
        current_model_info(). progress_cb(text) is an optional status sink (e.g.
        for a GUI download/loading line)."""
        def _p(msg):
            self._log(msg)
            if progress_cb:
                try:
                    progress_cb(msg)
                except Exception:
                    pass

        name = self.cfg["model"]
        download_root = self._download_root()
        local_only = bool(self.cfg.get("local_files_only", False))

        if _CUDA_MISSING:
            _p("NVIDIA runtime missing (%s); GPU unavailable -> CPU only."
               % ", ".join(_CUDA_MISSING))

        if self.cfg.get("device") in ("auto", "cuda"):
            gpu_compute = cuda.supported_cuda_compute(self.cfg["compute_type"])
            # Try the configured GPU model, then a smaller GPU model (VRAM relief
            # on a 4GB card), each on the supported compute type.
            gpu_attempts = [(name, gpu_compute)]
            if name not in ("base", "base.en", "tiny", "tiny.en"):
                gpu_attempts.append(("base.en", gpu_compute))
            for mname, compute in gpu_attempts:
                m = None
                try:
                    _p("Loading '%s' on GPU (%s)..." % (mname, compute))
                    m = WhisperModel(mname, device="cuda", compute_type=compute,
                                     download_root=download_root,
                                     local_files_only=local_only)
                    self._warmup(m)
                    # Publish the model under the lock so a swap can't race an
                    # in-flight inference (C1).
                    with self._model_lock:
                        self.model, self.device = m, "cuda"
                        self.model_name, self.compute_type = mname, compute
                    self._log("Loaded '%s' on CUDA (%s).", mname, compute)
                    return self.current_model_info()
                except Exception as exc:
                    self._log("CUDA load failed for '%s' (%s): %s",
                              mname, compute, exc)
                    del m
                    try:
                        import gc
                        gc.collect()
                    except Exception:
                        pass
            self._log("GPU unavailable; falling back to CPU (slower).")
            if self.cfg.get("device") == "cuda":
                self._log("device='cuda' was requested but failed; using CPU.")

        # CPU fallback always works (fully local, slower).
        compute = self.cfg["cpu_compute_type"]
        _p("Loading '%s' on CPU (%s)..." % (name, compute))
        m = WhisperModel(name, device="cpu", compute_type=compute,
                         download_root=download_root, local_files_only=local_only)
        self._warmup(m)
        # Publish under the lock so a swap can't race an in-flight inference (C1).
        with self._model_lock:
            self.model, self.device = m, "cpu"
            self.model_name, self.compute_type = name, compute
        self._log("Loaded '%s' on CPU (%s).", name, compute)
        return self.current_model_info()

    def _load_cpu_model(self):
        """Load the model on CPU only (inference-time fallback)."""
        compute = self.cfg["cpu_compute_type"]
        m = WhisperModel(self.cfg["model"], device="cpu", compute_type=compute,
                         download_root=self._download_root(),
                         local_files_only=bool(self.cfg.get("local_files_only", False)))
        self._warmup(m)
        self._log("Loaded '%s' on CPU (%s) [fallback].", self.cfg["model"], compute)
        return m

    def current_model_info(self):
        """Return {"model","device","compute_type","loaded"} describing the
        currently loaded model. device is "gpu"/"cpu" for display."""
        device_label = None
        if self.device == "cuda":
            device_label = "gpu"
        elif self.device == "cpu":
            device_label = "cpu"
        return {
            "model": self.cfg.get("model"),
            "loaded_name": self.model_name,
            "device": device_label,
            "compute_type": self.compute_type,
            "loaded": self.model is not None,
            "cuda_missing": list(_CUDA_MISSING),
        }

    def _transcribe(self, audio, override_prompt=None):
        # language (None=auto-detect) + task (transcribe|translate) come from the
        # shared helper. Translate-to-English needs a multilingual model; on an
        # English-only model fall back to transcribe so the paste path can't fail.
        kwargs = _transcribe.transcribe_kwargs(self.cfg)
        ok, _warn = _transcribe.validate_task_for_model(
            self.cfg.get("model"), kwargs.get("task"))
        if not ok:
            kwargs["task"] = "transcribe"
        # Per-app context mode (when enabled) overrides the biasing prompt for
        # this utterance; otherwise use the learned/augmented bias prompt.
        prompt = (override_prompt or self._bias_prompt
                  or self.cfg.get("initial_prompt") or None)
        # Serialize inference against file transcription + model swaps (C1).
        with self._model_lock:
            segments, _info = self.model.transcribe(
                audio,
                beam_size=self.cfg.get("beam_size", 1),
                condition_on_previous_text=False,
                vad_filter=bool(self.cfg.get("vad_filter", True)),
                vad_parameters=dict(min_silence_duration_ms=500,
                                    speech_pad_ms=200),
                no_speech_threshold=0.6,
                # Personal-vocabulary biasing (TIER 0, soft): the augmented
                # prompt carries a short learned-term list; hotwords nudge
                # proper nouns.
                initial_prompt=prompt,
                hotwords=(self._hotwords or None),
                **kwargs,  # language + task
            )
            return "".join(s.text for s in segments)

    def _stream_transcribe(self, audio):
        """Transcribe for STREAMING: VAD OFF (the streaming loop does its own
        energy VAD; faster-whisper's VAD would strip short rolling buffers and
        emit nothing), greedy, no initial_prompt bias. CUDA->CPU fallback once."""
        # language + task from the shared helper. Streaming forces a concrete
        # language (auto-detect per rolling chunk is unstable) and degrades a
        # translate task on an English-only model to plain transcribe.
        kwargs = _transcribe.transcribe_kwargs(self.cfg)
        if kwargs.get("language") is None:
            kwargs["language"] = "en"
        ok, _warn = _transcribe.validate_task_for_model(
            self.cfg.get("model"), kwargs.get("task"))
        if not ok:
            kwargs["task"] = "transcribe"

        def _go():
            # Streaming re-transcribes a rolling buffer ~1x/s; biasing every pass
            # is cheap, but keep the hotword cap modest (build_hotwords caps it)
            # to avoid streaming hallucinations on partial buffers.
            # Serialize inference against file transcription + model swaps (C1).
            # The faster-whisper segment generator decodes lazily as it's
            # iterated, so the iteration MUST stay inside the lock.
            with self._model_lock:
                segs, _info = self.model.transcribe(
                    audio, beam_size=1,
                    condition_on_previous_text=False, vad_filter=False,
                    no_speech_threshold=0.6,
                    initial_prompt=(self._bias_prompt or None),
                    hotwords=(self._hotwords or None),
                    **kwargs)  # language + task
                parts = []
                for s in segs:
                    nsp = getattr(s, "no_speech_prob", 0.0) or 0.0
                    alp = getattr(s, "avg_logprob", 0.0) or 0.0
                    # Skip segments Whisper itself flags as silence, or very
                    # low-confidence garbage (hallucinations on noise).
                    if nsp > 0.8 or alp < -1.3:
                        continue
                    parts.append(s.text)
                return "".join(parts)
        try:
            return _go()
        except Exception as exc:
            if self.device == "cuda" and _is_cuda_error(exc):
                self._log("CUDA streaming error (%s); switching to CPU.", exc)
                # Swap the model under the lock so an in-flight inference on
                # another thread can't see a half-swapped engine (C1).
                with self._model_lock:
                    self.model = self._load_cpu_model()
                    self.device = "cpu"
                return _go()
            raise

    def _transcribe_with_fallback(self, audio, override_prompt=None):
        """Transcribe; on a CUDA-class runtime error transparently reload on CPU
        once and retry, then stick to CPU for the rest of the session.
        ``override_prompt`` (optional) lets a per-app context mode bias this
        utterance's initial_prompt; None -> the normal learned bias prompt."""
        try:
            return self._transcribe(audio, override_prompt=override_prompt)
        except Exception as exc:
            if self.device == "cuda" and _is_cuda_error(exc):
                self._log("CUDA inference error (%s); reloading on CPU and "
                          "retrying. Future dictations will use CPU.", exc)
                try:
                    # Swap the model under the lock (C1).
                    with self._model_lock:
                        self.model = self._load_cpu_model()
                        self.device = "cpu"
                    return self._transcribe(audio, override_prompt=override_prompt)
                except Exception:
                    log.error("CPU fallback also failed:\n%s",
                              traceback.format_exc())
                    raise
            raise

    # ---- trigger registration ----
    def _register_triggers(self):
        """(Re)install the dictation trigger and the optional command trigger.
        If BOTH are mouse chords (which may share a button, e.g. left+right and
        left+middle), they go on ONE multi-chord hook; otherwise each registers
        independently. Returns True if the dictation trigger armed."""
        for h in self._trigger_handles:
            try:
                h.stop()
            except Exception:
                pass
        self._trigger_handles = []
        dt, ct = self._trigger, self._command_trigger
        # Easiest model: TAP the trigger to dictate, HOLD it to enter command
        # mode. Works for a mouse chord AND a keyboard combo (e.g. hold
        # ctrl+shift+space ~1s for the AI-edit / voice-command mode).
        if self.cfg.get("command_via_hold", True) and _is_mouse_chord(dt):
            handle = _platform.make_tap_hold_chord(
                dt, self.on_trigger,
                self._on_command_hold_start, self._on_command_hold_end,
                float(self.cfg.get("command_hold_seconds", 0.7)))
            if handle is not None:
                self._trigger_handles.append(handle)
                return True
            # fall through to the older models if tap/hold is unsupported
        if self.cfg.get("command_via_hold", True) and dt and \
                not str(dt).lower().startswith("mouse:"):
            handle = _platform.make_tap_hold_keyboard(
                dt, self.on_trigger,
                self._on_command_hold_start, self._on_command_hold_end,
                float(self.cfg.get("command_hold_seconds", 0.7)))
            if handle is not None:
                self._trigger_handles.append(handle)
                # A separate command trigger still registers below if set; but
                # hold already covers command mode, so we're done.
                return True
            # fall through to plain keyboard registration if unsupported
        if _is_mouse_chord(dt) and ct and _is_mouse_chord(ct):
            handle = _platform.make_multi_chord(
                [(dt, self.on_trigger), (ct, self.on_command_trigger)])
            if handle is not None:
                self._trigger_handles.append(handle)
                return True
            # fall through to independent registration if unsupported
        ok = False
        hd = self._triggers.register(dt, self.on_trigger)
        if hd is not None:
            self._trigger_handles.append(hd)
            ok = True
        if ct:
            hc = self._triggers.register(ct, self.on_command_trigger)
            if hc is not None:
                self._trigger_handles.append(hc)
            else:
                self._log("Could not register command trigger '%s'.", ct)
        return ok

    def set_trigger(self, trigger):
        """(Re)register the dictation trigger live and persist it. Returns True
        on success (keeps the old trigger on failure)."""
        old = self._trigger
        self._trigger = trigger
        if self._register_triggers():
            self.cfg["trigger"] = trigger
            _config.save_config(self.cfg)
            self._log("Trigger set to '%s'.", trigger)
            return True
        self._trigger = old
        self._register_triggers()
        self._log("Could not register trigger '%s'; kept '%s'.", trigger, old)
        return False

    def set_command_trigger(self, trigger):
        """Set/clear the 2nd trigger that captures a spoken command directly
        (no wake word). Pass "" to disable. Persists to config."""
        self._command_trigger = trigger or ""
        ok = self._register_triggers()
        self.cfg["command_trigger"] = self._command_trigger
        _config.save_config(self.cfg)
        self._log("Command trigger set to '%s'.", self._command_trigger or "(none)")
        return ok

    @property
    def trigger(self):
        return self._trigger

    def set_mode(self, mode):
        """Switch between "batch" (press->speak->paste), "streaming" (words typed
        live as you speak), and "preview" (a live bar shows rough words; the clean
        batch transcript is pasted once on stop). Applied to the next activation;
        no model reload needed. Persists to config."""
        m = str(mode).lower()
        if "prev" in m:                       # "preview" / "live preview"
            mode = "preview"
        elif m.startswith("stream") or "type" in m:  # "streaming" / "live-type"
            mode = "streaming"
        else:
            mode = "batch"
        self.mode = mode
        self.cfg["mode"] = mode
        _config.save_config(self.cfg)
        self._log("Mode set to '%s'.", mode)
        return mode

    # ---- audio callback (PortAudio thread) ----
    def _audio_cb(self, indata, frames, time_info, status):
        if status:
            log.debug("audio status: %s", status)
        with self._frames_lock:
            if self._stream is None:
                return  # stale callback after teardown
            if self._max_frames and len(self._frames) >= self._max_frames:
                return
            # MUST copy: PortAudio reuses/overwrites the indata buffer.
            self._frames.append(indata.copy())
        # VU meter (best-effort, never block the audio thread).
        if self.on_level is not None:
            try:
                rms = float(np.sqrt(np.mean(np.square(indata))))
                # Light shaping so the meter is responsive (rms is small).
                level = min(1.0, rms * 6.0)
                self.on_level(level)
            except Exception:
                pass

    # ---- start / stop recording ----
    def _start_recording(self):
        with self._stream_lock:
            with self._frames_lock:
                self._frames = []
            try:
                self._stream = sd.InputStream(
                    samplerate=self.sr, channels=1, dtype="float32",
                    callback=self._audio_cb)
                self._stream.start()
            except Exception as exc:
                log.error("Microphone error: %s", exc)
                self._log("Microphone error: %s", exc)
                self.beeper.error()
                self._stream = None
                return False
        self.beeper.start()
        self._log("Recording started.")
        max_s = float(self.cfg.get("max_record_seconds", 120) or 0)
        if max_s > 0:
            self._max_frames = int((max_s * self.sr) / 256) + 64
            self._auto_stop_timer = threading.Timer(max_s, self._fire_auto_stop)
            self._auto_stop_timer.daemon = True
            self._auto_stop_timer.start()
        else:
            self._max_frames = 0
        return True

    def _fire_auto_stop(self):
        self._log("Max record time reached; auto-stopping.")
        self.ctrl_q.put(CMD_STOP)

    def _teardown_stream_and_get_audio(self):
        t = self._auto_stop_timer
        self._auto_stop_timer = None
        if t is not None:
            try:
                t.cancel()
            except Exception:
                pass
        with self._stream_lock:
            stream = self._stream
            if stream is not None:
                try:
                    stream.stop()
                    stream.close()
                except Exception as exc:
                    log.warning("Error closing stream: %s", exc)
            # Null the stream FIRST (under frames_lock) so any in-flight callback
            # early-returns, THEN harvest -> no lost/torn frame.
            with self._frames_lock:
                self._stream = None
                frames = self._frames
                self._frames = []
        self.beeper.stop()
        if self.on_level is not None:
            try:
                self.on_level(0.0)
            except Exception:
                pass
        if not frames:
            self._log("No audio captured.")
            return None
        try:
            return np.concatenate(frames, axis=0).flatten().astype(np.float32)
        except Exception as exc:
            log.error("Failed to assemble audio: %s", exc)
            return None

    # ---- the trigger callback (listener thread; keep MICROSCOPIC) ----
    def on_trigger(self):
        """Dictation trigger (e.g. left+right): streaming live typing or batch
        press->speak->paste, per self.mode."""
        try:
            if self.mode == "streaming":
                self._on_trigger_streaming()
                return
            if self.mode == "preview":
                self._on_trigger_preview()
                return
            with self.state_lock:
                if self.state == IDLE:
                    self.state = RECORDING
                    self._active = "dictation"
                    started = self._start_recording()
                    if not started:
                        self.state = IDLE
                        self._active = None
                        return
                    self._emit_state(RECORDING)
                elif self.state == RECORDING and self._active == "dictation":
                    self.state = TRANSCRIBING   # block toggles until done
                    self._emit_state(TRANSCRIBING)
                    self.ctrl_q.put(CMD_STOP)
                else:
                    self._log("Busy; ignoring dictation trigger.")
        except Exception:
            # The listener thread must NEVER die.
            log.error("on_trigger crashed:\n%s", traceback.format_exc())

    def _on_trigger_streaming(self):
        """Streaming-mode toggle: start a live session, or finalize it. Keep this
        microscopic -- the heavy final decode runs on the control thread."""
        with self.state_lock:
            if self.state == IDLE:
                self.state = RECORDING
                self._active = "dictation"
                if not self._start_streaming():
                    self.state = IDLE
                    self._active = None
                    return
                self._emit_state(RECORDING)
            elif self.state == RECORDING and self._active == "dictation":
                self.state = TRANSCRIBING   # block toggles until finalized
                self._emit_state(TRANSCRIBING)
                self.ctrl_q.put(CMD_STOP_STREAM)
            else:
                self._log("Busy; ignoring dictation trigger.")

    def _on_trigger_preview(self):
        """Live-preview toggle: start a preview session (rough live bar, document
        untouched), or finalize it. Keep microscopic -- the accurate final decode
        runs on the worker thread via the normal batch path."""
        with self.state_lock:
            if self.state == IDLE:
                self.state = RECORDING
                self._active = "dictation"
                if not self._start_preview():
                    self.state = IDLE
                    self._active = None
                    return
                self._emit_state(RECORDING)
            elif self.state == RECORDING and self._active == "dictation":
                self.state = TRANSCRIBING   # block toggles until finalized
                self._emit_state(TRANSCRIBING)
                self.ctrl_q.put(CMD_STOP_PREVIEW)
            else:
                self._log("Busy; ignoring dictation trigger.")

    def on_command_trigger(self):
        """Command trigger (e.g. left+middle): capture a short utterance and run
        it as a command (no wake word needed -- the trigger means 'command')."""
        try:
            with self.state_lock:
                if self.state == IDLE:
                    self.state = RECORDING
                    self._active = "command"
                    if not self._start_recording():
                        self.state = IDLE
                        self._active = None
                        return
                    self._emit_state(RECORDING)
                elif self.state == RECORDING and self._active == "command":
                    self.state = TRANSCRIBING
                    self._emit_state(TRANSCRIBING)
                    self.ctrl_q.put(CMD_STOP_COMMAND)
                else:
                    self._log("Busy; ignoring command trigger.")
        except Exception:
            log.error("on_command_trigger crashed:\n%s", traceback.format_exc())

    def _on_command_hold_start(self):
        """Chord HELD past the threshold -> begin capturing a spoken command
        (push-to-talk). Keep microscopic."""
        try:
            with self.state_lock:
                if self.state == IDLE:
                    self.state = RECORDING
                    self._active = "command"
                    if not self._start_recording():
                        self.state = IDLE
                        self._active = None
                        return
                    self._emit_state(RECORDING)
                else:
                    self._log("Busy; ignoring command hold.")
        except Exception:
            log.error("command hold start crashed:\n%s", traceback.format_exc())

    def _on_command_hold_end(self):
        """Chord released after a hold -> stop + run the command."""
        try:
            with self.state_lock:
                if self.state == RECORDING and self._active == "command":
                    self.state = TRANSCRIBING
                    self._emit_state(TRANSCRIBING)
                    self.ctrl_q.put(CMD_STOP_COMMAND)
        except Exception:
            log.error("command hold end crashed:\n%s", traceback.format_exc())

    def _start_streaming(self):
        """Create + start a StreamingSession (listener thread; keep light)."""
        if self.model is None:
            return False
        if self._typer is None:
            self._typer = _platform.make_typer()
        if self._typer is None:
            self._log("Streaming needs key synthesis this OS doesn't provide.")
            return False
        # Insertion method: paste (reliable everywhere incl. Win11 Notepad) or
        # type (synthesized keystrokes).
        self._stream_orig_clip = None
        if self.cfg.get("streaming_insert", "paste") == "type":
            insert_fn = self._typer.type_text
        else:
            try:
                self._stream_orig_clip = self.clip.save()
            except Exception:
                self._stream_orig_clip = None
            insert_fn = self._stream_paste
        self._stream_session = _streaming.StreamingSession(
            self.model, self.cfg, transcribe_fn=self._stream_transcribe,
            on_text=None, on_log=self._log, on_level=self.on_level,
            insert_fn=insert_fn, model_lock=self._model_lock)
        self._stream_session.keep_audio = bool(self.cfg.get("save_recordings"))
        if not self._stream_session.start():
            self._stream_session = None
            self.beeper.error()
            return False
        self.beeper.start()
        return True

    def _stream_paste(self, text):
        """Insert a streamed chunk via clipboard paste (reliable in all apps,
        including the Win11 Notepad). The user's clipboard is saved at session
        start and restored in _stop_streaming."""
        try:
            self.clip._set_text_immediate(text)
            time.sleep(0.02)
            self._typer.press_keys("ctrl+v")
            time.sleep(0.02)
        except Exception:
            log.error("stream paste failed", exc_info=True)

    def _stop_streaming(self):
        """Finalize the streaming session (heavy final decode) -> IDLE. Runs on
        the control thread."""
        sess = self._stream_session
        self._stream_session = None
        if sess is None:
            self._finish_idle()
            return
        self.beeper.stop()
        text = ""
        try:
            text = sess.finalize_and_stop()
        except Exception:
            log.error("streaming finalize failed:\n%s", traceback.format_exc())
        if self.cfg.get("save_recordings"):
            try:
                _debuglog.save_recording(sess.full_audio(), self.sr, {
                    "mode": "streaming", "text": (text or "").strip(),
                    "model": self.cfg.get("model"), "device": self.device})
            except Exception:
                log.error("save streaming recording failed", exc_info=True)
        # Restore the clipboard we borrowed for paste-mode streaming.
        if self._stream_orig_clip is not None:
            try:
                self.clip.restore(self._stream_orig_clip)
            except Exception:
                pass
            self._stream_orig_clip = None
        if text:
            self.last_transcript = text
            # Record the streamed transcript to history (streaming is biased but
            # not auto-corrected -- append-only typing can't be rewritten mid-
            # stream; the user can still edit + learn from it in History).
            self._record_history(text, "streaming")
            if self.on_transcript:
                try:
                    self.on_transcript(text)
                except Exception:
                    pass
            self.beeper.done()
        self._finish_idle()

    # ---- live-preview mode (rough live bar -> accurate batch paste) ----
    def _start_preview(self):
        """Start mic capture + a live overlay decode for preview mode. The
        document is NEVER touched during dictation (insert_fn is inert); audio is
        always kept so the final accurate decode runs on the full utterance.

        FAIL-OPEN: on any failure, hide the bar and fall back to a plain batch
        recording so the user still gets their text."""
        if self.model is None:
            return False
        self._preview_fellback = False
        try:
            self._preview_show()                      # GUI shows the bar (marshalled)
            sess = _streaming.StreamingSession(
                self.model, self.cfg,
                transcribe_fn=self._stream_transcribe,   # locked rolling decode
                on_text=self._preview_text,              # -> overlay.set_text (marshalled)
                on_log=self._log, on_level=self.on_level,
                insert_fn=lambda *_a, **_k: None,        # NEVER touch the document
                model_lock=self._model_lock)
            sess.keep_audio = True                       # keep full audio for final decode
            if not sess.start():
                raise RuntimeError("preview session failed to start")
            self._stream_session = sess
            self.beeper.start()
            return True
        except Exception:
            log.error("preview start failed; falling back to batch:\n%s",
                      traceback.format_exc())
            # FAIL-OPEN: hide the bar and start a plain batch recording instead.
            self._preview_hide()
            self._stream_session = None
            self._preview_fellback = True               # remember for stop path
            return self._start_recording()

    def _stop_preview(self):
        """Finalize a preview session: harvest the WHOLE utterance and hand it to
        the SAME accurate batch path as normal dictation (work_q -> _handle_
        recording), so the final paste is byte-for-byte identical to batch. Runs
        on the control thread."""
        sess = self._stream_session
        self._stream_session = None
        self.beeper.stop()

        # FAIL-OPEN branch: preview never started -> we recorded plain batch audio.
        if sess is None or self._preview_fellback:
            self._preview_fellback = False
            audio = self._teardown_stream_and_get_audio()
            self._preview_hide()
            if audio is None:
                self.beeper.error()
                self._finish_idle()
                return
            self.work_q.put((audio, "dictation"))       # normal batch worker path
            return

        # Normal preview: stop the live session, harvest the WHOLE utterance.
        try:
            sess.finalize_and_stop()                    # ends live decode; no doc writes
        except Exception:
            log.error("preview finalize failed:\n%s", traceback.format_exc())
        audio = None
        try:
            audio = sess.full_audio()                   # the entire utterance (keep_audio)
        except Exception:
            log.error("preview full_audio failed:\n%s", traceback.format_exc())
        self._preview_hide()                            # overlay gone before paste

        if audio is None or not len(audio):
            self.beeper.error()
            self._finish_idle()
            return
        # Hand off to the SAME accurate batch path as normal dictation.
        self.work_q.put((audio, "dictation"))

    # ---- control thread (heavy teardown) ----
    def _control_loop(self):
        while self._running:
            cmd = self.ctrl_q.get()
            if cmd is None:
                self.ctrl_q.task_done()
                break
            try:
                if cmd == CMD_STOP:
                    self._handle_stop("dictation")
                elif cmd == CMD_STOP_COMMAND:
                    self._handle_stop("command")
                elif cmd == CMD_STOP_STREAM:
                    self._stop_streaming()
                elif cmd == CMD_STOP_PREVIEW:
                    self._stop_preview()
            except Exception:
                log.error("control_loop error:\n%s", traceback.format_exc())
                self._finish_idle()
            finally:
                self.ctrl_q.task_done()

    def _handle_stop(self, kind="dictation"):
        with self.state_lock:
            if self._stop_in_progress:
                return
            if self.state == RECORDING:
                self.state = TRANSCRIBING
                self._emit_state(TRANSCRIBING)
            elif self.state != TRANSCRIBING:
                return
            self._stop_in_progress = True
        audio = self._teardown_stream_and_get_audio()
        if audio is None:
            self.beeper.error()
            self._finish_idle()
            return
        self._log("%s stopped (%.2fs). Queued.",
                  "Command" if kind == "command" else "Recording",
                  len(audio) / self.sr)
        self.work_q.put((audio, kind))

    def _exec_command(self, action, raw, dur, took):
        """Run a recognized voice command's keystrokes via the platform Typer."""
        if self._typer is None:
            self._typer = _platform.make_typer()
        if self._typer is None:
            self._log("Voice command recognized but this OS has no key-typer.")
            self.beeper.error()
            return
        try:
            _commands.execute_command(action, self._typer)
            desc = _commands.describe(action)
            self._log("Voice command: %r -> %s (%.2fs audio, %.2fs decode).",
                      raw.strip(), desc, dur, took)
            self.last_transcript = "[command] " + desc
            if self.on_transcript:
                try:
                    self.on_transcript(self.last_transcript)
                except Exception:
                    pass
            self.beeper.done()
        except Exception:
            log.error("Voice command failed:\n%s", traceback.format_exc())
            self.beeper.error()

    def _finish_idle(self):
        with self.state_lock:
            self.state = IDLE
            self._stop_in_progress = False
            self._active = None
        self._emit_state(IDLE)

    # ---- worker thread (transcription + paste / command) ----
    def _worker_loop(self):
        while self._running:
            item = self.work_q.get()
            if item is None:
                self.work_q.task_done()
                break
            audio, kind = item
            try:
                if kind == "command":
                    self._handle_command(audio)
                else:
                    self._handle_recording(audio)
            except Exception:
                log.error("Worker failed (listener stays alive):\n%s",
                          traceback.format_exc())
                self.beeper.error()
            finally:
                self._finish_idle()
                self.work_q.task_done()

    def _handle_command(self, audio):
        """Command-trigger path: transcribe the utterance and run it as a command
        (no wake word required -- the command TRIGGER signals intent)."""
        dur = len(audio) / self.sr
        min_s = float(self.cfg.get("min_record_seconds", 0.4) or 0)
        if dur < min_s:
            self._log("Discarded short command (%.2fs).", dur)
            self.beeper.error()
            return
        t0 = time.time()
        raw = self._transcribe_with_fallback(audio)
        took = time.time() - t0
        if self.cfg.get("save_recordings"):
            _debuglog.save_recording(audio, self.sr, {
                "mode": "command", "raw": (raw or "").strip(),
                "model": self.cfg.get("model"), "device": self.device,
                "decode_s": round(took, 2)})
        action = _commands.parse_command(
            raw,
            command_word=self.cfg.get("command_word", "computer"),
            require_command_word=False)   # trigger means command; wake word optional
        if action is not None:
            self._exec_command(action, raw, dur, took)
        elif self.cfg.get("ai_edit", True) and raw.strip():
            self._ai_edit(raw.strip())
        else:
            self._log("Command not recognized: %r (%.2fs audio, %.2fs decode).",
                      raw.strip(), dur, took)
            self.beeper.filtered()

    def _read_clipboard_text(self):
        try:
            import pyperclip
            return pyperclip.paste() or ""
        except Exception:
            return ""

    def _ai_edit(self, instruction):
        """A non-mechanical command -> LLM edit of the SELECTED text: copy the
        selection, ask the model to apply the instruction, paste the result back
        (replacing the selection), then restore the user's clipboard."""
        # AI editing is OPTIONAL. If the local model isn't set up, say so clearly
        # (a quick reachability check) instead of hanging on a timeout.
        if not _ai.is_available(self.cfg):
            self._log("AI editing isn't set up (no local model running). "
                      "Recognized '%r' as an edit, but skipping. Install the "
                      "local AI from Settings to enable smart edits.", instruction)
            self.beeper.filtered()
            return
        if self._typer is None:
            self._typer = _platform.make_typer()
        orig = self.clip.save()
        selected = ""
        try:
            if self._typer is not None:
                self._typer.press_keys("ctrl+c")   # copy the highlighted text
                time.sleep(0.15)
                selected = self._read_clipboard_text()
        except Exception:
            log.error("AI edit: copy selection failed:\n%s",
                      traceback.format_exc())
        self._log("AI edit: %r (selection: %d chars)...", instruction,
                  len(selected))
        t0 = time.time()
        result, err = _ai.edit_text(instruction, selected, self.cfg)
        if err or not result:
            self._log("AI edit failed: %s", err or "empty result")
            try:
                self.clip.restore(orig)
            except Exception:
                pass
            self.beeper.error()
            return
        # Replace the selection with the result, then restore the real clipboard.
        try:
            self.clip.paste_text(result)
        finally:
            try:
                self.clip.restore(orig)
            except Exception:
                pass
        self.last_transcript = result
        if self.on_transcript:
            try:
                self.on_transcript(result)
            except Exception:
                pass
        self._log("AI edit done (%d->%d chars, %.1fs).", len(selected),
                  len(result), time.time() - t0)
        self.beeper.done()

    def _handle_recording(self, audio):
        dur = len(audio) / self.sr
        min_s = float(self.cfg.get("min_record_seconds", 0.4) or 0)
        if dur < min_s:
            self._log("Discarded short recording (%.2fs < %.2fs).", dur, min_s)
            self.beeper.error()
            return
        t0 = time.time()
        # Per-app context mode (best-effort): resolve the active foreground app's
        # biasing prompt. On any failure (Win32 error, off, ...) this returns
        # None and transcription falls back to the static learned prompt -- the
        # paste path is never broken.
        mode_prompt = self._get_current_mode_prompt()
        raw = self._transcribe_with_fallback(audio, override_prompt=mode_prompt)
        took = time.time() - t0
        if self.cfg.get("save_recordings"):
            _debuglog.save_recording(audio, self.sr, {
                "mode": "dictation", "raw": (raw or "").strip(),
                "model": self.cfg.get("model"), "device": self.device,
                "decode_s": round(took, 2)})
        # Voice command? (e.g. "computer backspace five", "computer enter").
        if self.cfg.get("voice_commands", True):
            action = _commands.parse_command(
                raw,
                command_word=self.cfg.get("command_word", "computer"),
                require_command_word=self.cfg.get("require_command_word", True))
            if action is not None:
                self._exec_command(action, raw, dur, took)
                return
        text, was_filtered = clean_transcript(raw, self.cfg)
        if not text:
            if was_filtered:
                self._log("Hallucination filter dropped %r (%.2fs audio, "
                          "%.2fs decode).", raw.strip(), dur, took)
                self.beeper.filtered()
            else:
                self._log("Empty transcript (%.2fs audio, %.2fs decode); "
                          "nothing pasted.", dur, took)
                self.beeper.error()
            return
        # Apply learned deterministic corrections (TIER 1) AFTER cleanup and
        # BEFORE paste, so what's pasted == what's recorded in history (the diff
        # baseline). Best-effort: never breaks the paste path.
        if self._corrections:
            try:
                text = _learn.apply_corrections(text, self._corrections)
            except Exception:
                pass
        # Apply user text-expansion snippets (TIER 1.5: deterministic expansion)
        # AFTER corrections and BEFORE paste, so what's pasted == what's recorded
        # in history. Batch only. Best-effort: never breaks the paste path.
        if self._snippets:
            try:
                text = _snippets.apply_snippets(text, self._snippets)
            except Exception:
                pass
        # AI auto-cleanup (opt-in, batch-only): fix punctuation, grammar, filler.
        # Runs AFTER clean_transcript + corrections + snippets, BEFORE paste.
        # Graceful fallback on timeout/error: just use the text as-is (never
        # block the paste). History records the COMMITTED text (post-cleanup if
        # it succeeds, raw if it errors).
        cleanup_level = self.cfg.get("cleanup_level", "light")
        # "light" is rule-based and fully offline -- run it unconditionally (no
        # Ollama probe needed, instant). medium/high need Ollama, so only probe
        # availability for those (a down Ollama is skipped, not stalled).
        if (self.cfg.get("auto_cleanup", False) and cleanup_level != "off" and
                (cleanup_level == "light" or _ai.is_available(self.cfg))):
            try:
                cleaned, err = _ai.cleanup_text(
                    text,
                    cleanup_level,
                    self.cfg)
                if err:
                    self._log("AI cleanup failed (using raw): %s", err)
                elif cleaned and cleaned.strip():
                    text = cleaned
                    self._log("AI cleanup applied (%s).", cleanup_level)
            except Exception:
                # Never block paste on any error (including timeout).
                log.debug("AI cleanup exception (using raw):\n%s",
                          traceback.format_exc())
        delivered = self.clip.paste_text(text)
        self._log("Pasted %d chars (%.2fs audio, %.2fs decode, delivered=%s).",
                  len(text), dur, took, delivered)
        # Report the transcript without the trailing space we add for pasting.
        self.last_transcript = text.rstrip()
        # Record the COMMITTED (post-cleanup, post-corrections) text to history.
        self._record_history(self.last_transcript, "dictation")
        if self.on_transcript:
            try:
                self.on_transcript(self.last_transcript)
            except Exception:
                pass
        if delivered:
            self.beeper.done()
        else:
            self.beeper.error()

    # ---- lifecycle ----
    def start(self):
        """Start the worker/control threads and register the trigger. Idempotent
        for the threads; re-registers the trigger each call. Returns True if the
        trigger registered."""
        if self.model is None:
            raise RuntimeError("load_model() must be called before start().")
        self._running = True
        if not self._threads_started:
            threading.Thread(target=self._worker_loop, daemon=True,
                             name="worker").start()
            threading.Thread(target=self._control_loop, daemon=True,
                             name="control").start()
            self._threads_started = True
        self._emit_state(IDLE)
        ok = self._register_triggers()
        if ok:
            extra = (" + command: %s" % self._command_trigger
                     if self._command_trigger else "")
            self._log("OpenVerba ready (%s, model=%s). Trigger: %s%s.",
                      self.device, self.cfg.get("model"), self._trigger, extra)
        return ok

    def pause(self):
        """Unregister all triggers but keep the model + threads warm. Call
        start() / resume() to re-arm."""
        for h in self._trigger_handles:
            try:
                h.stop()
            except Exception:
                pass
        self._trigger_handles = []
        self._log("Dictation paused (triggers disarmed).")

    def resume(self):
        """Re-arm the triggers after pause()."""
        armed = self._register_triggers()
        self._log("Dictation resumed." if armed else
                  "Could not re-arm the trigger.")
        return armed

    @property
    def is_armed(self):
        return bool(self._trigger_handles)

    def stop(self):
        """Fully stop: disarm triggers, stop any in-flight stream, and signal the
        worker/control threads to exit. The engine can be start()ed again."""
        self._running = False
        for h in self._trigger_handles:
            try:
                h.stop()
            except Exception:
                pass
        self._trigger_handles = []
        # Tear down a live streaming/preview session if any (both reuse
        # _stream_session), then ensure the preview bar disappears on app stop.
        if self._stream_session is not None:
            try:
                self._stream_session.finalize_and_stop(timeout=2.0)
            except Exception:
                pass
            self._stream_session = None
        self._preview_hide()
        # Stop a live recording stream if any.
        try:
            with self._stream_lock:
                if self._stream is not None:
                    try:
                        self._stream.stop()
                        self._stream.close()
                    except Exception:
                        pass
                with self._frames_lock:
                    self._stream = None
                    self._frames = []
        except Exception:
            pass
        # Wake the loops so the daemon threads exit cleanly.
        try:
            self.ctrl_q.put(None)
            self.work_q.put(None)
        except Exception:
            pass
        self._threads_started = False
        self._emit_state(IDLE)
        self._log("OpenVerba stopped.")
