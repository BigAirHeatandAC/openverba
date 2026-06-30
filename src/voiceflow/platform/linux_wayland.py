"""
voiceflow.platform.linux_wayland - the Linux/Wayland platform backend.

Implements the platform ABCs for a native Wayland session:

  * Clipboard  -> ``wl-clipboard`` (``wl-copy`` / ``wl-paste``) per MIME type
    (format-aware snapshot/restore).
  * Paste      -> ``ydotool`` (primary; universal via the kernel uinput device,
    needs ``ydotoold`` running + ``/dev/uinput`` writable) -> ``wtype``
    (fallback; works on wlroots compositors: sway / Hyprland).
  * Hotkeys / Mouse / Triggers -> the shared evdev listener (linux_common).
    pynput silently no-ops under native Wayland and can't see mouse side-buttons,
    so evdev (reading /dev/input directly) is the ONLY reliable mechanism, and it
    distinguishes a real key release from autorepeat for hold/push-to-talk.
  * Permissions -> Linux facts (read /dev/input, /dev/uinput, ydotoold) + the
    Wayland paste/clipboard tool checks.

NEVER simulate *typing* the transcript on Wayland -- non-ASCII / non-US layouts
garble. Always clipboard + paste keystroke (layout-independent), matching the
verified Windows behaviour.

Wayland is a "may require setup" tier: GNOME in particular needs ydotoold set up
(and there is no portable global-hotkey API), so the backend surfaces clear
diagnostics rather than promising "just works".

CANNOT BE VERIFIED ON THE WINDOWS DEV MACHINE: every runtime path needs a real
Wayland session with wl-clipboard / ydotool(d) / wtype / evdev and a writable
/dev/uinput. What IS exercised on Windows is that the module imports cleanly
(subprocess/shutil only) and that the shared trigger logic works.
"""

from __future__ import annotations

import logging
import subprocess
import time

from .base import ClipboardBackend, Paster
from .linux_common import (  # re-exported for triggers.py / the factory
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
    ydotoold_running,
    uinput_accessible,
)

log = logging.getLogger("voiceflow.platform.linux_wayland")

__all__ = [
    "TriggerRecorder", "classify_trigger", "PRESETS", "register_trigger",
    "make_clipboard", "Hotkeys", "Mouse", "Clipboard", "PasterImpl",
    "PermissionsImpl", "Triggers", "WaylandClipboard", "WaylandPaster",
]

