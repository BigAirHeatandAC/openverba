"""
voiceflow.platform.linux_x11 - the Linux/X11 platform backend.

Implements the platform ABCs for an X11 session:

  * Clipboard  -> ``xclip`` per X11 TARGET (format-aware snapshot/restore).
  * Paste      -> ``xdotool key ctrl+v`` (primary) -> pynput (fallback).
  * Hotkeys / Mouse / Triggers -> the shared evdev listener (linux_common): the
    only mechanism that sees mouse side-buttons and distinguishes a real key
    release from autorepeat, and that works identically on X11 and Wayland.
  * Permissions -> Linux facts (read /dev/input, /dev/uinput) + the X11 paste
    tool check.

NEVER simulate *typing* the transcript -- always clipboard + paste keystroke
(layout-independent). This matches the verified Windows behaviour (set clipboard
-> paste -> restore).

CANNOT BE VERIFIED ON THE WINDOWS DEV MACHINE: every runtime path here needs a
real X11 session with xclip / xdotool / evdev. What IS exercised on Windows is
that the module imports cleanly (subprocess/shutil only at module top; evdev and
the X11 tools are touched lazily) and that the OS-agnostic trigger logic in
linux_common works.
"""

from __future__ import annotations

import logging
import subprocess
import time

from .base import ClipboardBackend, Paster
from .linux_common import (  # re-exported so triggers.py / the factory find them
    LinuxHotkeys as Hotkeys_cls,
    LinuxMouse as Mouse_cls,
    LinuxTriggers as Triggers_cls,
    LinuxPermissions as _LinuxPermissions,
    TriggerRecorder,
    TriggerHandle,  # noqa: F401  (parity with windows.py surface)
    classify_trigger,
    PRESETS,
    register_trigger,
    tool_present,
)

log = logging.getLogger("voiceflow.platform.linux_x11")

__all__ = [
    "TriggerRecorder", "classify_trigger", "PRESETS", "register_trigger",
    "make_clipboard", "Hotkeys", "Mouse", "Clipboard", "PasterImpl",
    "PermissionsImpl", "Triggers", "X11Clipboard", "X11Paster",
]

# X11 TARGETs we snapshot/restore. text/plain + UTF8_STRING cover plain text;
# the rich ones are best-effort (lost gracefully if absent).
_TEXT_TARGETS = ["UTF8_STRING", "text/plain;charset=utf-8", "STRING", "TEXT"]
_RICH_TARGETS = ["text/html", "image/png", "text/rtf",
                 "application/x-kde4-urilist", "text/uri-list"]
_ALL_TARGETS = _TEXT_TARGETS + _RICH_TARGETS

# We operate on the CLIPBOARD selection (Ctrl+C/Ctrl+V), not PRIMARY.
_SELECTION = "clipboard"


def _run(cmd, *, inp: bytes | None = None, timeout: float = 2.0):
    """Run a subprocess, returning (rc, stdout_bytes). Never raises."""
    try:
        p = subprocess.run(cmd, input=inp, stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, timeout=timeout)
        return p.returncode, p.stdout or b""
    except Exception as exc:
        log.debug("subprocess %s failed: %s", cmd, exc)
        return 1, b""


