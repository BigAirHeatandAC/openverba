"""
voiceflow.platform.windows - the Windows platform backend.

This module is the OS-specific code EXTRACTED VERBATIM from the original
voiceflow/paste.py and voiceflow/hotkeys.py, re-exposed behind the platform
ABCs (voiceflow.platform.base). The hard-won, verified behaviours are preserved
byte-for-byte:

  * 40-byte SendInput INPUT struct (_INPUT sized to its largest union member;
    ctypes.sizeof(_INPUT) == 40 on 64-bit), with explicit argtypes -> otherwise
    Windows rejects every paste with ERROR_INVALID_PARAMETER and nothing pastes.
  * All-format clipboard save/restore via win32clipboard (CF_DIB/CF_DIBV5,
    CF_HDROP, HTML/RTF, unicode text), snapshotting HGLOBAL formats as raw bytes
    and skipping GDI/handle + OS-synthesizable text formats.
  * Concrete-bytes paste (NOT delayed rendering): the transcript is on the
    clipboard the instant Ctrl+V fires.
  * WH_MOUSE_LL hooks with c_void_p restype/argtypes (so 64-bit handles aren't
    truncated): the LEFT+RIGHT chord with hold-and-forward (suppress both
    buttons, replay normal clicks, commit drags/holds) and single suppressed
    mouse buttons (mouse:x1/x2/middle via WM_XBUTTON 0x020B/0x020C, XBUTTON id
    in HIWORD(mouseData)).

The factory in voiceflow.platform.__init__ selects this module on Windows.
"""

import os
import re
import time
import queue
import logging
import threading
import traceback
import ctypes
from ctypes import wintypes

import keyboard

from .base import (
    HotkeyBackend, MouseBackend, ClipboardBackend, Paster, Permissions, Typer,
    TriggerBackend, TriggerHandle as _TriggerHandleABC,
)

log = logging.getLogger("voiceflow.platform.windows")

# 'mouse' library is optional (only used as a convenience for click coords; the
# actual single-button suppression uses our own WH_MOUSE_LL hook).
try:
    import mouse as _mouse  # noqa: F401  (kept for availability detection)
    _HAVE_MOUSE = True
except Exception:
    _mouse = None
    _HAVE_MOUSE = False

# pywin32 clipboard (required for all-format save/restore). If missing, degrade
# to text-only (loses non-text payloads) but keep running.
try:
    import win32clipboard as _wcb
    import win32con as _wcon
    import win32gui as _wgui
    import win32api as _wapi
    _HAVE_WIN32CLIP = True
except Exception:
    _wcb = _wcon = _wgui = _wapi = None
    _HAVE_WIN32CLIP = False


# ===========================================================================
# === Clipboard + SendInput paste (extracted from paste.py, verbatim) =======
# ===========================================================================

# ---------------------------------------------------------------------------
# kernel32 / user32 prototypes for raw clipboard memory + paste injection.
# ---------------------------------------------------------------------------
if os.name == "nt":
    _k32 = ctypes.windll.kernel32
    _k32.GlobalAlloc.restype = wintypes.HGLOBAL
    _k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    _k32.GlobalLock.restype = wintypes.LPVOID
    _k32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    _k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    _k32.GlobalUnlock.restype = wintypes.BOOL
    _k32.GlobalFree.restype = wintypes.HGLOBAL
    _k32.GlobalFree.argtypes = [wintypes.HGLOBAL]

GHND = 0x0042  # GMEM_MOVEABLE | GMEM_ZEROINIT (matches the Win32 contract)

# Clipboard format ids (defined even if win32con is missing).
CF_TEXT = 1
CF_BITMAP = 2
CF_METAFILEPICT = 3
CF_SYLK = 4
CF_DIF = 5
CF_TIFF = 6
CF_OEMTEXT = 7
CF_DIB = 8
CF_PALETTE = 9
CF_PENDATA = 10
CF_RIFF = 11
CF_WAVE = 12
CF_UNICODETEXT = 13
CF_ENHMETAFILE = 14
CF_HDROP = 15
CF_LOCALE = 16
CF_DIBV5 = 17
CF_OWNERDISPLAY = 0x0080
CF_DSPTEXT = 0x0081
CF_DSPBITMAP = 0x0082
CF_DSPMETAFILEPICT = 0x0083
CF_DSPENHMETAFILE = 0x008E

# Formats that are GDI/handle objects (NOT HGLOBAL) -> never GlobalLock these.
_NON_HGLOBAL_FORMATS = {
    CF_BITMAP, CF_PALETTE, CF_METAFILEPICT, CF_ENHMETAFILE, CF_OWNERDISPLAY,
    CF_DSPBITMAP, CF_DSPMETAFILEPICT, CF_DSPENHMETAFILE,
}
# OS auto-synthesizes these from CF_UNICODETEXT -> don't snapshot/restore them.
_SYNTH_TEXT_FORMATS = {CF_TEXT, CF_OEMTEXT, CF_LOCALE}


# ---------------------------------------------------------------------------
# Raw SendInput Ctrl+V (clears stuck modifiers first). 40-byte INPUT.
# ---------------------------------------------------------------------------
if os.name == "nt":
    _u32 = ctypes.WinDLL("user32", use_last_error=True)
    _u32.GetAsyncKeyState.restype = ctypes.c_short
    _KEYEVENTF_KEYUP = 0x0002
    _INPUT_KEYBOARD = 1
    _VK_CONTROL, _VK_SHIFT, _VK_MENU, _VK_LWIN, _VK_RWIN = 0x11, 0x10, 0x12, 0x5B, 0x5C
    _VK_V = 0x56

    # ULONG_PTR is pointer-sized: 8 bytes on 64-bit Python, 4 on 32-bit.
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        _ULONG_PTR = ctypes.c_ulonglong
    else:
        _ULONG_PTR = ctypes.c_ulong

    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                    ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", _ULONG_PTR)]

    class _KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", _ULONG_PTR)]

    class _HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                    ("wParamH", wintypes.WORD)]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT),
                    ("hi", _HARDWAREINPUT)]

    class _INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]

    _u32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int)
    _u32.SendInput.restype = wintypes.UINT

    def _ev(vk, up=False):
        flags = _KEYEVENTF_KEYUP if up else 0
        inp = _INPUT()
        inp.type = _INPUT_KEYBOARD
        inp.u.ki = _KEYBDINPUT(vk, 0, flags, 0, 0)
        return inp

    def _send_ctrl_v_sendinput():
        """Release any stuck modifier, then send Ctrl+V. Returns the number of
        events SendInput actually injected for the Ctrl+V (0 == blocked)."""
        ups = [_ev(vk, up=True) for vk in
               (_VK_CONTROL, _VK_SHIFT, _VK_MENU, _VK_LWIN, _VK_RWIN)
               if _u32.GetAsyncKeyState(vk) & 0x8000]
        if ups:
            arr = (_INPUT * len(ups))(*ups)
            _u32.SendInput(len(arr), arr, ctypes.sizeof(_INPUT))
            time.sleep(0.01)  # let the modifier key-ups register first
        seq = [_ev(_VK_CONTROL), _ev(_VK_V),
               _ev(_VK_V, up=True), _ev(_VK_CONTROL, up=True)]
        arr = (_INPUT * len(seq))(*seq)
        return _u32.SendInput(len(arr), arr, ctypes.sizeof(_INPUT))

    # Mouse-button synthesis (used by MouseChordHook to "replay" a click it held
    # back). Injected events carry LLMHF_INJECTED so our own hook ignores them.
    _MOUSEEVENTF_LEFTDOWN = 0x0002
    _MOUSEEVENTF_LEFTUP = 0x0004
    _MOUSEEVENTF_RIGHTDOWN = 0x0008
    _MOUSEEVENTF_RIGHTUP = 0x0010
    _MOUSEEVENTF_MIDDLEDOWN = 0x0020
    _MOUSEEVENTF_MIDDLEUP = 0x0040
    _INPUT_MOUSE = 0

    def _synth_mouse(flags):
        inp = _INPUT()
        inp.type = _INPUT_MOUSE
        inp.u.mi = _MOUSEINPUT(0, 0, 0, flags, 0, 0)
        arr = (_INPUT * 1)(inp)
        _u32.SendInput(1, arr, ctypes.sizeof(_INPUT))

    # ---- Unicode typing (streaming mode) -----------------------------------
    # SendInput with KEYEVENTF_UNICODE types arbitrary characters regardless of
    # the user's keyboard layout, and never produces an Enter key.
    _KEYEVENTF_UNICODE = 0x0004

    def _utf16_units(ch):
        """UTF-16 code unit(s) for a character (surrogate pair if > U+FFFF)."""
        cp = ord(ch)
        if cp <= 0xFFFF:
            return (cp,)
        cp -= 0x10000
        return (0xD800 + (cp >> 10), 0xDC00 + (cp & 0x3FF))

    def _ev_unicode(unit, up=False):
        flags = _KEYEVENTF_UNICODE | (_KEYEVENTF_KEYUP if up else 0)
        inp = _INPUT()
        inp.type = _INPUT_KEYBOARD
        inp.u.ki = _KEYBDINPUT(0, unit, flags, 0, 0)
        return inp

    def _release_held_modifiers():
        """Drop any modifier still physically held (from the trigger) so typed
        characters aren't interpreted as shortcuts."""
        ups = [_ev(vk, up=True) for vk in
               (_VK_CONTROL, _VK_SHIFT, _VK_MENU, _VK_LWIN, _VK_RWIN)
               if _u32.GetAsyncKeyState(vk) & 0x8000]
        if ups:
            arr = (_INPUT * len(ups))(*ups)
            _u32.SendInput(len(arr), arr, ctypes.sizeof(_INPUT))

    def _type_text_sendinput(text, chunk=400):
        """Type `text` as Unicode key events. Returns events injected."""
        seq = []
        for ch in text:
            if ch in ("\r", "\n"):
                continue  # SAFETY: never synthesize Enter.
            for unit in _utf16_units(ch):
                seq.append(_ev_unicode(unit, up=False))
                seq.append(_ev_unicode(unit, up=True))
        sent = 0
        for i in range(0, len(seq), chunk):
            part = seq[i:i + chunk]
            arr = (_INPUT * len(part))(*part)
            sent += _u32.SendInput(len(part), arr, ctypes.sizeof(_INPUT))
        return sent
