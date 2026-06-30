"""
voiceflow.modes - per-app context profiles ("modes").

A *mode* is a small, user-editable profile that says: "when the foreground app
is one of THESE executables, bias dictation with THIS cleanup prompt/tone." When
``per_app_modes`` is enabled, the engine detects the foreground window's exe
basename and resolves the first matching enabled mode; its ``prompt`` is used to
bias Whisper transcription (e.g. Email -> formal, Slack -> casual, Code -> don't
reformat). When nothing matches, the built-in "Default" mode applies.

Design (mirrors the defensive style of ``snippets.py`` / ``learn.py``):
  * Stdlib only. Every public function is best-effort and NEVER raises into the
    dictation/paste path.
  * ``get_foreground_exe`` is fully wrapped: on any error (missing API, denied
    process, non-Windows, ...) it returns ``None`` and the caller falls back to
    the static prompt -- the paste path is never broken.
  * ``load_modes`` seeds + heals: missing/empty/corrupt modes.json -> a fresh
    copy of ``BUILTIN_MODES``; per-mode validation silently drops bad entries;
    a "Default" mode is always guaranteed to exist.
  * ``resolve_mode`` is a pure function (no I/O, no side effects) and always
    returns a valid mode dict, never ``None``.
"""

from __future__ import annotations

import os
import json
import copy
import logging

from .constants import DATA_DIR, ensure_data_dir

log = logging.getLogger("voiceflow.modes")

# The required shape of a mode dict. A mode that is missing any of these keys
# (with the right type) is silently dropped on load.
_REQUIRED_KEYS = ("name", "enabled", "apps", "prompt", "tone")

# A last-resort fallback used only if validation somehow leaves us without a
# "Default" mode (should be impossible, but resolve_mode must never return None).
_FALLBACK_MODE = {
    "name": "Default",
    "enabled": True,
    "apps": [],
    "prompt": "Hello. This is a dictation. Add commas, periods, and proper "
              "capitalization.",
    "tone": "neutral",
}

# Built-in seed modes (written to modes.json on first use if it's missing/empty).
BUILTIN_MODES = [
    {
        "name": "Default",
        "enabled": True,
        "apps": [],  # empty -> matches any app (the catch-all)
        "prompt": "Hello. This is a dictation. Add commas, periods, and proper "
                  "capitalization.",
        "tone": "neutral",
    },
    {
        "name": "Email",
        "enabled": True,
        "apps": ["outlook.exe", "thunderbird.exe", "mail.exe", "hxoutlook.exe"],
        "prompt": "This is formal business email. Use proper punctuation, "
                  "capitalization, and a professional tone.",
        "tone": "formal",
    },
    {
        "name": "Chat/Slack",
        "enabled": True,
        "apps": ["slack.exe", "discord.exe", "teams.exe", "ms-teams.exe",
                 "telegram.exe", "whatsapp.exe"],
        "prompt": "This is casual chat. Keep it concise, natural, and "
                  "conversational. It is OK to skip formal punctuation.",
        "tone": "casual",
    },
    {
        "name": "Code",
        "enabled": True,
        "apps": ["code.exe", "vscode.exe", "devenv.exe", "sublime_text.exe",
                 "pycharm64.exe", "idea64.exe", "windowsterminal.exe"],
        "prompt": "User is writing code. Do NOT reformat, do NOT fix grammar, "
                  "do NOT add punctuation. Keep code as code.",
        "tone": "code-aware",
    },
]