# ===========================================================================
# === Clipboard (xclip per TARGET) ==========================================
# ===========================================================================
class _X11ClipboardManager:
    """Engine-facing clipboard with the verbatim paste cycle (parity with the
    Windows ClipboardManager): save -> set transcript -> paste -> settle ->
    restore. Snapshot is best-effort per X11 TARGET via xclip.
    """

    def __init__(self, restore_delay_ms=200, read_timeout_ms=2500):
        self.restore_floor_s = max(restore_delay_ms / 1000.0, 0.0)
        self.read_timeout_s = max(read_timeout_ms / 1000.0, 0.3)

    # ---- low-level xclip helpers ----
    @staticmethod
    def _xclip_get(target: str, timeout: float) -> bytes | None:
        rc, out = _run(["xclip", "-selection", _SELECTION, "-t", target, "-o"],
                       timeout=timeout)
        if rc == 0 and out:
            return out
        return None

    @staticmethod
    def _xclip_set(target: str, data: bytes, timeout: float) -> bool:
        rc, _ = _run(["xclip", "-selection", _SELECTION, "-t", target, "-i"],
                     inp=data, timeout=timeout)
        return rc == 0

    @staticmethod
    def _available_targets(timeout: float) -> list[str]:
        rc, out = _run(["xclip", "-selection", _SELECTION, "-t", "TARGETS", "-o"],
                       timeout=timeout)
        if rc != 0 or not out:
            return []
        try:
            return [ln.strip() for ln in out.decode("utf-8", "replace").splitlines()
                    if ln.strip()]
        except Exception:
            return []

    # ---- save ----
    def save(self) -> dict:
        """Snapshot the clipboard as {target: bytes}, best-effort. Only the
        TARGETs xclip reports as available AND that we know how to round-trip are
        captured; lazily-rendered/unknown formats are dropped (documented)."""
        if not tool_present("xclip"):
            return {}
        present = set(self._available_targets(self.read_timeout_s))
        saved: dict[str, bytes] = {}
        # Capture at most one text target (the first present) + rich targets.
        text_done = False
        for tgt in _ALL_TARGETS:
            if present and tgt not in present:
                continue
            if tgt in _TEXT_TARGETS:
                if text_done:
                    continue
                data = self._xclip_get(tgt, self.read_timeout_s)
                if data is not None:
                    saved[tgt] = data
                    text_done = True
            else:
                data = self._xclip_get(tgt, self.read_timeout_s)
                if data is not None:
                    saved[tgt] = data
        return saved

    # ---- set text ----
    def _set_text_immediate(self, text: str) -> None:
        if not tool_present("xclip"):
            log.warning("xclip not found; cannot set clipboard on X11.")
            return
        self._xclip_set("UTF8_STRING", text.encode("utf-8"), self.read_timeout_s)

    # ---- restore ----
    def restore(self, saved: dict) -> None:
        if not saved or not tool_present("xclip"):
            return
        # xclip owns ONE selection per invocation; setting multiple TARGETs
        # atomically isn't possible via the CLI, so we set the best text target
        # last (so it wins as the readable text), after any rich targets.
        items = list(saved.items())
        items.sort(key=lambda kv: kv[0] in _TEXT_TARGETS)  # rich first, text last
        for tgt, data in items:
            if isinstance(data, str):
                data = data.encode("utf-8")
            self._xclip_set(tgt, data, self.read_timeout_s)

    def _clear(self) -> None:
        if tool_present("xclip"):
            self._xclip_set("UTF8_STRING", b"", self.read_timeout_s)

    # ---- the full paste cycle ----
    def paste_text(self, text: str) -> bool:
        """save -> set transcript -> Ctrl+V -> settle -> restore (in finally).
        Returns True if we set the clipboard and sent the paste."""
        if not text:
            return False
        saved = self.save()
        try:
            self._set_text_immediate(text)
            time.sleep(0.03)
            X11Paster().paste()
            time.sleep(max(self.restore_floor_s, 0.15))
        finally:
            if saved:
                self.restore(saved)
            else:
                self._clear()
        return True


class X11Clipboard(ClipboardBackend):
    """ClipboardBackend ABC wrapper over the xclip manager."""

    def __init__(self, restore_delay_ms=200, read_timeout_ms=2500):
        self._mgr = _X11ClipboardManager(restore_delay_ms, read_timeout_ms)

    def snapshot(self):
        return self._mgr.save()

    def restore(self, snap):
        self._mgr.restore(snap)

    def set_text(self, text):
        self._mgr._set_text_immediate(text)

    # passthroughs (parity with WindowsClipboard)
    def save(self):
        return self._mgr.save()

    def paste_text(self, text):
        return self._mgr.paste_text(text)


