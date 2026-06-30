"""
voiceflow.history - the local-only transcript history store (history.jsonl).

Every committed transcript's TEXT is appended here (gated by the
``transcript_history`` config flag), independent of ``save_recordings`` (which
governs AUDIO + debug captures). History is text-only and cheap, so it's on by
default; nothing is ever uploaded -- it lives in DATA_DIR
(%LOCALAPPDATA%\\VoiceFlow) next to config.json and survives upgrades.

Record schema (one JSON line per utterance)::

    {"id": "20260615-143052-318", "ts": 1750000000.0, "mode": "dictation",
     "original": "meet me at big air", "edited": null, "source_app": "chrome.exe"}

  - id          : sortable + unique timestamp ("%Y%m%d-%H%M%S-%f"[:-3]).
  - ts          : time.time() epoch float (for "2m ago" relative display).
  - mode        : "dictation" | "streaming" | "command".
  - original    : the committed text exactly as pasted (post-cleanup,
                  post-corrections, rstripped). This is the diff baseline.
  - edited      : None until the user edits it in the History view.
  - source_app  : best-effort foreground process name (or None).

EVERYTHING in this module is best-effort: it must NEVER raise into the engine /
paste path. A history failure can never break dictation.
"""

from __future__ import annotations

import os
import json
import logging
import datetime
import threading

from .constants import HISTORY_PATH, ensure_data_dir

log = logging.getLogger("voiceflow.history")

_lock = threading.Lock()

# Guards against two appends in the same millisecond producing the same id.
_last_id = None
_id_seq = 0

# Hard cap so the file can't grow unbounded. When append() sees the file has more
# than HISTORY_MAX_LINES lines, it rewrites keeping only the most recent
# HISTORY_KEEP_LINES. The cap check is lazy (only on append) so the common path
# stays a cheap append.
HISTORY_MAX_LINES = 5000
HISTORY_KEEP_LINES = 4000


def _stamp():
    """A sortable, unique id (mirrors debuglog._stamp, copied so history has no
    dependency on debuglog). A per-process counter suffix is appended only when
    two stamps would collide (same millisecond) so ids stay unique -- they key
    update_edit()."""
    global _last_id, _id_seq
    base = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
    if base == _last_id:
        _id_seq += 1
        return "%s-%d" % (base, _id_seq)
    _last_id = base
    _id_seq = 0
    return base


def _detect_source_app():
    """Best-effort foreground process name on Windows (e.g. 'chrome.exe'), else
    None. Wrapped entirely in try/except -- never a hard dependency, never
    blocks. Returns None on any platform/probe failure."""
    try:
        if os.name != "nt":
            return None
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return None
        # PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = kernel32.OpenProcess(0x1000, False, pid.value)
        if not h:
            return None
        try:
            buf = ctypes.create_unicode_buffer(260)
            size = wintypes.DWORD(260)
            # QueryFullProcessImageNameW (0 = Win32 path format).
            if kernel32.QueryFullProcessImageNameW(h, 0, buf,
                                                   ctypes.byref(size)):
                return os.path.basename(buf.value) or None
        finally:
            kernel32.CloseHandle(h)
    except Exception:
        return None
    return None


def append(original, mode, source_app=None):
    """Append one history record. Gated by the CALLER (the engine checks the
    ``transcript_history`` flag). Returns the new record id, or None on failure.

    ``source_app`` defaults to a best-effort foreground process probe; pass an
    explicit value (incl. None) to skip the probe."""
    try:
        if original is None:
            return None
        original = str(original).strip()
        if not original:
            return None
        ensure_data_dir()
        if source_app is None:
            source_app = _detect_source_app()
        import time as _time
        with _lock:
            rec_id = _stamp()   # under the lock: id uniqueness + counter safety
            rec = {
                "id": rec_id,
                "ts": _time.time(),
                "mode": str(mode or "dictation"),
                "original": original,
                "edited": None,
                "source_app": source_app,
            }
            line = json.dumps(rec, ensure_ascii=False)
            with open(HISTORY_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            _enforce_cap_locked()
        return rec_id
    except Exception:
        log.debug("history.append failed", exc_info=True)
        return None


def _enforce_cap_locked():
    """If history.jsonl has grown past HISTORY_MAX_LINES, rewrite it keeping only
    the most recent HISTORY_KEEP_LINES. Caller MUST hold _lock. Best-effort."""
    try:
        if not os.path.exists(HISTORY_PATH):
            return
        with open(HISTORY_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) <= HISTORY_MAX_LINES:
            return
        keep = lines[-HISTORY_KEEP_LINES:]
        tmp = HISTORY_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(keep)
        os.replace(tmp, HISTORY_PATH)
    except Exception:
        log.debug("history cap enforcement failed", exc_info=True)


def load(limit=500):
    """Return up to ``limit`` records, NEWEST-FIRST. Bad/corrupt lines are
    skipped. Never raises (returns [] on failure)."""
    try:
        if not os.path.exists(HISTORY_PATH):
            return []
        with _lock:
            with open(HISTORY_PATH, "r", encoding="utf-8") as f:
                lines = f.readlines()
        out = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if isinstance(rec, dict) and rec.get("id"):
                out.append(rec)
        out.reverse()  # newest-first
        if limit is not None and limit >= 0:
            out = out[:limit]
        return out
    except Exception:
        log.debug("history.load failed", exc_info=True)
        return []


def update_edit(rec_id, edited):
    """Set the matching record's ``edited`` field via an atomic full-file rewrite
    (write to .tmp -> os.replace). Returns True if a record was updated. Never
    raises."""
    try:
        if not rec_id or not os.path.exists(HISTORY_PATH):
            return False
        edited = None if edited is None else str(edited)
        with _lock:
            with open(HISTORY_PATH, "r", encoding="utf-8") as f:
                lines = f.readlines()
            changed = False
            out_lines = []
            for line in lines:
                s = line.strip()
                if not s:
                    continue
                try:
                    rec = json.loads(s)
                except Exception:
                    out_lines.append(s)
                    continue
                if isinstance(rec, dict) and rec.get("id") == rec_id:
                    rec["edited"] = edited
                    changed = True
                    out_lines.append(json.dumps(rec, ensure_ascii=False))
                else:
                    out_lines.append(s)
            if not changed:
                return False
            tmp = HISTORY_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("\n".join(out_lines) + "\n")
            os.replace(tmp, HISTORY_PATH)
        return True
    except Exception:
        log.debug("history.update_edit failed", exc_info=True)
        return False


def clear():
    """Delete history.jsonl (and any stray .tmp). Returns True on success. Never
    raises."""
    try:
        with _lock:
            for p in (HISTORY_PATH, HISTORY_PATH + ".tmp"):
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except Exception:
                    pass
        return True
    except Exception:
        log.debug("history.clear failed", exc_info=True)
        return False