# MIME types we snapshot/restore. Plain text first (the common case); the rich
# ones are best-effort and lost gracefully if absent.
_TEXT_MIMES = ["text/plain;charset=utf-8", "text/plain", "UTF8_STRING", "STRING"]
_RICH_MIMES = ["text/html", "image/png", "text/rtf", "text/uri-list"]
_ALL_MIMES = _TEXT_MIMES + _RICH_MIMES


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
# === Clipboard (wl-clipboard per MIME) =====================================
# ===========================================================================
class _WaylandClipboardManager:
    """Engine-facing clipboard with the verbatim paste cycle (parity with the
    Windows ClipboardManager): save -> set transcript -> paste -> settle ->
    restore. Snapshot is best-effort per MIME via wl-paste."""

    def __init__(self, restore_delay_ms=200, read_timeout_ms=2500):
        self.restore_floor_s = max(restore_delay_ms / 1000.0, 0.0)
        self.read_timeout_s = max(read_timeout_ms / 1000.0, 0.3)

    # ---- low-level wl-clipboard helpers ----
    @staticmethod
    def _list_types(timeout: float) -> list[str]:
        rc, out = _run(["wl-paste", "--list-types"], timeout=timeout)
        if rc != 0 or not out:
            return []
        try:
            return [ln.strip() for ln in out.decode("utf-8", "replace").splitlines()
                    if ln.strip()]
        except Exception:
            return []

    @staticmethod
    def _wl_paste(mime: str, timeout: float) -> bytes | None:
        rc, out = _run(["wl-paste", "--no-newline", "--type", mime], timeout=timeout)
        if rc == 0 and out:
            return out
        return None

    @staticmethod
    def _wl_copy(mime: str, data: bytes, timeout: float) -> bool:
        rc, _ = _run(["wl-copy", "--type", mime], inp=data, timeout=timeout)
        return rc == 0

    # ---- save ----
    def save(self) -> dict:
        """Snapshot the clipboard as {mime: bytes}, best-effort. Only MIME types
        wl-paste reports AND that we round-trip are captured; lazily-offered/
        unknown types are dropped (documented)."""
        if not tool_present("wl-paste"):
            return {}
        present = set(self._list_types(self.read_timeout_s))
        saved: dict[str, bytes] = {}
        text_done = False
        for mime in _ALL_MIMES:
            if present and mime not in present:
                continue
            if mime in _TEXT_MIMES:
                if text_done:
                    continue
                data = self._wl_paste(mime, self.read_timeout_s)
                if data is not None:
                    saved[mime] = data
                    text_done = True
            else:
                data = self._wl_paste(mime, self.read_timeout_s)
                if data is not None:
                    saved[mime] = data
        return saved

    # ---- set text ----
    def _set_text_immediate(self, text: str) -> None:
        if not tool_present("wl-copy"):
            log.warning("wl-copy not found; cannot set clipboard on Wayland.")
            return
        self._wl_copy("text/plain;charset=utf-8", text.encode("utf-8"),
                      self.read_timeout_s)

    # ---- restore ----
    def restore(self, saved: dict) -> None:
        if not saved or not tool_present("wl-copy"):
            return
        # wl-copy replaces the whole selection each call, so to restore multiple
        # types we set rich types first and the primary text type LAST (so the
        # readable text wins). This loses the multi-type atomicity of the
        # original, like the X11 path -- documented best-effort.
        items = list(saved.items())
        items.sort(key=lambda kv: kv[0] in _TEXT_MIMES)  # rich first, text last
        for mime, data in items:
            if isinstance(data, str):
                data = data.encode("utf-8")
            self._wl_copy(mime, data, self.read_timeout_s)

    def _clear(self) -> None:
        if tool_present("wl-copy"):
            _run(["wl-copy", "--clear"], timeout=self.read_timeout_s)

    # ---- the full paste cycle ----
    def paste_text(self, text: str) -> bool:
        """save -> set transcript -> paste chord -> settle -> restore (finally).
        Returns True if we set the clipboard and sent the paste."""
        if not text:
            return False
        saved = self.save()
        try:
            self._set_text_immediate(text)
            time.sleep(0.03)
            WaylandPaster().paste()
            time.sleep(max(self.restore_floor_s, 0.15))
        finally:
            if saved:
                self.restore(saved)
            else:
                self._clear()
        return True