# ===========================================================================
# === Paste (xdotool -> pynput) =============================================
# ===========================================================================
class X11Paster(Paster):
    """Synthesize the paste chord on X11. Primary: ``xdotool key`` (robust, no
    library). Fallback: pynput keyboard controller. Never types the transcript
    itself -- only the chord."""

    def __init__(self):
        self._chord = "ctrl+v"

    def set_chord(self, chord):
        self._chord = (chord or "ctrl+v").strip().lower()

    def _xdotool_chord(self) -> str:
        # VoiceFlow chord "ctrl+v" / "shift+insert" -> xdotool "ctrl+v" /
        # "shift+Insert". xdotool wants capitalized special keysyms.
        parts = self._chord.split("+")
        keysym = {
            "ctrl": "ctrl", "control": "ctrl", "alt": "alt", "shift": "shift",
            "super": "super", "win": "super", "meta": "super",
            "insert": "Insert", "ins": "Insert", "v": "v",
        }
        return "+".join(keysym.get(p, p) for p in parts)

    def paste(self):
        if tool_present("xdotool"):
            rc, _ = _run(["xdotool", "key", "--clearmodifiers",
                          self._xdotool_chord()], timeout=2.0)
            if rc == 0:
                return
            log.warning("xdotool paste returned %s; trying pynput.", rc)
        self._pynput_paste()

    def _pynput_paste(self):
        try:
            from pynput.keyboard import Controller, Key
            kb = Controller()
            mods = {"ctrl": Key.ctrl, "control": Key.ctrl, "alt": Key.alt,
                    "shift": Key.shift, "super": Key.cmd, "win": Key.cmd,
                    "meta": Key.cmd}
            specials = {"insert": Key.insert, "ins": Key.insert}
            parts = self._chord.split("+")
            mod_keys = [mods[p] for p in parts if p in mods]
            main = next((p for p in parts if p not in mods), "v")
            main_key = specials.get(main, main)
            for m in mod_keys:
                kb.press(m)
            kb.press(main_key)
            kb.release(main_key)
            for m in reversed(mod_keys):
                kb.release(m)
        except Exception as exc:
            log.error("pynput X11 paste failed: %s "
                      "(install xdotool for a reliable paste).", exc)


# ===========================================================================
# === Permissions (Linux facts + X11 paste tool check) ======================
# ===========================================================================
class X11Permissions(_LinuxPermissions):
    def diagnostics(self):
        d = super().diagnostics()
        d["xclip"] = tool_present("xclip")
        d["xdotool"] = tool_present("xdotool")
        # On X11 paste works via xdotool OR pynput; clipboard needs xclip.
        d["can_paste"] = bool(d["xclip"]) and (
            bool(d["xdotool"]) or _pynput_present())
        if not d["xclip"]:
            d["paste_hint"] = ("Install xclip (clipboard) and xdotool (paste): "
                               "e.g. sudo apt install xclip xdotool")
        elif not d["xdotool"] and not _pynput_present():
            d["paste_hint"] = ("Install xdotool for a reliable paste: "
                               "sudo apt install xdotool")
        else:
            d["paste_hint"] = None
        return d


def _pynput_present() -> bool:
    try:
        import pynput  # noqa: F401
        return True
    except Exception:
        return False


# ===========================================================================
# === Factory hooks (consumed by voiceflow.platform.make_backends) ==========
# ===========================================================================
def make_clipboard(restore_delay_ms=200, read_timeout_ms=2500):
    """The engine's clipboard object (the xclip manager with the verbatim paste
    cycle)."""
    return _X11ClipboardManager(restore_delay_ms, read_timeout_ms)


def Hotkeys():
    return Hotkeys_cls()


def Mouse():
    return Mouse_cls()


def Clipboard():
    return X11Clipboard()


def PasterImpl():
    return X11Paster()


def PermissionsImpl():
    return X11Permissions()


def Triggers():
    return Triggers_cls()


def diagnostics() -> dict:
    """UI-facing one-shot diagnostics for the X11 backend (input + paste tools)."""
    return X11Permissions().diagnostics()