else:  # pragma: no cover
    _u32 = None
    _MOUSEEVENTF_LEFTDOWN = _MOUSEEVENTF_LEFTUP = 0
    _MOUSEEVENTF_RIGHTDOWN = _MOUSEEVENTF_RIGHTUP = 0
    _MOUSEEVENTF_MIDDLEDOWN = _MOUSEEVENTF_MIDDLEUP = 0

    def _send_ctrl_v_sendinput():
        raise OSError("SendInput unavailable on non-Windows")

    def _synth_mouse(flags):
        raise OSError("SendInput unavailable on non-Windows")

    def _release_held_modifiers():
        raise OSError("SendInput unavailable on non-Windows")

    def _type_text_sendinput(text, chunk=400):
        raise OSError("SendInput unavailable on non-Windows")


def _log_focus_diag():
    """Log what window is focused and which modifiers are still held at paste
    time -- the two things that silently break a paste."""
    if os.name != "nt":
        return
    try:
        hwnd = _u32.GetForegroundWindow()
        buf = ctypes.create_unicode_buffer(256)
        _u32.GetWindowTextW(hwnd, buf, 256)
        held = [n for n, vk in (("ctrl", _VK_CONTROL), ("alt", _VK_MENU),
                                ("shift", _VK_SHIFT), ("win", _VK_LWIN))
                if _u32.GetAsyncKeyState(vk) & 0x8000]
        log.info("paste target: hwnd=%s title=%r modifiers_held=%s",
                 hwnd, buf.value, held or "none")
    except Exception:
        pass


def send_paste():
    """Clear any modifier still physically held from the trigger hotkey (the #1
    cause of a garbled/failed paste -> e.g. Ctrl+Alt+V), THEN Ctrl+V.

    On Windows we use SendInput first; if it reports it injected nothing (0 ->
    blocked by UIPI / no interactive desktop), fall back to the keyboard lib.
    keyboard.send is also the non-Windows path."""
    if os.name == "nt":
        _log_focus_diag()
        try:
            n = _send_ctrl_v_sendinput()
            if n and n > 0:
                return
            err = ctypes.get_last_error()
            log.warning("SendInput injected 0 events (blocked; GetLastError=%s). "
                        "Common cause: the focused window is an elevated/admin "
                        "app -- run OpenVerba as admin. Falling back to "
                        "keyboard.send.", err)
        except Exception as exc:
            log.warning("SendInput Ctrl+V failed (%s); trying keyboard.send", exc)
    try:
        for mod in ("ctrl", "alt", "shift", "windows"):
            try:
                keyboard.release(mod)
            except Exception:
                pass
        keyboard.send("ctrl+v")
        log.info("Paste sent via keyboard.send fallback.")
    except Exception as exc:
        log.warning("keyboard ctrl+v failed: %s", exc)


# ---------------------------------------------------------------------------
# ClipboardManager: all-format save/restore + the full paste cycle.
# ---------------------------------------------------------------------------
class ClipboardManager:
    """Serializes all clipboard access under one lock. The paste cycle sets the
    transcript as concrete clipboard data, sends Ctrl+V, waits, then restores
    the saved clipboard."""

    def __init__(self, restore_delay_ms=200, read_timeout_ms=2500):
        self.restore_floor_s = max(restore_delay_ms / 1000.0, 0.0)
        self.read_timeout_s = max(read_timeout_ms / 1000.0, 0.3)
        self._lock = threading.RLock()

    # ---- low-level helpers ----
    def _open(self, retries=25, delay=0.02):
        """OpenClipboard(NULL) with retries AND ownership verification. Some
        pywin32 versions don't reliably raise on transient denial, so we confirm
        before trusting it."""
        last = None
        for _ in range(retries):
            try:
                _wcb.OpenClipboard(0)
            except Exception as exc:
                last = exc
                time.sleep(delay)
                continue
            return True
        log.warning("OpenClipboard failed after retries: %s", last)
        return False

    @staticmethod
    def _alloc_hglobal(data):
        size = len(data) if data else 1
        h = _k32.GlobalAlloc(GHND, size)
        if not h:
            raise MemoryError("GlobalAlloc failed")
        p = _k32.GlobalLock(h)
        if not p:
            _k32.GlobalFree(h)
            raise MemoryError("GlobalLock failed")
        try:
            if data:
                ctypes.memmove(p, data, len(data))
        finally:
            _k32.GlobalUnlock(h)
        return h

    @staticmethod
    def _set_clip_bytes(fmt, data):
        """SetClipboardData via a self-owned GHND handle. On success the OS owns
        the handle. On failure we MUST free it (else it leaks every restore)."""
        h = ClipboardManager._alloc_hglobal(data)
        try:
            _wcb.SetClipboardData(fmt, h)
        except Exception:
            try:
                _k32.GlobalFree(h)
            except Exception:
                pass
            raise

    # ---- save ----
    def save(self):
        """Snapshot safe clipboard formats as raw bytes -> {format_id: bytes}.
        Returns {} on failure (caller still functions, just can't restore)."""
        if not _HAVE_WIN32CLIP:
            return self._save_text_only()
        saved = {}
        with self._lock:
            if not self._open():
                return saved
            try:
                present = set()
                fmt = _wcb.EnumClipboardFormats(0)
                while fmt:
                    present.add(fmt)
                    fmt = _wcb.EnumClipboardFormats(fmt)
                have_unicode = CF_UNICODETEXT in present
                for fmt in present:
                    # Skip GDI/handle formats: GlobalLock on these crashes.
                    if fmt in _NON_HGLOBAL_FORMATS:
                        continue
                    # Skip OS-synthesizable text formats if the source is present.
                    if have_unicode and fmt in _SYNTH_TEXT_FORMATS:
                        continue
                    try:
                        data = None
                        try:
                            h = _wcb.GetClipboardDataHandle(fmt)
                            if h:
                                raw = _wcb.GetGlobalMemory(h)
                                if raw is not None:
                                    data = bytes(raw)
                        except Exception:
                            data = None
                        # Fallback for CF_UNICODETEXT only: capture as bytes.
                        if data is None and fmt == CF_UNICODETEXT:
                            try:
                                val = _wcb.GetClipboardData(CF_UNICODETEXT)
                                if isinstance(val, str):
                                    data = val.encode("utf-16-le") + b"\x00\x00"
                                elif isinstance(val, (bytes, bytearray)):
                                    data = bytes(val)
                            except Exception:
                                data = None
                        # Any other format that didn't yield raw bytes is not
                        # round-trippable -> skip rather than corrupt it.
                        if data is not None:
                            saved[fmt] = data
                    except Exception:
                        pass  # never let one bad format abort the snapshot
            finally:
                try:
                    _wcb.CloseClipboard()
                except Exception:
                    pass
        return saved

    def _save_text_only(self):
        try:
            import pyperclip
            return {"__text__": pyperclip.paste()}
        except Exception:
            return {}

    def _set_text_immediate(self, text):
        """Set the transcript as concrete CF_UNICODETEXT bytes."""
        data = text.encode("utf-16-le") + b"\x00\x00"
        if not _HAVE_WIN32CLIP:
            try:
                import pyperclip
                pyperclip.copy(text)
            except Exception as exc:
                log.warning("set_text fallback failed: %s", exc)
            return
        with self._lock:
            if not self._open():
                return
            try:
                _wcb.EmptyClipboard()
                self._set_clip_bytes(CF_UNICODETEXT, data)
            finally:
                try:
                    _wcb.CloseClipboard()
                except Exception:
                    pass

    # ---- restore ----
    def restore(self, saved):
        if not _HAVE_WIN32CLIP:
            self._restore_text_only(saved)
            return
        with self._lock:
            if not self._open():
                return
            try:
                _wcb.EmptyClipboard()
                for fmt, data in (saved or {}).items():
                    try:
                        if isinstance(data, str):
                            # Only ever stored for CF_UNICODETEXT; restore exact.
                            b = data.encode("utf-16-le") + b"\x00\x00"
                            self._set_clip_bytes(CF_UNICODETEXT, b)
                        else:
                            self._set_clip_bytes(fmt, data)
                    except Exception:
                        continue  # one bad format must not kill the rest
            finally:
                try:
                    _wcb.CloseClipboard()
                except Exception:
                    pass

    def _restore_text_only(self, saved):
        try:
            import pyperclip
            pyperclip.copy((saved or {}).get("__text__", "") or "")
        except Exception:
            pass

    def _clear(self):
        """Proactively clear the clipboard (used when we couldn't snapshot the
        original, so dictated text doesn't linger in Clipboard History)."""
        if not _HAVE_WIN32CLIP:
            return
        with self._lock:
            if not self._open():
                return
            try:
                _wcb.EmptyClipboard()
            finally:
                try:
                    _wcb.CloseClipboard()
                except Exception:
                    pass

    # ---- the full paste cycle ----
    def paste_text(self, text):
        """Save full clipboard -> set the transcript as concrete clipboard data
        -> Ctrl+V -> brief settle so the target finishes pasting -> restore the
        original clipboard (in finally, so the original is always put back).

        NOTE: we deliberately do NOT use clipboard "delayed rendering" here. It
        relies on the OS sending WM_RENDERFORMAT to the thread that owns the
        helper window and that thread pumping messages at the right moment; in
        this app the listener blocks while paste runs on the worker thread, so
        the message is never dispatched and the target receives nothing. Setting
        concrete bytes is simple and reliable: the data is on the clipboard the
        instant Ctrl+V fires.

        Returns True if we put the text on the clipboard and sent the paste."""
        if not text:
            return False
        saved = self.save()
        try:
            self._set_text_immediate(text)
            time.sleep(0.03)            # let the clipboard settle before paste
            send_paste()
            # Give the target app time to actually read the clipboard before we
            # put the original back. Floor at 150ms; browsers/Electron can be
            # slower. Tune via clipboard_restore_delay_ms.
            time.sleep(max(self.restore_floor_s, 0.15))
        finally:
            if saved:
                self.restore(saved)
            else:
                # Nothing to restore; clear our transcript so it doesn't persist
                # in Clipboard History.
                self._clear()
        return True


