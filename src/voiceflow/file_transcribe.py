"""
voiceflow.file_transcribe - pure transcription + subtitle-formatting logic for
the "transcribe an audio/video file" feature.

This module is deliberately GUI-free and (almost) side-effect-free:

  * The formatters (``segments_to_text`` / ``segments_to_srt`` / ``segments_to_vtt``)
    are PURE string functions -- fully unit-testable headless, no model, no I/O.
  * ``load_audio_file`` decodes any FFmpeg-readable media (.mp3/.wav/.m4a/.webm/
    .mp4/...) to a mono float32 numpy array. It reuses faster-whisper's own PyAV
    decoder (already an installed dependency), with a direct-``ffmpeg`` CLI
    fallback. Returns ``None`` on failure (the caller surfaces a friendly toast).
  * ``transcribe_file`` wraps ``model.transcribe()`` with the SAME bias/config
    keys as batch dictation and the SAME CUDA->CPU fallback policy, FAIL-OPEN:
    it returns ``([], error_msg)`` on any failure rather than raising.

It NEVER touches the live dictation engine's state: the model is used read-only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

log = logging.getLogger("voiceflow.file_transcribe")


# ---------------------------------------------------------------------------
# Segment value object (one cue from faster-whisper).
# ---------------------------------------------------------------------------
@dataclass
class FileSegment:
    """A single transcription segment: a time span and its (trimmed) text."""

    start: float    # seconds
    end: float      # seconds
    text: str       # text for this span


# ---------------------------------------------------------------------------
# CUDA-class error sniff (kept independent of engine so this module stands
# alone; mirrors voiceflow.engine._is_cuda_error).
# ---------------------------------------------------------------------------
def _is_cuda_error(exc) -> bool:
    s = (str(exc) or "").lower()
    return any(k in s for k in
               ("cuda", "cublas", "cudnn", "out of memory", "gpu", "device"))


class _NullLock:
    """A no-op context manager used when no model lock is supplied, so the
    transcribe path needn't special-case the absence of a lock."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ---------------------------------------------------------------------------
# Timestamp formatting.
# ---------------------------------------------------------------------------
def _fmt_ts(seconds: float, sep: str) -> str:
    """Format ``seconds`` as HH:MM:SS<sep>mmm (sep="," for SRT, "." for VTT).
    Negative/None coerce to 0. Milliseconds are floored to 3 digits."""
    try:
        total = float(seconds)
    except (TypeError, ValueError):
        total = 0.0
    if total < 0 or total != total:   # negative or NaN
        total = 0.0
    millis = int(round(total * 1000.0))
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return "%02d:%02d:%02d%s%03d" % (hours, minutes, secs, sep, millis)


# ---------------------------------------------------------------------------
# Pure formatters. Each takes a list[FileSegment] and returns a string.
# ---------------------------------------------------------------------------
def segments_to_text(segments) -> str:
    """Merge segments into plain text, one segment per line (newline-joined).
    Empty/whitespace-only segments are dropped."""
    if not segments:
        return ""
    lines = []
    for s in segments:
        text = (getattr(s, "text", "") or "").strip()
        if text:
            lines.append(text)
    return "\n".join(lines)


def segments_to_srt(segments) -> str:
    """Format segments as SubRip (.srt):

        1
        00:00:00,000 --> 00:00:05,000
        Text

    Empty-text segments are skipped. Returns "" for no segments."""
    if not segments:
        return ""
    blocks = []
    idx = 1
    for s in segments:
        text = (getattr(s, "text", "") or "").strip()
        if not text:
            continue
        start = _fmt_ts(getattr(s, "start", 0.0), ",")
        end = _fmt_ts(getattr(s, "end", 0.0), ",")
        blocks.append("%d\n%s --> %s\n%s\n" % (idx, start, end, text))
        idx += 1
    return "\n".join(blocks)