class WaylandClipboard(ClipboardBackend):
    """ClipboardBackend ABC wrapper over the wl-clipboard manager."""

    def __init__(self, restore_delay_ms=200, read_timeout_ms=2500):
        self._mgr = _WaylandClipboardManager(restore_delay_ms, read_timeout_ms)

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
# === Paste (ydotool -> wtype) ==============================================
# ===========================================================================
class WaylandPaster(Paster):
    """Synthesize the paste chord on Wayland. Primary: ``ydotool key`` (uinput;
    needs ydotoold + /dev/uinput). Fallback: ``wtype`` (wlroots: sway/Hyprland).
    Never types the transcript itself -- only the chord."""

    # ydotool uses Linux input-event key CODES (not keysyms): keydown=:1,
    # keyup=:0. 29=LEFTCTRL, 47=V, 42=LEFTSHIFT, 110=INSERT.
    _YDOTOOL_CODES = {
        "ctrl": 29, "control": 29, "shift": 42, "alt": 56,
        "super": 125, "win": 125, "meta": 125,
        "v": 47, "insert": 110, "ins": 110,
    }

    def __init__(self):
        self._chord = "ctrl+v"

    def set_chord(self, chord):
        self._chord = (chord or "ctrl+v").strip().lower()

    def _ydotool_args(self) -> list[str] | None:
        parts = self._chord.split("+")
        codes = []
        for p in parts:
            c = self._YDOTOOL_CODES.get(p)
            if c is None:
                return None
            codes.append(c)
        # press all in order, then release in reverse.
        seq = ["%d:1" % c for c in codes] + ["%d:0" % c for c in reversed(codes)]
        return ["ydotool", "key", *seq]

    def _wtype_args(self) -> list[str]:
        # wtype: -M/-m press/release a modifier; the main key via -k or -P/-p.
        wt_mods = {"ctrl": "ctrl", "control": "ctrl", "shift": "shift",
                   "alt": "alt", "super": "logo", "win": "logo", "meta": "logo"}
        wt_keys = {"v": "v", "insert": "Insert", "ins": "Insert"}
        parts = self._chord.split("+")
        mods = [wt_mods[p] for p in parts if p in wt_mods]
        main = next((p for p in parts if p not in wt_mods), "v")
        key = wt_keys.get(main, main)
        args = ["wtype"]
        for m in mods:
            args += ["-M", m]
        args += ["-P", key, "-p", key]
        for m in reversed(mods):
            args += ["-m", m]
        return args

    def paste(self):
        # Primary: ydotool (only if its daemon + uinput are available).
        if tool_present("ydotool"):
            args = self._ydotool_args()
            if args is None:
                log.warning("Unsupported paste chord %r for ydotool.", self._chord)
            elif not ydotoold_running():
                log.warning("ydotool present but ydotoold isn't running; "
                            "start it (and ensure /dev/uinput is writable). "
                            "Falling back to wtype.")
            elif not uinput_accessible():
                log.warning("/dev/uinput not writable; ydotool paste will fail. "
                            "Falling back to wtype.")
            else:
                rc, _ = _run(args, timeout=2.0)
                if rc == 0:
                    return
                log.warning("ydotool paste returned %s; trying wtype.", rc)
        # Fallback: wtype (wlroots compositors).
        if tool_present("wtype"):
            rc, _ = _run(self._wtype_args(), timeout=2.0)
            if rc == 0:
                return
            log.warning("wtype paste returned %s.", rc)
        log.error("No working Wayland paste tool. Install ydotool (+ydotoold, "
                  "/dev/uinput) or wtype (sway/Hyprland).")


# ===========================================================================
# === Permissions (Linux facts + Wayland tool checks) =======================
# ===========================================================================
class WaylandPermissions(_LinuxPermissions):
    def diagnostics(self):
        d = super().diagnostics()
        d["wl_copy"] = tool_present("wl-copy")
        d["wl_paste"] = tool_present("wl-paste")
        d["ydotool"] = tool_present("ydotool")
        d["wtype"] = tool_present("wtype")
        # Paste works if EITHER ydotool (with daemon + uinput) OR wtype is usable,
        # and clipboard tools are present.
        ydotool_ok = (d["ydotool"] and d["ydotoold_running"]
                      and d["uinput_accessible"])
        d["can_paste"] = bool(d["wl_copy"]) and (ydotool_ok or bool(d["wtype"]))
        hints = []
        if not (d["wl_copy"] and d["wl_paste"]):
            hints.append("Install wl-clipboard: sudo apt install wl-clipboard")
        if not ydotool_ok and not d["wtype"]:
            if d["ydotool"] and not d["ydotoold_running"]:
                hints.append("Start the ydotoold daemon (and add /dev/uinput "
                             "access) so ydotool can paste.")
            elif d["ydotool"] and not d["uinput_accessible"]:
                hints.append("Make /dev/uinput writable (udev rule / group) for "
                             "ydotool.")
            else:
                hints.append("Install ydotool (+ydotoold) or, on sway/Hyprland, "
                             "wtype, to synthesize the paste keystroke.")
        d["paste_hint"] = " ".join(hints) if hints else None
        return d


# ===========================================================================
# === Factory hooks (consumed by voiceflow.platform.make_backends) ==========
# ===========================================================================
def make_clipboard(restore_delay_ms=200, read_timeout_ms=2500):
    """The engine's clipboard object (the wl-clipboard manager with the verbatim
    paste cycle)."""
    return _WaylandClipboardManager(restore_delay_ms, read_timeout_ms)


def Hotkeys():
    return Hotkeys_cls()


def Mouse():
    return Mouse_cls()


def Clipboard():
    return WaylandClipboard()


def PasterImpl():
    return WaylandPaster()


def PermissionsImpl():
    return WaylandPermissions()


def Triggers():
    return Triggers_cls()


def diagnostics() -> dict:
    """UI-facing one-shot diagnostics for the Wayland backend (input + paste/
    clipboard tools, ydotoold, /dev/uinput)."""
    return WaylandPermissions().diagnostics()