# ===========================================================================
# === Triggers: keyboard combo + mouse hooks (extracted from hotkeys.py) ====
# ===========================================================================

# ---------------------------------------------------------------------------
# Win32 message + constant definitions for the low-level mouse hook.
# ---------------------------------------------------------------------------
_WH_MOUSE_LL = 14
_WM_MOUSEMOVE = 0x0200
_WM_LBUTTONDOWN, _WM_LBUTTONUP = 0x0201, 0x0202
_WM_RBUTTONDOWN, _WM_RBUTTONUP = 0x0204, 0x0205
_WM_MBUTTONDOWN, _WM_MBUTTONUP = 0x0207, 0x0208
_WM_XBUTTONDOWN, _WM_XBUTTONUP = 0x020B, 0x020C
_WM_QUIT = 0x0012
_XBUTTON1, _XBUTTON2 = 1, 2  # value in HIWORD(mouseData)
_LLMHF_INJECTED = 0x00000001


class _MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", wintypes.POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


def _xbutton_from_data(mouse_data):
    """Extract the XBUTTON id (1 or 2) from HIWORD(mouseData)."""
    return (mouse_data >> 16) & 0xFFFF


# ---------------------------------------------------------------------------
# Low-level mouse hook base: runs a message loop on its own thread, installs a
# WH_MOUSE_LL hook, and dispatches button events to a subclass _on_event().
# ---------------------------------------------------------------------------
class _MouseHookBase:
    def __init__(self):
        self._thread = None
        self._thread_id = None
        self._hook = None
        self._proc = None
        self._stop = False

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="mousehook")
        self._thread.start()

    def stop(self):
        self._stop = True
        if self._thread_id:
            try:
                ctypes.windll.user32.PostThreadMessageW(
                    self._thread_id, _WM_QUIT, 0, 0)
            except Exception:
                pass

    # subclasses override: return True to SUPPRESS this event.
    def _on_event(self, msg, mouse_data):
        return False

    def _run(self):
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._thread_id = kernel32.GetCurrentThreadId()
        LRESULT = ctypes.c_ssize_t
        HOOKPROC = ctypes.CFUNCTYPE(LRESULT, ctypes.c_int,
                                    wintypes.WPARAM, wintypes.LPARAM)
        # Correct prototypes so 64-bit handles aren't truncated to 32-bit ints
        # (the cause of SetWindowsHookExW "returning" 0 with no error).
        user32.SetWindowsHookExW.restype = ctypes.c_void_p
        user32.SetWindowsHookExW.argtypes = (ctypes.c_int, HOOKPROC,
                                             ctypes.c_void_p, wintypes.DWORD)
        user32.CallNextHookEx.restype = LRESULT
        user32.CallNextHookEx.argtypes = (ctypes.c_void_p, ctypes.c_int,
                                          wintypes.WPARAM, wintypes.LPARAM)
        user32.UnhookWindowsHookEx.restype = wintypes.BOOL
        user32.UnhookWindowsHookEx.argtypes = (ctypes.c_void_p,)
        kernel32.GetModuleHandleW.restype = ctypes.c_void_p
        kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)

        def proc(nCode, wParam, lParam):
            if nCode == 0:
                try:
                    info = ctypes.cast(
                        lParam, ctypes.POINTER(_MSLLHOOKSTRUCT)).contents
                    mouse_data = info.mouseData
                except Exception:
                    mouse_data = 0
                try:
                    if self._on_event(wParam, mouse_data):
                        return 1
                except Exception:
                    log.error("mouse hook handler error:\n%s",
                              traceback.format_exc())
            return user32.CallNextHookEx(None, nCode, wParam, lParam)

        self._proc = HOOKPROC(proc)  # keep a ref so it isn't GC'd
        self._hook = user32.SetWindowsHookExW(
            _WH_MOUSE_LL, self._proc, kernel32.GetModuleHandleW(None), 0)
        if not self._hook:
            log.error("Failed to install mouse hook (err=%s)",
                      ctypes.get_last_error())
            return
        log.info("Mouse hook installed (%s).", type(self).__name__)
        msg = wintypes.MSG()
        while not self._stop:
            r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if r in (0, -1):
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        try:
            user32.UnhookWindowsHookEx(self._hook)
        except Exception:
            pass
        self._hook = None


# Chord-able buttons (left/right/middle). x1/x2 are available as single-button
# triggers; in a chord they'd need XBUTTON mouseData on replay, so they're not
# offered as chord members.
_CHORD_DOWN = {"left": _WM_LBUTTONDOWN, "right": _WM_RBUTTONDOWN,
               "middle": _WM_MBUTTONDOWN}
_CHORD_UP = {"left": _WM_LBUTTONUP, "right": _WM_RBUTTONUP,
             "middle": _WM_MBUTTONUP}
_CHORD_SYNTH_DOWN = {"left": _MOUSEEVENTF_LEFTDOWN,
                     "right": _MOUSEEVENTF_RIGHTDOWN,
                     "middle": _MOUSEEVENTF_MIDDLEDOWN}
_CHORD_SYNTH_UP = {"left": _MOUSEEVENTF_LEFTUP, "right": _MOUSEEVENTF_RIGHTUP,
                   "middle": _MOUSEEVENTF_MIDDLEUP}
_CHORDABLE = ("left", "right", "middle")


class MouseChordHook:
    """Fires `callback` when two mouse buttons (any pair of left/right/middle)
    are pressed together -- WITHOUT the stray first-click that switches
    browser/Notepad tabs. Default pair is left+right.

    Hold-and-forward: when the first button goes down we SUPPRESS it and hold it
    "pending" for a brief window (~60ms). Then:
      - the other button goes down within the window -> CHORD: fire the toggle
        and suppress both buttons entirely (no click, no context menu, no
        middle-paste/autoscroll, no tab switch).
      - the pending button is released first -> it was a normal click: replay it
        (synthesize down+up) so the app still gets a clean click.
      - the mouse moves a few px, or the window expires while still held ->
        press/drag: commit it (synthesize the down) so dragging and
        press-and-hold keep working.
    We only ever suppress an UP whose DOWN we suppressed, so no button gets
    "stuck". Our own synthesized (injected) events are ignored by the hook.

    This is a standalone WH_MOUSE_LL hook (it needs cursor position + injected
    filtering, which the simple _MouseHookBase dispatch does not provide).
    """
    _CHORD_WINDOW = 0.06     # s: max gap between the two presses to count as chord
    _MOVE_THRESH = 6         # px: movement that commits a pending press (drag)

    def __init__(self, callback, button_a="left", button_b="right"):
        self.callback = callback
        self._a = button_a
        self._b = button_b
        self._down = {_CHORD_DOWN[button_a]: button_a, _CHORD_DOWN[button_b]: button_b}
        self._up = {_CHORD_UP[button_a]: button_a, _CHORD_UP[button_b]: button_b}
        self._thread = None
        self._thread_id = None
        self._hook = None
        self._proc = None
        self._stop = False
        self._lock = threading.RLock()
        self._pending = None     # {"btn": <name>, "x": int, "y": int}
        self._timer = None
        self._owe_up = set()     # buttons whose UP we must suppress (chord-eaten)

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="mousechord")
        self._thread.start()

    def stop(self):
        self._stop = True
        self._cancel_timer()
        if self._thread_id:
            try:
                ctypes.windll.user32.PostThreadMessageW(
                    self._thread_id, _WM_QUIT, 0, 0)
            except Exception:
                pass

    def _fire(self):
        try:
            self.callback()
        except Exception:
            log.error("Mouse-chord callback error:\n%s", traceback.format_exc())

    # ---- pending-press timer ----
    def _cancel_timer(self):
        if self._timer is not None:
            try:
                self._timer.cancel()
            except Exception:
                pass
            self._timer = None

    def _start_timer(self):
        self._cancel_timer()
        self._timer = threading.Timer(self._CHORD_WINDOW, self._on_timeout)
        self._timer.daemon = True
        self._timer.start()

    def _on_timeout(self):
        with self._lock:
            if self._pending is not None:
                self._commit_pending()  # still held & alone -> deliver the down

    def _commit_pending(self):
        if self._pending is None:
            return
        btn = self._pending["btn"]
        self._pending = None
        self._cancel_timer()
        _synth_mouse(_CHORD_SYNTH_DOWN[btn])

    # ---- state machine (all calls hold self._lock) ----
    def _on_down(self, btn, x, y):
        other = self._b if btn == self._a else self._a
        if self._pending is not None and self._pending["btn"] == other:
            self._pending = None
            self._cancel_timer()
            self._owe_up.update({self._a, self._b})
            self._fire()
            return True
        if self._pending is None:
            self._pending = {"btn": btn, "x": x, "y": y}
            self._start_timer()
            return True
        self._commit_pending()
        return False

    def _on_up(self, btn):
        if self._pending is not None and self._pending["btn"] == btn:
            self._pending = None
            self._cancel_timer()
            _synth_mouse(_CHORD_SYNTH_DOWN[btn])
            _synth_mouse(_CHORD_SYNTH_UP[btn])
            return True
        if btn in self._owe_up:
            self._owe_up.discard(btn)
            return True
        return False

    def _on_move(self, x, y):
        if self._pending is not None:
            if (abs(x - self._pending["x"]) > self._MOVE_THRESH or
                    abs(y - self._pending["y"]) > self._MOVE_THRESH):
                self._commit_pending()
        return False

    def _handle(self, msg, x, y):
        with self._lock:
            if msg in self._down:
                return self._on_down(self._down[msg], x, y)
            if msg in self._up:
                return self._on_up(self._up[msg])
            if msg == _WM_MOUSEMOVE:
                return self._on_move(x, y)
        return False

    def _run(self):
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._thread_id = kernel32.GetCurrentThreadId()
        LRESULT = ctypes.c_ssize_t
        HOOKPROC = ctypes.CFUNCTYPE(LRESULT, ctypes.c_int,
                                    wintypes.WPARAM, wintypes.LPARAM)
        user32.SetWindowsHookExW.restype = ctypes.c_void_p
        user32.SetWindowsHookExW.argtypes = (ctypes.c_int, HOOKPROC,
                                             ctypes.c_void_p, wintypes.DWORD)
        user32.CallNextHookEx.restype = LRESULT
        user32.CallNextHookEx.argtypes = (ctypes.c_void_p, ctypes.c_int,
                                          wintypes.WPARAM, wintypes.LPARAM)
        user32.UnhookWindowsHookEx.restype = wintypes.BOOL
        user32.UnhookWindowsHookEx.argtypes = (ctypes.c_void_p,)
        kernel32.GetModuleHandleW.restype = ctypes.c_void_p
        kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)

        def proc(nCode, wParam, lParam):
            if nCode == 0:
                try:
                    info = ctypes.cast(
                        lParam, ctypes.POINTER(_MSLLHOOKSTRUCT)).contents
                    injected = bool(info.flags & _LLMHF_INJECTED)
                    if not injected and self._handle(wParam, info.pt.x, info.pt.y):
                        return 1
                except Exception:
                    log.error("Mouse-chord hook proc error:\n%s",
                              traceback.format_exc())
            return user32.CallNextHookEx(None, nCode, wParam, lParam)

        self._proc = HOOKPROC(proc)
        self._hook = user32.SetWindowsHookExW(
            _WH_MOUSE_LL, self._proc, kernel32.GetModuleHandleW(None), 0)
        if not self._hook:
            log.error("Failed to install mouse-chord hook (err=%s)",
                      ctypes.get_last_error())
            return
        log.info("Mouse-chord hook installed (%s+%s = toggle, hold-and-forward).",
                 self._a, self._b)
        msg = wintypes.MSG()
        while not self._stop:
            r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if r in (0, -1):
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        self._cancel_timer()
        try:
            user32.UnhookWindowsHookEx(self._hook)
        except Exception:
            pass
        self._hook = None