def segments_to_vtt(segments) -> str:
    """Format segments as WebVTT (.vtt):

        WEBVTT

        00:00:00.000 --> 00:00:05.000
        Text

    Always starts with the WEBVTT header. Empty-text segments are skipped."""
    parts = ["WEBVTT\n"]
    if segments:
        cues = []
        for s in segments:
            text = (getattr(s, "text", "") or "").strip()
            if not text:
                continue
            start = _fmt_ts(getattr(s, "start", 0.0), ".")
            end = _fmt_ts(getattr(s, "end", 0.0), ".")
            cues.append("%s --> %s\n%s\n" % (start, end, text))
        if cues:
            parts.append("\n".join(cues))
    return "\n".join(parts)


# Dispatch table for the GUI ("text"/"srt"/"vtt" -> formatter, extension).
FORMATTERS = {
    "text": (segments_to_text, ".txt"),
    "srt": (segments_to_srt, ".srt"),
    "vtt": (segments_to_vtt, ".vtt"),
}


def format_segments(segments, fmt: str) -> str:
    """Format ``segments`` using one of "text"/"srt"/"vtt" (default text)."""
    formatter, _ext = FORMATTERS.get(fmt, FORMATTERS["text"])
    return formatter(segments)


def extension_for(fmt: str) -> str:
    """The file extension for an output format key ("text"->.txt etc.)."""
    _formatter, ext = FORMATTERS.get(fmt, FORMATTERS["text"])
    return ext


# ---------------------------------------------------------------------------
# Audio loading (any FFmpeg-decodable media -> mono float32 @ sr).
# ---------------------------------------------------------------------------
def load_audio_file(file_path: str, sr: int = 16000):
    """Decode an audio/video file to a mono float32 numpy array at ``sr`` Hz.

    Tries faster-whisper's PyAV decoder first (already an installed dependency,
    handles .mp3/.wav/.m4a/.flac/.ogg/.webm/.mp4/.mkv/... via FFmpeg's codecs),
    then a direct ``ffmpeg`` CLI fallback. Returns ``None`` on any failure
    (unsupported format, missing file, no decoder) -- the caller tells the user.
    """
    if not file_path:
        return None
    # 1) faster-whisper's PyAV-based decoder (best-effort).
    try:
        from faster_whisper.audio import decode_audio
        audio = decode_audio(file_path, sampling_rate=sr)
        arr = np.asarray(audio, dtype=np.float32)
        if arr.size:
            return arr.flatten()
    except Exception:
        log.debug("decode_audio failed for %r; trying ffmpeg CLI",
                  file_path, exc_info=True)
    # 2) Direct ffmpeg CLI fallback (no new Python dependency).
    try:
        return _ffmpeg_decode(file_path, sr)
    except Exception:
        log.debug("ffmpeg CLI decode failed for %r", file_path, exc_info=True)
        return None


