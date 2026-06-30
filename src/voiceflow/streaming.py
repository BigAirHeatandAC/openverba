"""
voiceflow.streaming - real-time "words appear as you speak" dictation.

This is the STREAMING counterpart to the batch DictationEngine. It reuses the
already-loaded faster-whisper model (no second model, no extra process) and, as
the user speaks, types CONFIRMED words straight into the focused app via a
platform Typer (SendInput Unicode on Windows) -- leaving the clipboard
untouched.

Stability (the whole trick): Whisper revises earlier words as it hears more
context, which would make naive streaming flicker/rewrite text in a foreign app.
We never rewrite. We re-transcribe a rolling buffer ~once a second and only emit
a word once TWO consecutive transcriptions agree on it (LocalAgreement-2). That
makes the output strictly append-only -- the only safe contract for typing into
an app we don't control. A short trailing silence (energy VAD) ends the
utterance: we flush the remaining tail and reset the buffer so re-transcription
stays fast.

Design: docs/STREAMING_DESIGN.md. The committer is deliberately small and
pluggable so a faster policy (e.g. AlignAtt/SimulStreaming) can replace
LocalAgreement later without touching the audio/typing loop.

NOTE: AI auto-cleanup is NOT applied in streaming mode. Streaming types live
text append-only (it can't rewrite earlier words after they've been sent to the
app). Cleanup happens in batch mode only, where the entire transcript is
finalized before paste -- buffering the whole utterance to clean it would defeat
the streaming latency benefit and rewriting already-pasted text would break the
append-only contract.
"""

from __future__ import annotations

import re
import time
import logging
import threading

import numpy as np
import sounddevice as sd

from . import transcribe as _transcribe

log = logging.getLogger("voiceflow.streaming")

# Whole-hypothesis Whisper silence artifacts to drop in streaming. These appear
# when the model is fed (near-)silence; the energy gate stops most, this catches
# the rest. Kept tight so it can't swallow a real sentence (only an utterance
# that is ENTIRELY one of these is dropped).
_HALLUCINATIONS = {
    "you", "thank you", "thanks", "thanks for watching",
    "thank you for watching", "please subscribe", "like and subscribe",
    "bye", "okay", "ok", "", ".",
}


def _norm_hyp(text):
    return re.sub(r"[^a-z0-9 ]", "", (text or "").lower()).strip()