class SingleButtonHook(_MouseHookBase):
    """Fires `callback` on the DOWN of a single non-left/right mouse button
    (middle / x1 / x2), suppressing BOTH that button's down and up so no
    browser back/forward or middle-click action leaks. We track that we
    suppressed the down so we always suppress the matching up (no stuck state)."""

    def __init__(self, button, callback):
        super().__init__()
        self.button = button  # "middle" | "x1" | "x2"
        self.callback = callback
        self._down_suppressed = False

    def _fire(self):
        try:
            self.callback()
        except Exception:
            log.error("Mouse-button callback error:\n%s",
                      traceback.format_exc())

    def _matches(self, msg, mouse_data):
        if self.button == "middle":
            return msg in (_WM_MBUTTONDOWN, _WM_MBUTTONUP), \
                   msg == _WM_MBUTTONDOWN
        if self.button in ("x1", "x2"):
            if msg not in (_WM_XBUTTONDOWN, _WM_XBUTTONUP):
                return False, False
            want = _XBUTTON1 if self.button == "x1" else _XBUTTON2
            if _xbutton_from_data(mouse_data) != want:
                return False, False
            return True, msg == _WM_XBUTTONDOWN
        return False, False

    def _on_event(self, msg, mouse_data):
        is_ours, is_down = self._matches(msg, mouse_data)
        if not is_ours:
            return False
        if is_down:
            self._fire()
            self._down_suppressed = True
            return True
        # button UP: suppress only if we suppressed the matching down.
        if self._down_suppressed:
            self._down_suppressed = False
            return True
        return True  # also suppress orphan ups for our button


# ---------------------------------------------------------------------------
# TriggerHandle: a uniform "stop me" wrapper over keyboard / mouse triggers.
# ---------------------------------------------------------------------------
class TriggerHandle(_TriggerHandleABC):
    def __init__(self, kind, handle):
        self.kind = kind          # "keyboard" | "mouse" | "chord"
        self._handle = handle

    def stop(self):
        try:
            if self.kind in ("chord", "mouse", "taphold"):
                self._handle.stop()
            elif self.kind == "keyboard":
                keyboard.remove_hotkey(self._handle)
        except Exception:
            pass
        self._handle = None


_SINGLE_MOUSE_BUTTONS = ("middle", "x1", "x2")


def _normalize(trigger):
    return (trigger or "").strip()


class MultiChordHook(MouseChordHook):
    """One WH_MOUSE_LL hook recognizing MULTIPLE two-button chords that may SHARE
    a button (e.g. left+right AND left+middle). Same hold-and-forward behaviour as
    MouseChordHook, but each chord fires its own callback. Required because two
    separate chord hooks both watching 'left' would fight over that button.
    """

    def __init__(self, chords):
        # chords: list of (button_a, button_b, callback)
        self._cb = {}
        buttons = set()
        for a, b, cb in chords:
            self._cb[frozenset((a, b))] = cb
            buttons.update((a, b))
        first = chords[0]
        super().__init__(None, first[0], first[1])  # sets up the plumbing
        # Watch ALL buttons involved in any chord (not just the first pair).
        self._down = {_CHORD_DOWN[b]: b for b in buttons}
        self._up = {_CHORD_UP[b]: b for b in buttons}

    def _on_down(self, btn, x, y):
        if self._pending is not None:
            pair = frozenset((self._pending["btn"], btn))
            cb = self._cb.get(pair)
            if cb is not None:                 # the two held buttons form a chord
                self._pending = None
                self._cancel_timer()
                self._owe_up.update(pair)
                try:
                    cb()
                except Exception:
                    log.error("Mouse-chord callback error:\n%s",
                              traceback.format_exc())
                return True
            # Not a registered pair -> commit the pending one as a real click and
            # start fresh with this button (it may chord with the next one).
            self._commit_pending()
            self._pending = {"btn": btn, "x": x, "y": y}
            self._start_timer()
            return True
        self._pending = {"btn": btn, "x": x, "y": y}
        self._start_timer()
        return True


def register_chords(specs):
    """Register several mouse chords on ONE hook. specs = list of
    (trigger_str, callback) where each trigger_str is 'mouse:a+b'. Returns a
    TriggerHandle, or None if any spec is not a valid left/right/middle chord."""
    chords = []
    for trig, cb in specs:
        flat = _normalize(trig).replace(" ", "").lower()
        if not (flat.startswith("mouse:") and "+" in flat):
            return None
        parts = [{"wheel": "middle", "scroll": "middle",
                  "scrollwheel": "middle"}.get(p, p)
                 for p in flat.split(":", 1)[1].split("+") if p]
        if len(parts) != 2 or parts[0] == parts[1] or \
                not all(p in _CHORDABLE for p in parts):
            return None
        chords.append((parts[0], parts[1], cb))
    try:
        hook = MultiChordHook(chords)
        hook.start()
        return TriggerHandle("chord", hook)
    except Exception:
        log.error("Could not start multi-chord hook:\n%s", traceback.format_exc())
        return None


class TapHoldChordHook(MouseChordHook):
    """A left+right (or any pair) chord with TAP vs HOLD:
       - quick TAP  (both released before hold_seconds) -> on_tap()
       - HOLD       (both held >= hold_seconds)         -> on_hold_start(),
                    then on release                     -> on_hold_end()
    A more generous chord window (you can press the 2nd button a beat after the
    1st) makes it far easier to trigger than a simultaneous chord. Single-button
    clicks/drags still pass through via the inherited hold-and-forward."""
    _CHORD_WINDOW = 0.22       # gap allowed between the two presses (was 0.06)
    _HOLD_SECONDS = 0.7

    def __init__(self, on_tap, on_hold_start, on_hold_end,
                 button_a="left", button_b="right", hold_seconds=None):
        super().__init__(None, button_a, button_b)
        self.on_tap = on_tap
        self.on_hold_start = on_hold_start
        self.on_hold_end = on_hold_end
        if hold_seconds:
            self._HOLD_SECONDS = float(hold_seconds)
        self._chord = False
        self._hold_fired = False
        self._hold_timer = None

    def stop(self):
        self._cancel_hold()
        super().stop()

    def _cancel_hold(self):
        if self._hold_timer is not None:
            try:
                self._hold_timer.cancel()
            except Exception:
                pass
            self._hold_timer = None

    def _start_hold(self):
        self._cancel_hold()
        self._hold_timer = threading.Timer(self._HOLD_SECONDS, self._on_hold_timeout)
        self._hold_timer.daemon = True
        self._hold_timer.start()

    def _on_hold_timeout(self):
        with self._lock:
            if self._chord and not self._hold_fired:
                self._hold_fired = True
                self._safe(self.on_hold_start)

    def _safe(self, cb):
        try:
            if cb:
                cb()
        except Exception:
            log.error("Chord callback error:\n%s", traceback.format_exc())

    def _on_down(self, btn, x, y):
        other = self._b if btn == self._a else self._a
        if self._pending is not None and self._pending["btn"] == other:
            # Both buttons down -> chord engaged. Tap vs hold decided on release.
            self._pending = None
            self._cancel_timer()
            self._owe_up.update({self._a, self._b})
            self._chord = True
            self._hold_fired = False
            self._start_hold()
            return True
        if self._pending is None:
            self._pending = {"btn": btn, "x": x, "y": y}
            self._start_timer()
            return True
        self._commit_pending()
        self._pending = {"btn": btn, "x": x, "y": y}
        self._start_timer()
        return True

    def _on_up(self, btn):
        if self._chord:
            self._cancel_hold()
            if self._hold_fired:
                self._safe(self.on_hold_end)
            else:
                self._safe(self.on_tap)
            self._chord = False
            self._owe_up.discard(btn)
            return True
        if self._pending is not None and self._pending["btn"] == btn:
            self._pending = None
            self._cancel_timer()
            _synth_mouse(_CHORD_SYNTH_DOWN[btn])
            _synth_mouse(_CHORD_SYNTH_UP[btn])
            return True
        if btn in self._owe_up:
            self._owe_up.discard(btn)
            return True
        return False