def _ffmpeg_decode(file_path: str, sr: int):
    """Decode via the ``ffmpeg`` executable to raw mono float32 PCM. Returns a
    numpy array, or None if ffmpeg isn't available / fails."""
    import shutil
    import subprocess

    exe = shutil.which("ffmpeg")
    if not exe:
        return None
    cmd = [
        exe, "-nostdin", "-threads", "0", "-i", file_path,
        "-f", "f32le", "-ac", "1", "-acodec", "pcm_f32le",
        "-ar", str(int(sr)), "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, check=False)
    if proc.returncode != 0 or not proc.stdout:
        return None
    arr = np.frombuffer(proc.stdout, dtype=np.float32)
    return arr.copy() if arr.size else None


# ---------------------------------------------------------------------------
# Transcription wrapper (model is read-only; FAIL-OPEN).
# ---------------------------------------------------------------------------
def transcribe_file(file_path, model, cfg, on_progress=None,
                    should_cancel=None, on_cuda_error=None, lock=None):
    """Transcribe a media file with ``model`` (a faster_whisper.WhisperModel).

    Returns ``(segments, error_msg)``:
      * ``segments`` is a list[FileSegment] (possibly empty).
      * ``error_msg`` is None on success, or a short human string on failure.

    Reuses the batch-dictation config keys (language, beam_size, vad_filter, the
    learned-bias prompt + hotwords stored in cfg as ``__bias_prompt__`` /
    ``__hotwords__``). Uses faster-whisper's internal long-file chunking.

    FAIL-OPEN: any exception is caught and returned as ``([], error_msg)``.
    ``should_cancel()`` (optional) is polled between segments; on cancel the
    segments collected so far are returned with ``error_msg="canceled"``.
    ``on_progress(msg)`` (optional, best-effort) receives status strings.
    ``on_cuda_error(model)`` (optional) lets the caller swap to a CPU model on a
    CUDA-class failure and retry once.
    ``lock`` (optional, a threading.Lock) MUST be the SAME lock the live engine
    uses to serialize model.transcribe(): faster-whisper / CTranslate2 is not
    safe for concurrent inference on one WhisperModel, and the engine may swap
    the model on a CUDA->CPU fallback. Holding it around the decode (and the CPU-
    fallback swap) prevents file transcription from racing live dictation. The
    faster-whisper segment generator decodes lazily, so the WHOLE iteration runs
    under the lock. When None, a no-op lock is used (headless tests).
    """
    def _prog(msg):
        if on_progress:
            try:
                on_progress(msg)
            except Exception:
                pass

    if model is None:
        return [], "No speech model is loaded."

    cfg = cfg or {}
    audio = load_audio_file(file_path, int(cfg.get("sample_rate", 16000) or 16000))
    if audio is None or not getattr(audio, "size", 0):
        return [], ("Could not read this file. Try a common audio/video format "
                    "like .mp3, .wav, .m4a, or .mp4.")

    bias_prompt = (cfg.get("__bias_prompt__") or cfg.get("initial_prompt") or None)
    hotwords = (cfg.get("__hotwords__") or None)
    model_lock = lock or _NullLock()

    def _decode(active_model):
        # Serialize the ENTIRE decode (faster-whisper iterates lazily) against
        # the live engine's inference + model swaps via the shared lock (C1).
        with model_lock:
            seg_iter, _info = active_model.transcribe(
                audio,
                language=cfg.get("language"),            # None = auto-detect
                beam_size=int(cfg.get("beam_size", 1) or 1),
                condition_on_previous_text=False,
                vad_filter=bool(cfg.get("vad_filter", True)),
                vad_parameters=dict(min_silence_duration_ms=500,
                                    speech_pad_ms=200),
                no_speech_threshold=0.6,
                initial_prompt=bias_prompt,
                hotwords=hotwords,
            )
            out = []
            canceled = False
            for s in seg_iter:
                if should_cancel is not None:
                    try:
                        if should_cancel():
                            canceled = True
                            break
                    except Exception:
                        pass
                text = (getattr(s, "text", "") or "").strip()
                out.append(FileSegment(
                    start=float(getattr(s, "start", 0.0) or 0.0),
                    end=float(getattr(s, "end", 0.0) or 0.0),
                    text=text,
                ))
            return out, canceled

    try:
        segments, canceled = _decode(model)
        return segments, ("canceled" if canceled else None)
    except Exception as exc:
        # CUDA-class failure: let the caller hand us a CPU model and retry once.
        # The caller's on_cuda_error swaps the engine's shared model; it runs
        # OUTSIDE _decode but the retry _decode re-takes the lock.
        if _is_cuda_error(exc) and on_cuda_error is not None:
            _prog("GPU error; retrying on CPU...")
            try:
                with model_lock:
                    cpu_model = on_cuda_error(exc)
                if cpu_model is not None:
                    segments, canceled = _decode(cpu_model)
                    return segments, ("canceled" if canceled else None)
            except Exception:
                log.error("CPU retry failed for %r", file_path, exc_info=True)
        log.error("transcribe_file failed for %r: %s", file_path, exc)
        return [], "Transcription failed: %s" % exc
