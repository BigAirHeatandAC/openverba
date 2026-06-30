"""
voiceflow.debuglog - optional capture of audio recordings + their transcripts so
you can review what the app heard vs. what it produced, and improve from real
data.

When config ``save_recordings`` is on, each utterance is saved to
``%LOCALAPPDATA%\\VoiceFlow\\recordings``:
  - <timestamp>-<mode>.wav   : the exact mic audio that was transcribed
  - transcripts.jsonl        : one JSON line per utterance with the audio file
                               name, mode, the RAW model output, the final text/
                               action, model/device, and timings.

Pair them up later (listen to the .wav, read the line) to spot dropped words,
"..." artifacts, hallucinations, etc. Off by default (privacy + disk).
"""

from __future__ import annotations

import os
import json
import wave
import logging
import datetime
import threading

import numpy as np

from .constants import RECORDINGS_DIR, ensure_recordings_dir

log = logging.getLogger("voiceflow.debuglog")

_lock = threading.Lock()


def _stamp():
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]


def save_recording(audio, sr, meta):
    """Save `audio` (float32 numpy, mono) as a WAV and append a transcript line.

    meta: dict with at least {"mode": ...}; everything in it is recorded. Returns
    the wav path, or None on failure / empty audio. Best-effort: never raises."""
    try:
        if audio is None or len(audio) == 0:
            return None
        ensure_recordings_dir()
        stamp = _stamp()
        mode = str(meta.get("mode", "rec"))
        base = "%s-%s" % (stamp, mode)
        wav_path = os.path.join(RECORDINGS_DIR, base + ".wav")
        arr = np.clip(np.asarray(audio, dtype=np.float32), -1.0, 1.0)
        pcm = (arr * 32767).astype("<i2").tobytes()
        with wave.open(wav_path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(int(sr))
            wf.writeframes(pcm)
        entry = dict(meta)
        entry["time"] = stamp
        entry["wav"] = os.path.basename(wav_path)
        entry["duration_s"] = round(len(arr) / float(sr), 2)
        line = json.dumps(entry, ensure_ascii=False)
        with _lock:
            with open(os.path.join(RECORDINGS_DIR, "transcripts.jsonl"),
                      "a", encoding="utf-8") as f:
                f.write(line + "\n")
        log.info("Saved recording %s (%s, %.2fs)", entry["wav"], mode,
                 entry["duration_s"])
        return wav_path
    except Exception:
        log.error("save_recording failed", exc_info=True)
        return None