def register_tap_hold(trigger, on_tap, on_hold_start, on_hold_end,
                      hold_seconds=None):
    """Register a tap/hold mouse chord. Returns a TriggerHandle or None."""
    flat = _normalize(trigger).replace(" ", "").lower()
    if not (flat.startswith("mouse:") and "+" in flat):
        return None
    parts = [{"wheel": "middle", "scroll": "middle",
              "scrollwheel": "middle"}.get(p, p)
             for p in flat.split(":", 1)[1].split("+") if p]
    if len(parts) != 2 or parts[0] == parts[1] or \
            not all(p in _CHORDABLE for p in parts):
        return None
    try:
        hook = TapHoldChordHook(on_tap, on_hold_start, on_hold_end,
                                parts[0], parts[1], hold_seconds)
        hook.start()
        return TriggerHandle("chord", hook)
    except Exception:
        log.error("Could not start tap/hold chord hook:\n%s",
                  traceback.format_exc())
        return None


# ---------------------------------------------------------------------------
# Self-healing low-level keyboard hook (the app's OWN WH_KEYBOARD_LL, like the
# mouse hooks above) -- replaces the fragile `keyboard`-library hotkey.
#
# THE BUG IT FIXES: the `keyboard` lib installs ONE global low-level hook on a
# background listener thread. Windows SILENTLY removes a low-level hook whenever
# a hook callback overruns the ~300ms LowLevelHooksTimeout (happens under
# GPU/transcription load or after the machine idles). The lib's listener thread
# kept running but received nothing, and re-adding the hotkey (resume()) could
# NOT reinstall the dead OS hook -- so the trigger went permanently dead until a
# full restart. (That is exactly the "Dictation resumed yet nothing fires" we
# saw.)
#
# This hook fixes that two ways:
#   1) PREVENT: the hook proc does only O(1) work (set membership + maybe spawn
#      a daemon thread) and returns immediately -- engine work NEVER runs inline
#      -- so Windows is far less likely to time it out in the first place.
#   2) CURE: a periodic self-heal. Every few seconds, WHILE THE USER IS IDLE (so
#      no keypress can be dropped), it installs a fresh hook then unhooks the old
#      one. If Windows ever did remove our hook, a live one is back within
#      seconds with zero user action. pause()/resume() also fully reinstall now.
# ---------------------------------------------------------------------------
_WH_KEYBOARD_LL = 13
_WM_KEYDOWN, _WM_KEYUP = 0x0100, 0x0101
_WM_SYSKEYDOWN, _WM_SYSKEYUP = 0x0104, 0x0105
_WM_TIMER = 0x0113
_LLKHF_INJECTED = 0x00000010

# Virtual-key codes per modifier group (generic + left/right variants).
_MOD_VKS = {
    "ctrl":    {0x11, 0xA2, 0xA3},
    "control": {0x11, 0xA2, 0xA3},
    "ctl":     {0x11, 0xA2, 0xA3},
    "shift":   {0x10, 0xA0, 0xA1},
    "alt":     {0x12, 0xA4, 0xA5},
    "menu":    {0x12, 0xA4, 0xA5},
    "win":     {0x5B, 0x5C},
    "windows": {0x5B, 0x5C},
    "super":   {0x5B, 0x5C},
    "meta":    {0x5B, 0x5C},
    "cmd":     {0x5B, 0x5C},
}

# Virtual-key codes for common non-modifier ("main") trigger keys.
_VK_BY_KEYNAME = {
    "space": 0x20, "spacebar": 0x20,
    "enter": 0x0D, "return": 0x0D,
    "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
    "backspace": 0x08, "delete": 0x2E, "del": 0x2E, "insert": 0x2D,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "capslock": 0x14, "printscreen": 0x2C, "scrolllock": 0x91, "pausebreak": 0x13,
    "`": 0xC0, "-": 0xBD, "=": 0xBB, "[": 0xDB, "]": 0xDD, "\\": 0xDC,
    ";": 0xBA, "'": 0xDE, ",": 0xBC, ".": 0xBE, "/": 0xBF,
}
_VK_BY_KEYNAME.update({"f%d" % i: 0x70 + (i - 1) for i in range(1, 25)})


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


class _KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


def _vks_for_main(name):
    """Resolve a non-modifier trigger key name -> (set of vkCodes, set of
    scancodes). Uses a vk table for common keys, ord() for a single a-z/0-9, and
    the keyboard lib's scancodes as a backstop. Empty sets => unresolvable."""
    name = (name or "").strip().lower()
    vks = set()
    if name in _VK_BY_KEYNAME:
        vks.add(_VK_BY_KEYNAME[name])
    if len(name) == 1 and name.isalnum():
        vks.add(ord(name.upper()))
    scans = set()
    try:
        scans = set(keyboard.key_to_scan_codes(name))
    except Exception:
        scans = set()
    return vks, scans