class _NullLock:
    """A no-op context manager used when no model lock is supplied (so the
    default transcribe path doesn't need a conditional `with`)."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ---------------------------------------------------------------------------
# LocalAgreement-2 committer: emit only the word-prefix two passes agree on.
# Append-only -- it never returns a word it has already returned.
# ---------------------------------------------------------------------------
class LocalAgreement:
    def __init__(self):
        self._prev = []        # words from the previous transcription pass
        self._committed = 0     # how many words we've already emitted this utterance

    def update(self, words):
        """Feed the current full transcription (list of words). Return the list
        of NEWLY-confirmed words (those now in the agreed prefix of the last two
        passes, beyond what we already emitted)."""
        agreed = 0
        for a, b in zip(self._prev, words):
            if a == b:
                agreed += 1
            else:
                break
        new = []
        if agreed > self._committed:
            new = words[self._committed:agreed]
            self._committed = agreed
        self._prev = words
        return new

    def flush(self, words):
        """End-of-utterance: accept everything beyond what's committed as final
        (we won't get another pass to agree with). Returns the remaining words."""
        new = words[self._committed:] if len(words) > self._committed else []
        self._committed = len(words)
        self._prev = words
        return new

    def reset(self):
        self._prev = []
        self._committed = 0


def _tokenize(text):
    """Whitespace word tokens (this inherently drops newlines, so we can never
    emit an Enter)."""
    return text.split()


# ---------------------------------------------------------------------------
# StreamingSession: owns a mic stream + a decode loop, types confirmed words.
# ---------------------------------------------------------------------------
class StreamingSession:
    """One push-to-talk streaming session. Construct with the loaded model, the
    config, and a platform Typer; call start(); call finalize_and_stop() to end.

    Callbacks (optional, fire from the session's worker thread):
      on_text(full_text)   the running committed transcript (for an overlay/log)
      on_log(msg)          status line
      on_level(rms)        mic VU 0..1
    """

    _SILENCE_RMS = 0.006   # below this = "silence" for end-of-utterance VAD

    def __init__(self, model, cfg, typer=None, transcribe_fn=None,
                 on_text=None, on_log=None, on_level=None, insert_fn=None,
                 model_lock=None):
        self.model = model
        self.cfg = cfg
        self.typer = typer
        # Serializes model.transcribe() in the default (non-injected) path
        # against concurrent inference / model swaps. The engine normally injects
        # its own locked _stream_transcribe via transcribe_fn, so this only
        # matters if _default_transcribe runs. A dummy no-op lock when absent.
        self._model_lock = model_lock or _NullLock()
        # How committed text is inserted. Prefer an explicit insert_fn (e.g. the
        # engine's paste path, reliable in the Win11 Notepad); else type via the
        # typer.
        self._insert = insert_fn or (typer.type_text if typer is not None else None)
        # transcribe_fn(audio)->str lets the engine inject its CUDA->CPU
        # fallback wrapper; default to a plain model.transcribe.
        self._transcribe_fn = transcribe_fn or self._default_transcribe

        self.on_text = on_text
        self.on_log = on_log
        self.on_level = on_level

        self.sr = int(cfg.get("sample_rate", 16000))
        self.chunk_s = float(cfg.get("streaming_chunk_seconds", 1.0))
        self.silence_s = float(cfg.get("streaming_silence_seconds", 0.7))
        self.max_buf_s = float(cfg.get("streaming_max_buffer_seconds", 14.0))
        self.lang = cfg.get("language") or "en"

        self._buf = []                 # np float32 chunks for the CURRENT utterance
        self._buf_lock = threading.Lock()
        self._last_voice = None        # monotonic time of the last loud frame
        self._voiced = False           # has any speech-level audio arrived since reset?
        self._stream = None
        self._thread = None
        self._stop = threading.Event()
        self._la = LocalAgreement()
        self._typed_any = False        # have we typed any word this session?
        self._committed_text = []      # all committed words this session (for on_text)
        self._err = None
        self.keep_audio = False        # set True to retain the full session audio
        self._all = []                 # every chunk this session (for debug saving)

    def full_audio(self):
        """The entire session's mic audio (float32) if keep_audio was set."""
        if not self._all:
            return None
        return np.concatenate(self._all, axis=0).flatten().astype(np.float32)

    # ---- public lifecycle ----
    def start(self):
        with self._buf_lock:
            self._buf = []
        self._last_voice = time.monotonic()
        try:
            self._stream = sd.InputStream(
                samplerate=self.sr, channels=1, dtype="float32",
                blocksize=int(self.sr * 0.05), callback=self._audio_cb)
            self._stream.start()
        except Exception as exc:
            self._err = exc
            log.error("Streaming mic error: %s", exc)
            self._log("Microphone error: %s" % exc)
            return False
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="streaming")
        self._thread.start()
        self._log("Streaming started.")
        return True

    def finalize_and_stop(self, timeout=10.0):
        """Stop capture, run a final decode, flush the remaining tail, and
        return the full committed text. Safe to call once."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._close_stream()
        # Final flush (in case the worker exited before a last decode).
        try:
            self._decode_step(final=True)
        except Exception:
            log.error("final decode failed", exc_info=True)
        if self.cfg.get("add_trailing_space", True) and self._typed_any:
            self._type(" ")
        text = " ".join(self._committed_text).strip()
        self._log("Streaming stopped (%d words)." % len(self._committed_text))
        return text

    # ---- audio ----
    def _audio_cb(self, indata, frames, time_info, status):
        if status:
            log.debug("stream audio status: %s", status)
        if self._stop.is_set():
            return
        chunk = indata.copy()
        with self._buf_lock:
            self._buf.append(chunk)
        if self.keep_audio:
            self._all.append(chunk)
        try:
            rms = float(np.sqrt(np.mean(np.square(indata))))
        except Exception:
            rms = 0.0
        if rms > self._SILENCE_RMS:
            self._last_voice = time.monotonic()
            self._voiced = True
        if self.on_level is not None:
            try:
                self.on_level(min(1.0, rms * 6.0))
            except Exception:
                pass

    def _snapshot(self):
        with self._buf_lock:
            if not self._buf:
                return None
            return np.concatenate(self._buf, axis=0).flatten().astype(np.float32)

    def _buf_seconds(self):
        with self._buf_lock:
            return sum(len(c) for c in self._buf) / float(self.sr)

    def _reset_buffer(self):
        with self._buf_lock:
            self._buf = []
        self._la.reset()
        self._last_voice = time.monotonic()
        self._voiced = False

    # ---- decode loop ----
    def _run(self):
        last_decode = 0.0
        while not self._stop.is_set():
            time.sleep(0.05)
            now = time.monotonic()
            # End-of-utterance: trailing silence with something buffered.
            silent_for = now - (self._last_voice or now)
            if silent_for >= self.silence_s and self._buf_seconds() > 0.3:
                try:
                    self._decode_step(final=True)
                except Exception:
                    log.error("utterance decode failed", exc_info=True)
                self._reset_buffer()
                last_decode = now
                continue
            # Periodic incremental decode.
            if now - last_decode >= self.chunk_s and self._buf_seconds() >= 0.5:
                try:
                    self._decode_step(final=False)
                except Exception:
                    log.error("incremental decode failed", exc_info=True)
                last_decode = now
                # Hard cap: if the buffer grows too long without a pause, commit
                # it as final and reset so decode latency stays bounded.
                if self._buf_seconds() >= self.max_buf_s:
                    try:
                        self._decode_step(final=True)
                    except Exception:
                        log.error("cap decode failed", exc_info=True)
                    self._reset_buffer()

    def _default_transcribe(self, audio):
        # Fallback path (when no transcribe_fn is injected). The engine wires its
        # own _stream_transcribe with hotword/prompt biasing; mirror it here for
        # completeness. cfg may carry "__hotwords__" set by the engine's reload;
        # absent that, just the base initial_prompt.
        # language + task from the shared helper (overrides self.lang). Streaming
        # forces a concrete language (auto-detect per chunk is unstable) and
        # degrades a translate task on an English-only model to transcribe.
        kwargs = _transcribe.transcribe_kwargs(self.cfg)
        if kwargs.get("language") is None:
            kwargs["language"] = self.lang
        ok, _warn = _transcribe.validate_task_for_model(
            self.cfg.get("model"), kwargs.get("task"))
        if not ok:
            kwargs["task"] = "transcribe"
        # Serialize against concurrent inference / model swaps (C1). The faster-
        # whisper generator decodes lazily, so iterate inside the lock too.
        with self._model_lock:
            segs, _info = self.model.transcribe(
                audio, beam_size=1,
                condition_on_previous_text=False, vad_filter=False,
                no_speech_threshold=0.6,
                initial_prompt=(self.cfg.get("__bias_prompt__")
                                or self.cfg.get("initial_prompt") or None),
                hotwords=(self.cfg.get("__hotwords__") or None),
                **kwargs,  # language + task (overrides self.lang)
            )
            return "".join(s.text for s in segs)

    def _decode_step(self, final):
        audio = self._snapshot()
        if audio is None or len(audio) < int(self.sr * 0.2):
            return
        # Energy gate: never transcribe a buffer that had no speech-level audio
        # (this is what makes Whisper hallucinate "thank you"/"you" on silence).
        if not self._voiced or float(np.max(np.abs(audio))) < 0.012:
            return
        text = self._transcribe_fn(audio) or ""
        # Drop a whole-utterance Whisper silence artifact.
        if _norm_hyp(text) in _HALLUCINATIONS:
            return
        words = _tokenize(text)
        new = self._la.flush(words) if final else self._la.update(words)
        if new:
            self._emit_words(new)

    # ---- emit (type) ----
    def _emit_words(self, words):
        # Append-only: type each new word, space-separated. A leading space keeps
        # words apart across both the current and previous utterances.
        piece = ""
        for w in words:
            piece += (" " + w) if (self._typed_any or piece) else w
            self._typed_any = True
        self._type(piece)
        self._committed_text.extend(words)
        if self.on_text is not None:
            try:
                self.on_text(" ".join(self._committed_text))
            except Exception:
                pass

    def _type(self, text):
        if not text or self._insert is None:
            return
        try:
            self._insert(text)
        except Exception:
            log.error("insert failed", exc_info=True)

    def _log(self, msg):
        log.info(msg)
        if self.on_log is not None:
            try:
                self.on_log(msg)
            except Exception:
                pass

    def _close_stream(self):
        s = self._stream
        self._stream = None
        if s is not None:
            try:
                s.stop()
                s.close()
            except Exception:
                pass
        if self.on_level is not None:
            try:
                self.on_level(0.0)
            except Exception:
                pass
