"""
voiceflow.transcribe - pure, testable helpers for building faster-whisper
transcription kwargs from config + detecting English-only models.

This module deliberately imports NOTHING from the engine/model layer (no
faster_whisper, no engine). That keeps it unit-testable headless and makes the
recognition-language / translate-to-English policy a single source of truth that
every transcribe call site (batch, warmup, streaming) shares.

Two config keys drive it:
  * "language" -> the Whisper recognition language. None / "auto" / "" means
    auto-detect (Whisper guesses from the audio).
  * "translate_to_english" (bool) -> when True, ask Whisper to TRANSLATE the
    recognized speech to English (task="translate"). This REQUIRES a multilingual
    model; English-only ("*.en", "distil-large-v3") models can't translate.
"""

from __future__ import annotations


def transcribe_kwargs(cfg: dict) -> dict:
    """Build Whisper transcription parameters from config.

    Returns a dict with exactly two keys, safe to splat into
    ``model.transcribe(..., **kwargs)``::

        {"language": str | None, "task": "transcribe" | "translate"}

    Logic:
      language = cfg.get("language")
        - None / "auto" / "" / non-str -> None (auto-detect)
        - else -> the language code (e.g. "en", "es", "fr")
      task = "translate" if cfg.get("translate_to_english") else "transcribe"
        - Note: task="translate" REQUIRES a multilingual model. Callers that
          want graceful degradation should consult ``validate_task_for_model``
          (the engine warmup does this and falls back to "transcribe").
    """
    cfg = cfg or {}
    lang = cfg.get("language")
    if not isinstance(lang, str) or not lang.strip() or lang.strip().lower() == "auto":
        lang = None
    else:
        lang = lang.strip()

    task = "translate" if cfg.get("translate_to_english") else "transcribe"
    return {"language": lang, "task": task}


def is_english_only_model(model_name) -> bool:
    """Return True if the model name indicates an English-only model (no
    multilingual / translate support).

    English-only models:
      - any name containing ".en" (tiny.en, base.en, small.en, medium.en,
        distil-small.en, distil-medium.en, ...)
      - "distil-large-v3" (the distil-large checkpoint is English-only; the
        smaller distil-small / distil-medium checkpoints ARE multilingual)
    """
    if not isinstance(model_name, str) or not model_name:
        return False
    name = model_name.strip().lower()
    if ".en" in name:
        return True
    if "distil-large-v3" in name:
        return True
    return False


def validate_task_for_model(model_name, task: str):
    """Validate that ``task`` is compatible with ``model_name``.

    Returns ``(is_valid, warning_msg)``:
      - (True, None)  -> OK, the task is compatible with the model.
      - (False, msg)  -> task == "translate" on an English-only model; ``msg``
                         explains the incompatibility (caller should fall back
                         to "transcribe").
    """
    if task != "translate":
        return True, None
    if is_english_only_model(model_name):
        msg = ("Model %r is English-only and cannot translate to English. "
               "Switch to a multilingual model (e.g. small, medium, large-v3) "
               "to use translate-to-English." % model_name)
        return False, msg
    return True, None