class KeyboardHookLL:
    """Tap/hold for a KEYBOARD combo on the app's OWN low-level hook -- the
    robust twin of the mouse hooks. TAP the combo -> on_tap (dictate); HOLD the
    whole combo >= hold_seconds -> on_hold_start, then on_hold_end on release
    (command / AI-edit). Only the combo's NON-modifier key (e.g. the Space of
    ctrl+shift+space) is suppressed so it never leaks into the focused app; the
    modifiers themselves pass through untouched. Self-heals (see comment above).

    Design rules that keep Windows from timing the hook out (and that survived an
    adversarial review):
      * The hook proc does ONLY O(1) state-machine work. It NEVER creates a
        thread inline -- engine callbacks are queued to ONE long-lived dispatcher
        thread via a non-blocking put (so order is preserved and there is no
        Thread.start()/_started.wait() on the hot key path), and hold-timing is a
        deadline checked from the hook's own message loop, not a per-press Timer.
      * Injected key events (the app's OWN SendInput paste/typing) are ignored,
        like the mouse hook, so they can't corrupt the combo state.
      * Per-press suppression discipline: a key UP is suppressed only if its DOWN
        was suppressed, and we engage only on a FRESH main-down -- so a
        passed-through key can never get a swallowed UP (no stuck key in the
        target app), and an auto-repeat can't sneak an engage in mid-press.
      * Self-heal is timer-independent (a MsgWaitForMultipleObjects-timed message
        loop, no SetTimer to silently fail) with an age-based force-reinstall and
        a GetAsyncKeyState reconcile, so a lost UP or a starved idle-guard can
        never wedge the trigger permanently.

    The state machine (`_on_key`) is pure and unit-tested directly."""

    _HOLD_SECONDS = 0.7
    _TICK_MS = 100                # message-loop wake cadence (hold + self-heal)
    _REINSTALL_MIN_MS = 5000      # min gap between idle self-heal reinstalls
    _REINSTALL_FORCE_MS = 30000   # reinstall by now even if never idle
    _IDLE_GUARD_MS = 700          # "user idle" threshold for the safe heal path

    def __init__(self, combo, on_tap, on_hold_start, on_hold_end,
                 hold_seconds=None):
        self.combo = combo
        self.on_tap = on_tap
        self.on_hold_start = on_hold_start
        self.on_hold_end = on_hold_end
        if hold_seconds:
            try:
                self._HOLD_SECONDS = max(0.2, float(hold_seconds))
            except Exception:
                pass
        # Parse "ctrl+shift+space" -> required modifier groups + one main key.
        toks = [t for t in combo.replace(" ", "").lower().split("+") if t]
        self._mod_groups = {}     # group-name -> set(vkCodes)
        main_name = None
        for t in toks:
            if t in _MOD_VKS:
                self._mod_groups[t] = _MOD_VKS[t]
            else:
                main_name = t     # last non-modifier token is the trigger key
        self._main_vks, self._main_scans = _vks_for_main(main_name)
        self._resolved = bool(self._main_vks or self._main_scans)

        self._down_vks = set()         # currently-down MODIFIER vks (relevant)
        self._main_held = False        # is the main key physically down now
        self._main_suppressed = False  # are we eating the CURRENT main press
        self._active = False           # combo engaged (tap pending or holding)
        self._hold_fired = False
        self._hold_deadline = None     # monotonic time at which HOLD fires
        self._lock = threading.RLock()
        self._dispatch_sync = False    # tests set True to run callbacks inline

        self._cbq = queue.Queue()      # callbacks -> single ordered dispatcher
        self._dispatcher = None
        self._thread = None
        self._thread_id = None
        self._hook = None
        self._proc = None
        self._stop = False
        self._install_count = 0
        self._last_install_tick = 0
        self._installed_evt = threading.Event()

    # ---- lifecycle ----
    def start(self):
        if not self._resolved:
            self._installed_evt.set()     # caller will fall back
            return
        self._dispatcher = threading.Thread(target=self._dispatch_loop,
                                            daemon=True, name="kbd-hook-cb")
        self._dispatcher.start()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="kbd-hook")
        self._thread.start()

    def installed_ok(self, timeout=1.5):
        self._installed_evt.wait(timeout)
        return self._hook is not None

    def stop(self):
        self._stop = True
        try:
            self._cbq.put_nowait(None)    # sentinel -> end the dispatcher
        except Exception:
            pass
        if self._thread_id:
            try:
                ctypes.windll.user32.PostThreadMessageW(
                    self._thread_id, _WM_QUIT, 0, 0)
            except Exception:
                pass

    # ---- callback dispatch: ONE long-lived thread, FIFO, never spawned in-proc
    def _safe(self, cb):
        try:
            cb()
        except Exception:
            log.error("keyboard hook callback error:\n%s",
                      traceback.format_exc())

    def _dispatch_loop(self):
        while True:
            cb = self._cbq.get()
            if cb is None:                # sentinel from stop()
                return
            self._safe(cb)

    def _fire(self, cb):
        """Enqueue a callback for the single dispatcher: non-blocking, no thread
        spawn, FIFO (so on_hold_start can't be overtaken by on_hold_end/on_tap)."""
        if cb is None:
            return
        if self._dispatch_sync:
            self._safe(cb)
        else:
            try:
                self._cbq.put_nowait(cb)
            except Exception:
                pass

    # ---- hold timing (a deadline checked from the message loop; no Timer) ----
    def _cancel_hold_timer(self):
        self._hold_deadline = None

    def _arm_hold(self):
        self._hold_deadline = time.monotonic() + self._HOLD_SECONDS

    def _on_hold_timeout(self):
        with self._lock:
            if self._active and not self._hold_fired and \
                    self._main_held and self._mods_satisfied():
                self._hold_fired = True
                self._fire(self.on_hold_start)

    def _check_hold(self):
        if self._active and not self._hold_fired and \
                self._hold_deadline is not None and \
                time.monotonic() >= self._hold_deadline:
            self._on_hold_timeout()

    # ---- combo state ----
    def _mods_satisfied(self):
        return all(any(v in self._down_vks for v in vks)
                   for vks in self._mod_groups.values())

    def _is_main(self, vk, scancode):
        return vk in self._main_vks or (scancode and scancode in self._main_scans)

    def _modgroup_vk(self, vk):
        for vks in self._mod_groups.values():
            if vk in vks:
                return True
        return False

    def _engage(self):
        self._active = True
        self._hold_fired = False
        self._arm_hold()

    def _disengage(self):
        if not self._active:
            return
        self._active = False
        self._cancel_hold_timer()
        if self._hold_fired:
            self._fire(self.on_hold_end)
        else:
            self._fire(self.on_tap)

    def _on_key(self, vk, scancode, is_down):
        """Return True to SUPPRESS this key event. Pure state machine."""
        with self._lock:
            is_main = self._is_main(vk, scancode)
            is_mod = self._modgroup_vk(vk)
            if is_mod:
                if is_down:
                    self._down_vks.add(vk)
                else:
                    self._down_vks.discard(vk)
            if is_main:
                if is_down:
                    if self._main_held:
                        # OS auto-repeat: keep doing whatever this press decided.
                        # We NEVER engage on a repeat -- that would suppress an UP
                        # whose earlier DOWNs were already delivered (stuck key).
                        return self._main_suppressed
                    self._main_held = True
                    if not self._active and self._mods_satisfied():
                        self._engage()
                        self._main_suppressed = True
                        return True
                    self._main_suppressed = False   # passed through to the app
                    return False
                # main key UP: suppress it iff we suppressed THIS press' DOWN
                self._main_held = False
                was = self._main_suppressed
                self._main_suppressed = False
                if self._active and was:
                    self._disengage()
                return was
            # A modifier was released -> may break an engaged combo (end it).
            if is_mod and not is_down and self._active and \
                    not self._mods_satisfied():
                self._disengage()
            return False

    # ---- physical-key reconcile (recover from a lost UP / wedge) ----
    def _phys_down(self, user32, vk):
        try:
            return bool(user32.GetAsyncKeyState(vk) & 0x8000)
        except Exception:
            return False

    def _relevant_vks(self):
        vks = set(self._main_vks)
        for s in self._mod_groups.values():
            vks |= s
        return vks

    def _reconcile(self, user32):
        """If NOTHING combo-relevant is physically down, clear all transient
        state. Guarantees a lost key-UP (swallowed by a UAC/secure desktop or a
        focus change) can't permanently wedge _main_suppressed/_active."""
        if any(self._phys_down(user32, vk) for vk in self._relevant_vks()):
            return                     # something still held -> leave it alone
        with self._lock:
            if self._active or self._main_suppressed or self._down_vks:
                self._active = False
                self._main_held = False
                self._main_suppressed = False
                self._hold_fired = False
                self._hold_deadline = None
                self._down_vks.clear()

    # ---- OS hook + self-heal ----
    def _idle_ms(self, user32, kernel32):
        li = _LASTINPUTINFO()
        li.cbSize = ctypes.sizeof(li)
        if not user32.GetLastInputInfo(ctypes.byref(li)):
            return 0xFFFFFFFF          # can't measure -> treat as idle (heal-safe)
        return (kernel32.GetTickCount() - li.dwTime) & 0xFFFFFFFF

    def _install(self, user32, kernel32):
        h = user32.SetWindowsHookExW(
            _WH_KEYBOARD_LL, self._proc, kernel32.GetModuleHandleW(None), 0)
        if not h:
            log.error("Failed to install keyboard hook (err=%s)",
                      ctypes.get_last_error())
            return False
        self._hook = h
        self._install_count += 1
        self._last_install_tick = kernel32.GetTickCount()
        if self._install_count == 1:
            log.info("Keyboard hook installed (LL self-healing, trigger=%s).",
                     self.combo)
        else:
            log.debug("Keyboard hook self-heal reinstall #%d.",
                      self._install_count)
        return True

    def _maybe_reinstall(self, user32, kernel32):
        age = (kernel32.GetTickCount() - self._last_install_tick) & 0xFFFFFFFF
        idle = self._idle_ms(user32, kernel32)
        idle_ok = idle >= self._IDLE_GUARD_MS
        # Heal when safely idle, OR force it by _REINSTALL_FORCE_MS so a user who
        # never pauses (continuous typing) still gets a fresh hook on schedule.
        if not ((idle_ok and age >= self._REINSTALL_MIN_MS) or
                age >= self._REINSTALL_FORCE_MS):
            return
        if idle_ok:
            self._reconcile(user32)         # only touch state when truly idle
        old = self._hook
        # Install NEW then unhook OLD on THIS thread (no message is pumped in
        # between, so the proc never runs against two hooks) -> no gap, no race.
        if self._install(user32, kernel32) and old:
            try:
                user32.UnhookWindowsHookEx(old)
            except Exception:
                pass

    def _run(self):
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._thread_id = kernel32.GetCurrentThreadId()
        LRESULT = ctypes.c_ssize_t
        HOOKPROC = ctypes.CFUNCTYPE(LRESULT, ctypes.c_int,
                                    wintypes.WPARAM, wintypes.LPARAM)
        user32.SetWindowsHookExW.restype = ctypes.c_void_p
        user32.SetWindowsHookExW.argtypes = (ctypes.c_int, HOOKPROC,
                                             ctypes.c_void_p, wintypes.DWORD)
        user32.CallNextHookEx.restype = LRESULT
        user32.CallNextHookEx.argtypes = (ctypes.c_void_p, ctypes.c_int,
                                          wintypes.WPARAM, wintypes.LPARAM)
        user32.UnhookWindowsHookEx.restype = wintypes.BOOL
        user32.UnhookWindowsHookEx.argtypes = (ctypes.c_void_p,)
        user32.GetLastInputInfo.restype = wintypes.BOOL
        user32.GetLastInputInfo.argtypes = (ctypes.POINTER(_LASTINPUTINFO),)
        user32.GetAsyncKeyState.restype = ctypes.c_short
        user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
        user32.PeekMessageW.restype = wintypes.BOOL
        user32.PeekMessageW.argtypes = (ctypes.POINTER(wintypes.MSG),
                                        ctypes.c_void_p, wintypes.UINT,
                                        wintypes.UINT, wintypes.UINT)
        user32.MsgWaitForMultipleObjects.restype = wintypes.DWORD
        user32.MsgWaitForMultipleObjects.argtypes = (
            wintypes.DWORD, ctypes.c_void_p, wintypes.BOOL,
            wintypes.DWORD, wintypes.DWORD)
        kernel32.GetModuleHandleW.restype = ctypes.c_void_p
        kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
        kernel32.GetTickCount.restype = wintypes.DWORD

        def proc(nCode, wParam, lParam):
            if nCode == 0:                       # HC_ACTION
                try:
                    info = ctypes.cast(
                        lParam, ctypes.POINTER(_KBDLLHOOKSTRUCT)).contents
                    # Ignore the app's OWN injected keys (SendInput paste/typing)
                    # so they can't corrupt combo state -- mirrors the mouse hook.
                    if not (info.flags & _LLKHF_INJECTED):
                        is_down = wParam in (_WM_KEYDOWN, _WM_SYSKEYDOWN)
                        is_up = wParam in (_WM_KEYUP, _WM_SYSKEYUP)
                        if (is_down or is_up) and \
                                self._on_key(info.vkCode, info.scanCode, is_down):
                            return 1             # SUPPRESS this key
                except Exception:
                    log.error("keyboard hook proc error:\n%s",
                              traceback.format_exc())
            return user32.CallNextHookEx(None, nCode, wParam, lParam)

        self._proc = HOOKPROC(proc)              # keep a ref so it isn't GC'd
        ok = self._install(user32, kernel32)
        self._installed_evt.set()
        if not ok:
            return
        # Pump messages so the OS services the low-level hook (suppression stays
        # synchronous -- the OS calls proc directly while we wait), waking every
        # _TICK_MS to check the hold deadline + run the self-heal. NO SetTimer.
        QS_ALLINPUT = 0x04FF
        PM_REMOVE = 0x0001
        msg = wintypes.MSG()
        while not self._stop:
            user32.MsgWaitForMultipleObjects(
                0, None, False, self._TICK_MS, QS_ALLINPUT)
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                if msg.message == _WM_QUIT:
                    self._stop = True
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            try:
                self._check_hold()
                self._maybe_reinstall(user32, kernel32)
            except Exception:
                log.error("keyboard hook tick error:\n%s",
                          traceback.format_exc())
        try:
            if self._hook:
                user32.UnhookWindowsHookEx(self._hook)
        except Exception:
            pass
        self._hook = None


