"""
voiceflow.config - load/save config.json (in the per-user data dir) with type
and range coercion so a single bad edit can never brick startup.

Ported from main.py's load_config/_coerce_config/save_config. Differences:
  - config.json lives in %LOCALAPPDATA%\\VoiceFlow (constants.CONFIG_PATH).
  - the trigger key is "trigger" (the legacy "hotkey" key is accepted on load
    and migrated forward), matching the hotkeys/engine API.
"""

import os
import json
import logging

from .constants import (
    DEFAULT_CONFIG, CONFIG_PATH, MODELS_DIR, ensure_data_dir,
    _VALID_SAMPLE_RATES,
)

log = logging.getLogger("voiceflow.config")


def _coerce_config(cfg):
    """Type/range-validate config values so a single bad edit can't brick
    startup. Falls back to the DEFAULT_CONFIG value (recorded) on any bad
    field. Returns a list of human-readable correction strings."""
    corrections = []

    def _num(key, caster, minimum=None, allowed=None):
        try:
            v = caster(cfg.get(key))
            if v is None:
                raise ValueError("None")
            if minimum is not None and v < minimum:
                raise ValueError("below %s" % minimum)
            if allowed is not None and v not in allowed:
                raise ValueError("not in allowed set")
            cfg[key] = v
        except Exception as exc:
            corrections.append("%s=%r invalid (%s) -> %r"
                               % (key, cfg.get(key), exc, DEFAULT_CONFIG[key]))
            cfg[key] = DEFAULT_CONFIG[key]

    def _str(key):
        v = cfg.get(key)
        if not isinstance(v, str) or not v.strip():
            corrections.append("%s=%r invalid -> %r"
                               % (key, v, DEFAULT_CONFIG[key]))
            cfg[key] = DEFAULT_CONFIG[key]
        else:
            cfg[key] = v.strip()

    def _bool(key):
        if not isinstance(cfg.get(key), bool):
            corrections.append("%s=%r not bool -> %r"
                               % (key, cfg.get(key), DEFAULT_CONFIG[key]))
            cfg[key] = DEFAULT_CONFIG[key]

    def _str_enum(key, allowed):
        v = cfg.get(key)
        if not isinstance(v, str) or v not in allowed:
            corrections.append("%s=%r invalid -> %r"
                               % (key, v, DEFAULT_CONFIG[key]))
            cfg[key] = DEFAULT_CONFIG[key]

    _str("trigger")
    _str("model")
    _str("compute_type")
    _str("cpu_compute_type")
    _str("device")
    _str("insert_method")
    _str("mode")
    if cfg.get("mode") not in ("batch", "streaming", "preview"):
        corrections.append("mode=%r invalid -> 'batch'" % cfg.get("mode"))
        cfg["mode"] = "batch"
    _num("preview_max_chars", int, minimum=20)
    if not isinstance(cfg.get("preview_pos"), str):
        cfg["preview_pos"] = ""   # "x,y" (may be empty = default position)
    _num("streaming_chunk_seconds", float, minimum=0.2)
    _num("streaming_silence_seconds", float, minimum=0.2)
    _num("streaming_max_buffer_seconds", float, minimum=2.0)
    _num("command_hold_seconds", float, minimum=0.2)
    _num("ai_timeout", float, minimum=5)
    _num("cleanup_timeout", float, minimum=5)
    _str("command_word")
    _str("ai_provider")
    _str("ai_model")
    _str("cleanup_model")
    # cleanup_keep_alive is a free-form Ollama duration string ("10m"/"1h"); just
    # require a non-empty string (Ollama parses the format itself).
    if not isinstance(cfg.get("cleanup_keep_alive"), str) or \
            not cfg.get("cleanup_keep_alive").strip():
        corrections.append("cleanup_keep_alive=%r invalid -> %r"
                           % (cfg.get("cleanup_keep_alive"),
                              DEFAULT_CONFIG["cleanup_keep_alive"]))
        cfg["cleanup_keep_alive"] = DEFAULT_CONFIG["cleanup_keep_alive"]
    else:
        cfg["cleanup_keep_alive"] = cfg["cleanup_keep_alive"].strip()
    _str("ollama_url")
    _str("streaming_insert")
    if cfg.get("streaming_insert") not in ("paste", "type"):
        cfg["streaming_insert"] = "paste"
    _str("bug_report_email")
    _str("bug_report_method")
    if cfg.get("bug_report_method") not in ("form", "mailto"):
        cfg["bug_report_method"] = "form"
    _str("update_manifest_url")
    if not isinstance(cfg.get("last_notified_version"), str):
        cfg["last_notified_version"] = ""
    if not isinstance(cfg.get("ai_api_key"), str):
        cfg["ai_api_key"] = ""
    # command_trigger may be "" (disabled), so don't force a non-empty default.
    if not isinstance(cfg.get("command_trigger"), str):
        cfg["command_trigger"] = ""
    _num("sample_rate", int, minimum=1, allowed=_VALID_SAMPLE_RATES)
    _num("beam_size", int, minimum=1)
    _num("file_transcribe_batch_limit", int, minimum=1)
    # Supported extensions must be a non-empty list of strings; fall back to the
    # default set on anything malformed (a single bad edit can't break the picker).
    exts = cfg.get("file_transcribe_supported_exts")
    if not isinstance(exts, list) or not exts or \
            not all(isinstance(e, str) for e in exts):
        corrections.append("file_transcribe_supported_exts=%r invalid -> default"
                           % (exts,))
        cfg["file_transcribe_supported_exts"] = list(
            DEFAULT_CONFIG["file_transcribe_supported_exts"])
    _num("min_record_seconds", float, minimum=0)
    _num("max_record_seconds", float, minimum=0)
    _num("clipboard_restore_delay_ms", float, minimum=0)
    _num("clipboard_read_timeout_ms", float, minimum=0)
    _num("last_update_check", float, minimum=0)
    for b in ("vad_filter", "add_trailing_space", "strip_whitespace",
              "allow_multiline", "beep", "filter_hallucinations", "tray_icon",
              "local_files_only", "autostart", "first_run_done",
              "voice_commands", "require_command_word", "command_via_hold",
              "ai_edit", "save_recordings", "auto_update_check",
              "transcript_history", "personal_vocab_enabled",
              "corrections_enabled", "snippets_enabled",
              "file_transcribe_enabled", "translate_to_english",
              "auto_cleanup", "per_app_modes"):
        _bool(b)
    _str_enum("cleanup_level", ("off", "light", "medium", "high"))
    return corrections