# ---------------------------------------------------------------------------
# Foreground app detection (Windows, ctypes; fully defensive).
# ---------------------------------------------------------------------------
def get_foreground_exe():
    """Return the lowercase basename of the foreground window's executable
    (e.g. ``"slack.exe"``), or ``None`` on any error.

    Uses ctypes: GetForegroundWindow -> GetWindowThreadProcessId -> OpenProcess
    + QueryFullProcessImageNameW. Fully wrapped; NEVER raises (missing API,
    permission denied, non-Windows, timeout, ...). The caller treats ``None`` as
    "no specific app" and falls back to the Default mode / static prompt.
    """
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None

        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return None

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not h:
            # Fall back to the older (broader) access right.
            PROCESS_QUERY_INFORMATION = 0x0400
            h = kernel32.OpenProcess(
                PROCESS_QUERY_INFORMATION, False, pid.value)
        if not h:
            return None
        try:
            buf_len = wintypes.DWORD(1024)
            buf = ctypes.create_unicode_buffer(buf_len.value)
            ok = kernel32.QueryFullProcessImageNameW(
                h, 0, buf, ctypes.byref(buf_len))
            if not ok:
                return None
            full_path = buf.value or ""
        finally:
            try:
                kernel32.CloseHandle(h)
            except Exception:
                pass
        if not full_path:
            return None
        return os.path.basename(full_path).lower() or None
    except Exception:
        log.debug("get_foreground_exe failed", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Validation / persistence.
# ---------------------------------------------------------------------------
def _valid_mode(m):
    """True if ``m`` is a well-formed mode dict (right keys + types)."""
    if not isinstance(m, dict):
        return False
    for k in _REQUIRED_KEYS:
        if k not in m:
            return False
    if not isinstance(m.get("name"), str) or not m["name"].strip():
        return False
    if not isinstance(m.get("enabled"), bool):
        return False
    apps = m.get("apps")
    if not isinstance(apps, list) or not all(isinstance(a, str) for a in apps):
        return False
    if not isinstance(m.get("prompt"), str):
        return False
    if not isinstance(m.get("tone"), str):
        return False
    return True


def _ensure_default(modes):
    """Guarantee the returned list contains a "Default" mode (prepended if
    missing) so resolve_mode always has a catch-all."""
    if not any(isinstance(m, dict) and m.get("name") == "Default"
               for m in modes):
        modes = [copy.deepcopy(_FALLBACK_MODE)] + list(modes)
    return modes


def load_modes(path, seed=True):
    """Load modes.json from ``path``. If the file is missing or empty, return a
    copy of ``BUILTIN_MODES`` -- and, when ``seed`` is True, also write it to
    ``path`` (the UI's "first use" behavior). ``seed=False`` loads without
    creating a file (used by the engine at construct time so it never writes to
    disk just by starting). Corrupt JSON falls back to a copy of ``BUILTIN_MODES``
    (without overwriting the user's file). Invalid per-mode entries are silently
    dropped. Always returns a list containing at least the Default mode. NEVER
    raises.
    """
    try:
        if not path or not os.path.exists(path) or os.path.getsize(path) == 0:
            seeded = copy.deepcopy(BUILTIN_MODES)
            if seed:
                save_modes(path, seeded)
            return seeded
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            raise ValueError("modes.json is not a list")
        valid = [m for m in data if _valid_mode(m)]
        if not valid:
            # Nothing usable in the file -> fall back to the builtins (in
            # memory; do not clobber the user's file).
            return copy.deepcopy(BUILTIN_MODES)
        return _ensure_default(valid)
    except Exception:
        log.warning("modes: load failed; using builtin modes", exc_info=True)
        return copy.deepcopy(BUILTIN_MODES)


def save_modes(path, modes):
    """Persist ``modes`` to ``path`` (atomic temp-file + rename). Returns True
    on success, False on any I/O error. NEVER raises."""
    try:
        ensure_data_dir()
        data = modes if isinstance(modes, list) else []
        tmp = str(path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except Exception:
        log.debug("modes: save failed", exc_info=True)
        return False


# ---------------------------------------------------------------------------
# Resolution (pure function: no I/O, no side effects).
# ---------------------------------------------------------------------------
def resolve_mode(foreground_exe, modes):
    """Return the FIRST enabled mode whose ``apps`` list matches
    ``foreground_exe`` (case-insensitive), or the Default mode if no specific
    mode matches. A mode with an empty ``apps`` list matches any app (catch-all).

    ``foreground_exe`` may be ``None`` (no app detected) -> the Default mode.
    Always returns a valid mode dict; NEVER returns ``None``.
    """
    try:
        exe = (foreground_exe or "").strip().lower()
        modes = modes if isinstance(modes, list) else []
        default = None
        for m in modes:
            if not isinstance(m, dict) or not m.get("enabled", False):
                continue
            apps = m.get("apps") or []
            if not apps:
                # First catch-all wins as the "default" candidate, but keep
                # scanning for a specific app match.
                if default is None:
                    default = m
                continue
            if exe and any(isinstance(a, str) and a.strip().lower() == exe
                           for a in apps):
                return m
        if default is not None:
            return default
        # No catch-all among the given modes: prefer one literally named
        # "Default", else the last-resort fallback.
        for m in modes:
            if isinstance(m, dict) and m.get("name") == "Default":
                return m
        return copy.deepcopy(_FALLBACK_MODE)
    except Exception:
        log.debug("resolve_mode failed", exc_info=True)
        return copy.deepcopy(_FALLBACK_MODE)


def modes_path():
    """Convenience: the canonical modes.json path in DATA_DIR."""
    return os.path.join(DATA_DIR, "modes.json")