class KeyboardTapHold:
    """Tap/hold for a KEYBOARD combo (e.g. "ctrl+shift+space"), the keyboard
    twin of TapHoldChordHook: a quick press+release fires on_tap (dictate);
    holding the WHOLE combo for >= hold_seconds fires on_hold_start, then
    on_hold_end when released (command / AI-edit mode). The combo is suppressed
    so it never leaks a stray space/keystroke into the focused app.

    Implementation: register the combo with the keyboard lib (suppress=True) to
    catch the press; a watcher thread then polls whether all the combo's keys
    are still physically down (keyboard.is_pressed records state even for
    suppressed keys) to time the hold and detect the release."""

    _HOLD_SECONDS = 0.7

    def __init__(self, combo, on_tap, on_hold_start, on_hold_end,
                 hold_seconds=None):
        self.combo = combo
        self.on_tap = on_tap
        self.on_hold_start = on_hold_start
        self.on_hold_end = on_hold_end
        if hold_seconds:
            try:
                self._HOLD_SECONDS = max(0.2, float(hold_seconds))
            except Exception:
                pass
        # Every key in the combo must stay down for it to count as "held".
        self._keys = [p for p in combo.replace(" ", "").lower().split("+") if p]
        self._handle = None
        self._active = False
        self._lock = threading.Lock()

    def start(self):
        try:
            self._handle = keyboard.add_hotkey(
                self.combo, self._on_press, suppress=True,
                trigger_on_release=False)
        except Exception:
            # Some environments reject suppress; fall back (combo may leak).
            self._handle = keyboard.add_hotkey(
                self.combo, self._on_press, trigger_on_release=False)

    def stop(self):
        h, self._handle = self._handle, None
        if h is not None:
            try:
                keyboard.remove_hotkey(h)
            except Exception:
                pass

    def _safe(self, cb):
        if cb is None:
            return
        try:
            cb()
        except Exception:
            log.error("keyboard tap/hold callback error:\n%s",
                      traceback.format_exc())

    def _still_held(self):
        try:
            return all(keyboard.is_pressed(k) for k in self._keys)
        except Exception:
            return False

    def _on_press(self):
        # Fires on combo-down (and again on OS key-repeat) -> debounce re-entry.
        with self._lock:
            if self._active:
                return
            self._active = True
        threading.Thread(target=self._watch, daemon=True,
                         name="kbd-taphold").start()

    def _watch(self):
        t0 = time.monotonic()
        hold_fired = False
        while self._still_held():
            if not hold_fired and (time.monotonic() - t0) >= self._HOLD_SECONDS:
                hold_fired = True
                self._safe(self.on_hold_start)
            time.sleep(0.02)
            if time.monotonic() - t0 > 30.0:   # safety: never spin forever
                break
        if hold_fired:
            self._safe(self.on_hold_end)
        else:
            self._safe(self.on_tap)
        self._active = False


def register_tap_hold_keyboard(trigger, on_tap, on_hold_start, on_hold_end,
                               hold_seconds=None):
    """Register a tap/hold KEYBOARD combo. Prefers the app's OWN self-healing
    low-level hook (KeyboardHookLL) -- which survives the Windows hook-timeout
    death that silently killed the `keyboard`-library hotkey -- and falls back
    to the keyboard-library tap/hold if the own-hook can't install or the
    trigger can't be mapped to a key. Returns a TriggerHandle or None."""
    hk = _normalize(trigger)
    if not hk:
        return None
    flat = hk.replace(" ", "").lower()
    if flat.startswith("mouse:"):   # mouse triggers use register_tap_hold
        return None
    # Primary: own low-level hook (self-healing; suppresses only the main key).
    try:
        ll = KeyboardHookLL(hk, on_tap, on_hold_start, on_hold_end, hold_seconds)
        ll.start()
        if ll.installed_ok(timeout=1.5):
            return TriggerHandle("taphold", ll)
        ll.stop()
        log.warning("Own keyboard hook unavailable for '%s'; using keyboard "
                    "library fallback.", hk)
    except Exception:
        log.error("Own keyboard hook failed for '%s'; falling back:\n%s",
                  hk, traceback.format_exc())
    # Fallback: original keyboard-library tap/hold.
    try:
        hook = KeyboardTapHold(hk, on_tap, on_hold_start, on_hold_end,
                               hold_seconds)
        hook.start()
        return TriggerHandle("taphold", hook)
    except Exception:
        log.error("Could not start keyboard tap/hold hook:\n%s",
                  traceback.format_exc())
        return None


def register_trigger(trigger, callback):
    """(Re)register the global trigger. Returns a TriggerHandle on success, or
    None. Caller should .stop() the previous handle before registering a new
    one."""
    hk = _normalize(trigger)
    if not hk:
        return None

    flat = hk.replace(" ", "").lower()

    # A two-button mouse chord (any pair of left/right/middle) -> raw low-level
    # hook (handles its own hold-and-forward suppression).
    if flat.startswith("mouse:") and "+" in flat:
        spec = flat.split(":", 1)[1]
        parts = [{"wheel": "middle", "scroll": "middle",
                  "scrollwheel": "middle"}.get(p, p)
                 for p in spec.split("+") if p]
        if len(parts) == 2 and parts[0] != parts[1] and \
                all(p in _CHORDABLE for p in parts):
            try:
                hook = MouseChordHook(callback, parts[0], parts[1])
                hook.start()
                return TriggerHandle("chord", hook)
            except Exception:
                log.error("Could not start mouse-chord hook:\n%s",
                          traceback.format_exc())
                return None
        log.error("Unsupported mouse chord '%s' (use a pair of "
                  "left/right/middle, e.g. mouse:left+middle).", hk)
        return None

    # Single mouse button (middle / x1 / x2) -> our suppressing WH_MOUSE_LL hook.
    if hk.lower().startswith("mouse:"):
        btn = hk.split(":", 1)[1].strip().lower()
        # Accept some aliases.
        btn = {"x": "x1", "back": "x1", "forward": "x2"}.get(btn, btn)
        if btn not in _SINGLE_MOUSE_BUTTONS:
            log.error("Unsupported mouse trigger '%s' (use middle/x1/x2 or "
                      "left+right).", hk)
            return None
        try:
            hook = SingleButtonHook(btn, callback)
            hook.start()
            return TriggerHandle("mouse", hook)
        except Exception:
            log.error("Could not register mouse button '%s':\n%s",
                      btn, traceback.format_exc())
            return None

    # Keyboard combo (suppress so the combo doesn't leak into the focused app).
    try:
        handle = keyboard.add_hotkey(hk, callback, suppress=True,
                                     trigger_on_release=False)
        return TriggerHandle("keyboard", handle)
    except Exception as exc:
        log.warning("add_hotkey('%s') with suppress failed (%s); retrying "
                    "without suppress.", hk, exc)
        try:
            handle = keyboard.add_hotkey(hk, callback, trigger_on_release=False)
            return TriggerHandle("keyboard", handle)
        except Exception:
            log.error("Could not register trigger '%s':\n%s",
                      hk, traceback.format_exc())
            return None


# ---------------------------------------------------------------------------
# Classification + presets for the GUI picker.
# ---------------------------------------------------------------------------
_MOUSE_LABELS = {
    "mouse:middle": "Middle mouse button",
    "mouse:x1": "Mouse side button (back / thumb 1)",
    "mouse:x2": "Mouse side button (forward / thumb 2)",
    "mouse:left+right": "Left + Right click (chord)",
    "mouse:left+middle": "Left click + Middle (wheel) press (chord)",
    "mouse:right+middle": "Right + Middle (wheel) press (chord)",
}

# Conflict-prone triggers: technically work but cause stray clicks / lost
# right-clicks / tab jumps. Warned about in the GUI.
_CONFLICT_PRONE = {
    "mouse:left+right": ("Left+Right chord can fire on normal clicking and "
                         "swallows your right-click context menu while held."),
    "mouse:left+middle": ("Left+Middle chord uses your left button, so a quick "
                          "click still registers where you point; the middle "
                          "press is suppressed."),
    "mouse:right+middle": ("Right+Middle chord uses your right button; a quick "
                           "right-click still registers."),
    "mouse:middle": ("Middle click is also paste-on-Linux / open-in-new-tab in "
                     "browsers; using it here suppresses that everywhere."),
}

PRESETS = [
    {"trigger": "mouse:x1", "label": _MOUSE_LABELS["mouse:x1"],
     "clean": True,
     "note": "A thumb/side button is the most ergonomic, conflict-free choice."},
    {"trigger": "mouse:x2", "label": _MOUSE_LABELS["mouse:x2"],
     "clean": True, "note": "The other thumb/side button."},
    {"trigger": "f9", "label": "F9 key", "clean": True,
     "note": "A free function key that rarely clashes with apps."},
    {"trigger": "ctrl+shift+space", "label": "Ctrl + Shift + Space",
     "clean": True, "note": "A safe global combo (the default)."},
    {"trigger": "ctrl+alt+d", "label": "Ctrl + Alt + D", "clean": True,
     "note": "Another safe global combo."},
]


def classify_trigger(trigger):
    """Return {"trigger","label","clean","warning"} for a trigger string."""
    hk = _normalize(trigger)
    flat = hk.replace(" ", "").lower()
    # Canonicalize a mouse chord's button order (left, right, middle).
    if flat.startswith("mouse:") and "+" in flat:
        spec = flat.split(":", 1)[1]
        parts = [{"wheel": "middle", "scroll": "middle"}.get(p, p)
                 for p in spec.split("+") if p]
        order = {"left": 0, "right": 1, "middle": 2}
        parts = sorted(parts, key=lambda p: order.get(p, 9))
        flat = "mouse:" + "+".join(parts)
    label = _MOUSE_LABELS.get(flat)
    if label is None and flat.startswith("mouse:") and "+" in flat:
        label = flat.split(":", 1)[1].replace("+", " + ").title() + " (chord)"
    label = label or (hk if hk else "(none)")
    warning = _CONFLICT_PRONE.get(flat)
    clean = warning is None
    return {"trigger": hk, "label": label, "clean": clean, "warning": warning}


# ---------------------------------------------------------------------------
# TriggerRecorder: live-detect the user's next trigger for the GUI picker.
#
# It hooks the keyboard (keyboard.read_hotkey on a worker thread) AND a
# low-level mouse hook that reports ANY recognizable mouse button -- including
# left/right/middle/x1/x2 -- WITHOUT permanently suppressing the user's normal
# mouse use (it only swallows the one event it reports, then stops). This lets
# the GUI show, in real time, exactly what was detected. If the user clicks an
# undetectable button (e.g. an RGB/profile button that emits no standard event),
# nothing is reported and the GUI can surface "not detectable".
# ---------------------------------------------------------------------------
class _RecorderMouseHook(_MouseHookBase):
    def __init__(self, report):
        super().__init__()
        self._report = report

    def _on_event(self, msg, mouse_data):
        trig = None
        if msg == _WM_MBUTTONDOWN:
            trig = "mouse:middle"
        elif msg == _WM_XBUTTONDOWN:
            xb = _xbutton_from_data(mouse_data)
            trig = "mouse:x1" if xb == _XBUTTON1 else "mouse:x2"
        elif msg == _WM_LBUTTONDOWN:
            trig = "mouse:left+right"   # offered as the chord option
        elif msg == _WM_RBUTTONDOWN:
            trig = "mouse:left+right"
        if trig is not None:
            try:
                self._report(trig)
            except Exception:
                pass
            return True  # swallow just this reporting click
        return False