def load_config():
    """Load config.json, filling in any missing keys from DEFAULT_CONFIG and
    coercing values to safe types/ranges. Creates the file (with defaults) on
    first run. The returned dict carries two internal bookkeeping keys:
    "__parse_warn__" (str|None) and "__corrections__" (list[str])."""
    ensure_data_dir()
    cfg = dict(DEFAULT_CONFIG)
    parse_warn = None
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                user = json.load(fh)
            if isinstance(user, dict):
                # Migrate legacy "hotkey" -> "trigger".
                if "trigger" not in user and "hotkey" in user:
                    user["trigger"] = user["hotkey"]
                cfg.update({k: user[k] for k in user if k in DEFAULT_CONFIG})
        except Exception as exc:
            parse_warn = "Could not parse config.json (%s); using defaults." % exc
    else:
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
                json.dump(DEFAULT_CONFIG, fh, indent=2)
        except Exception:
            pass
    corrections = _coerce_config(cfg)
    # Normalize language AFTER coercion (language may be None = auto-detect).
    if cfg.get("language") in ("", "auto"):
        cfg["language"] = None
    elif cfg.get("language") is not None and not isinstance(cfg.get("language"), str):
        corrections.append("language=%r invalid -> None (auto)" % cfg.get("language"))
        cfg["language"] = None
    cfg["__parse_warn__"] = parse_warn
    cfg["__corrections__"] = corrections
    return cfg


def save_config(cfg):
    """Persist the user-facing config keys back to config.json (skipping the
    internal __dunder__ bookkeeping keys). Returns True on success."""
    ensure_data_dir()
    try:
        data = {k: cfg[k] for k in DEFAULT_CONFIG if k in cfg}
        with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        return True
    except Exception as exc:
        log.warning("Could not save config.json: %s", exc)
        return False


def resolve_download_root(cfg):
    """Return the directory where models should be downloaded/loaded from:
    the configured download_root if set, else the per-user MODELS_DIR."""
    root = cfg.get("download_root")
    if root:
        return root
    ensure_data_dir()
    return MODELS_DIR