class TriggerRecorder:
    """Capture the NEXT trigger for the GUI picker.

    on_detect(info): called once (on a background thread) with the classify
        dict {"trigger","label","clean","warning"} for what was detected. The
        GUI should marshal it to the UI thread (e.g. customtkinter .after).
    on_status(text): optional live status string ("Listening...", etc).

    Usage:
        rec = TriggerRecorder(on_detect=..., on_status=...)
        rec.start()
        ... user presses a key or clicks a mouse button ...
        rec.stop()   # also auto-stops after the first detection
    """

    def __init__(self, on_detect, on_status=None):
        self.on_detect = on_detect
        self.on_status = on_status
        self._mouse_hook = None
        self._kb_thread = None
        self._stopped = threading.Event()
        self._fired = threading.Event()

    def _status(self, text):
        if self.on_status:
            try:
                self.on_status(text)
            except Exception:
                pass

    def _emit(self, trigger):
        if self._fired.is_set():
            return
        self._fired.set()
        info = classify_trigger(trigger)
        try:
            self.on_detect(info)
        except Exception:
            log.error("TriggerRecorder on_detect error:\n%s",
                      traceback.format_exc())
        # Auto-stop after the first detection.
        self.stop()

    def start(self):
        self._status("Press a key combo or click a mouse button...")
        # Mouse: low-level hook reports any standard button.
        if os.name == "nt":
            try:
                self._mouse_hook = _RecorderMouseHook(self._emit)
                self._mouse_hook.start()
            except Exception:
                self._mouse_hook = None
        # Keyboard: read_hotkey blocks until a combo is released -> own thread.
        self._kb_thread = threading.Thread(
            target=self._kb_worker, daemon=True, name="trigrec-kb")
        self._kb_thread.start()

    def _kb_worker(self):
        try:
            hk = keyboard.read_hotkey(suppress=False)
        except Exception:
            hk = None
        if hk and not self._stopped.is_set() and not self._fired.is_set():
            self._emit(hk)

    def stop(self):
        if self._stopped.is_set():
            return
        self._stopped.set()
        if self._mouse_hook is not None:
            try:
                self._mouse_hook.stop()
            except Exception:
                pass
            self._mouse_hook = None
        # The keyboard worker may still be blocked in read_hotkey; it will
        # resolve on the next key event and self-check _stopped/_fired. We send
        # a harmless key to unblock it so the daemon thread doesn't linger.
        try:
            if self._kb_thread and self._kb_thread.is_alive():
                keyboard.press_and_release("shift")
        except Exception:
            pass


# ===========================================================================
# === ABC implementations (the platform factory hands these to the engine) ==
# ===========================================================================
class WindowsClipboard(ClipboardBackend):
    """ClipboardBackend wrapping the verified ClipboardManager. The engine uses
    the richer ClipboardManager directly (via the factory's clipboard object,
    which IS a ClipboardManager) so the verbatim paste cycle is preserved; this
    adapter satisfies the ABC for callers that want the abstract surface."""

    def __init__(self, restore_delay_ms=200, read_timeout_ms=2500):
        self._mgr = ClipboardManager(restore_delay_ms, read_timeout_ms)

    def snapshot(self):
        return self._mgr.save()

    def restore(self, snap):
        self._mgr.restore(snap)

    def set_text(self, text):
        self._mgr._set_text_immediate(text)

    # passthroughs so this can stand in for a ClipboardManager if desired
    def save(self):
        return self._mgr.save()

    def paste_text(self, text):
        return self._mgr.paste_text(text)


class WindowsPaster(Paster):
    """Paster wrapping the verified SendInput Ctrl+V path."""

    def __init__(self):
        self._chord = "ctrl+v"

    def paste(self):
        send_paste()

    def set_chord(self, chord):
        # The Windows SendInput path is Ctrl+V; Shift+Insert support for
        # terminals is a documented follow-up. We record the chord so callers
        # can read it back, but send_paste() remains Ctrl+V (verified).
        self._chord = chord or "ctrl+v"


class WindowsHotkeys(HotkeyBackend):
    """HotkeyBackend for plain keyboard combos (suppress=True)."""

    def __init__(self):
        self._registered = []   # [(combo, TriggerHandle)]

    def register(self, combo, on_press, on_release=None):
        self._registered.append((combo, on_press))

    def start(self):
        live = []
        for combo, cb in self._registered:
            handle = register_trigger(combo, cb)
            if handle is not None:
                live.append(handle)
        self._handles = live

    def stop(self):
        for h in getattr(self, "_handles", []):
            try:
                h.stop()
            except Exception:
                pass
        self._handles = []

    @property
    def supports_hold_mode(self):
        # The keyboard lib fires on press; hold/push-to-talk is not used by the
        # current toggle-based engine on Windows.
        return False


class WindowsMouse(MouseBackend):
    """MouseBackend for mouse-button + chord triggers (full suppression)."""

    supports_side_buttons = True

    def __init__(self):
        self._registered = []

    def register(self, button, on_press, on_release=None):
        self._registered.append((button, on_press))

    def start(self):
        live = []
        for button, cb in self._registered:
            trig = button if button.startswith("mouse:") else "mouse:" + button
            handle = register_trigger(trig, cb)
            if handle is not None:
                live.append(handle)
        self._handles = live

    def stop(self):
        for h in getattr(self, "_handles", []):
            try:
                h.stop()
            except Exception:
                pass
        self._handles = []


class WindowsTriggers(TriggerBackend):
    """The single, stable trigger API the engine uses: register ANY VoiceFlow
    trigger string (keyboard combo, mouse button, or the left+right chord) and
    get a stoppable handle. Delegates to the verbatim register_trigger."""

    def register(self, trigger, callback):
        return register_trigger(trigger, callback)

    def classify(self, trigger):
        return classify_trigger(trigger)

    @property
    def presets(self):
        return list(PRESETS)


class WindowsPermissions(Permissions):
    """Windows needs no TCC-style permission grants for hotkeys/paste. (UIPI:
    a non-elevated VoiceFlow can't paste into an elevated window -- documented,
    not a grantable permission.)"""

    def check(self):
        return {"accessibility": True, "input_monitoring": True, "mic": True}

    def request(self, name):
        return None

    def all_ok(self):
        return True


# Factory hooks (used by voiceflow.platform.make_backends).
# Virtual-key codes for named keys used by voice commands.
_VK_BY_NAME = {
    "backspace": 0x08, "back space": 0x08, "tab": 0x09, "enter": 0x0D,
    "return": 0x0D, "escape": 0x1B, "esc": 0x1B, "space": 0x20,
    "page_up": 0x21, "page_down": 0x22, "end": 0x23, "home": 0x24,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "insert": 0x2D, "delete": 0x2E, "del": 0x2E,
    "ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12,
    "win": 0x5B, "windows": 0x5B,
}


def _vk_for(name):
    name = name.lower().strip()
    if name in _VK_BY_NAME:
        return _VK_BY_NAME[name]
    if len(name) == 1:                 # single letter / digit
        return ord(name.upper())
    m = re.match(r"^f([0-9]{1,2})$", name)   # function keys F1..F24 (VK 0x70+)
    if m and 1 <= int(m.group(1)) <= 24:
        return 0x70 + int(m.group(1)) - 1
    return None


class WindowsTyper(Typer):
    """Types text into the focused window via SendInput KEYEVENTF_UNICODE (used
    by streaming mode), and presses keys/chords for VOICE COMMANDS. Releases any
    held trigger modifier first so inserted characters aren't swallowed as a
    shortcut. type_text never emits Enter; press_keys may (commands are explicit)."""

    def type_text(self, text):
        if not text:
            return
        try:
            _release_held_modifiers()
            _type_text_sendinput(text)
        except Exception:
            log.error("WindowsTyper.type_text failed:\n%s", traceback.format_exc())

    def press_keys(self, spec, count=1):
        """Press a key or chord, e.g. "backspace", "ctrl+a", "shift+end"."""
        if not spec:
            return
        parts = [p for p in str(spec).replace(" ", "").split("+") if p]
        if not parts:
            return
        mod_vks = [_vk_for(p) for p in parts[:-1]]
        key_vk = _vk_for(parts[-1])
        if key_vk is None or any(v is None for v in mod_vks):
            log.warning("Unknown key spec %r; ignoring.", spec)
            return
        try:
            _release_held_modifiers()
            for _ in range(max(1, int(count))):
                seq = [_ev(v) for v in mod_vks] + [_ev(key_vk)]
                seq += [_ev(key_vk, up=True)] + [_ev(v, up=True)
                                                 for v in reversed(mod_vks)]
                arr = (_INPUT * len(seq))(*seq)
                _u32.SendInput(len(arr), arr, ctypes.sizeof(_INPUT))
                time.sleep(0.005)
        except Exception:
            log.error("WindowsTyper.press_keys(%r) failed:\n%s",
                      spec, traceback.format_exc())

    @property
    def supports_incremental(self):
        return True


def make_typer():
    """Return the Windows incremental Typer for streaming mode."""
    return WindowsTyper()


def make_clipboard(restore_delay_ms=200, read_timeout_ms=2500):
    """Return the clipboard object the engine uses. This is the verified
    ClipboardManager itself (richer than the bare ClipboardBackend ABC, with the
    full verbatim paste cycle) so Windows paste behaviour is byte-for-byte
    preserved."""
    return ClipboardManager(restore_delay_ms, read_timeout_ms)


def Hotkeys():
    return WindowsHotkeys()


def Mouse():
    return WindowsMouse()


def Clipboard():
    return WindowsClipboard()


def PasterImpl():
    return WindowsPaster()


def PermissionsImpl():
    return WindowsPermissions()


def Triggers():
    return WindowsTriggers()
