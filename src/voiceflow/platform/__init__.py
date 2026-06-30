"""
voiceflow.platform - runtime-selected platform abstraction.

``detect_platform()`` picks the OS (and, on Linux, the session type), and
``make_backends()`` returns the concrete backends for that platform. The engine
and GUI talk only to these (and the ABCs in ``base``), never to an OS module
directly -- so adding macOS / Linux is a matter of dropping in a sibling module
that implements the same surface.

The Windows backend is the verified target. The macOS backend (``macos.py``)
is implemented per docs/PRODUCTION_PLAN.md sections 2.4 / 3.2 (pyobjc
NSPasteboard; Quartz/pynput Cmd+V; pynput hotkeys + CGEventTap side buttons; TCC
permissions). The Linux backends (``linux_x11.py`` and ``linux_wayland.py``,
sharing ``linux_common.py``) are implemented per section 2.5 (evdev
hotkeys/mouse; xclip+xdotool on X11; wl-clipboard+ydotool/wtype on Wayland).
"""

from __future__ import annotations

import os
import sys

from .base import (  # re-export the ABCs for convenience
    HotkeyBackend, MouseBackend, ClipboardBackend, Paster, Permissions,
    TriggerBackend, TriggerHandle,
)

__all__ = [
    "detect_platform", "make_backends", "make_trigger_backend", "make_clipboard",
    "make_permissions", "make_typer", "make_multi_chord",
    "make_tap_hold_chord", "make_tap_hold_keyboard", "diagnostics",
    "HotkeyBackend", "MouseBackend", "ClipboardBackend", "Paster", "Permissions",
    "TriggerBackend", "TriggerHandle",
]


def detect_platform() -> str:
    """Return one of: "windows" | "macos" | "linux-wayland" | "linux-x11"."""
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    if os.environ.get("WAYLAND_DISPLAY") or \
            os.environ.get("XDG_SESSION_TYPE") == "wayland":
        return "linux-wayland"
    return "linux-x11"


def _backend_module():
    """Import and return the OS-specific backend module for this platform."""
    p = detect_platform()
    if p == "windows":
        from . import windows as b
    elif p == "macos":  # pragma: no cover - needs a macOS host
        from . import macos as b
    elif p == "linux-wayland":  # pragma: no cover - needs a Linux/Wayland session
        from . import linux_wayland as b
    else:  # pragma: no cover - needs a Linux/X11 session
        from . import linux_x11 as b
    return b


def make_backends():
    """Return (Hotkeys, Mouse, Clipboard, Paster, Permissions) for this OS.

    Matches the plan's factory signature. Each element is a concrete instance of
    the corresponding ABC implementation.
    """
    b = _backend_module()
    return (b.Hotkeys(), b.Mouse(), b.Clipboard(), b.PasterImpl(),
            b.PermissionsImpl())


def make_trigger_backend() -> TriggerBackend:
    """Return the platform's combined TriggerBackend -- the single stable trigger
    API the engine uses (keyboard combos AND mouse buttons AND the left+right
    chord behind one register())."""
    return _backend_module().Triggers()


def make_clipboard(restore_delay_ms=200, read_timeout_ms=2500):
    """Return the platform's clipboard object for the engine. On Windows this is
    the verified ClipboardManager (with the verbatim paste cycle), which is
    richer than the bare ClipboardBackend ABC."""
    return _backend_module().make_clipboard(restore_delay_ms, read_timeout_ms)


def make_multi_chord(specs):
    """Register several mouse chords (that may share a button) on ONE hook.
    specs = list of (trigger_str, callback). Returns a stoppable handle, or None
    if unsupported on this OS / any spec isn't a valid chord."""
    b = _backend_module()
    fn = getattr(b, "register_chords", None)
    return fn(specs) if callable(fn) else None


def make_tap_hold_chord(trigger, on_tap, on_hold_start, on_hold_end,
                        hold_seconds=None):
    """Register a tap/hold mouse chord (tap -> on_tap; hold -> on_hold_start then
    on_hold_end on release). Returns a stoppable handle, or None if unsupported."""
    b = _backend_module()
    fn = getattr(b, "register_tap_hold", None)
    return (fn(trigger, on_tap, on_hold_start, on_hold_end, hold_seconds)
            if callable(fn) else None)


def make_tap_hold_keyboard(trigger, on_tap, on_hold_start, on_hold_end,
                           hold_seconds=None):
    """Register a tap/hold KEYBOARD combo (tap -> on_tap; hold -> on_hold_start
    then on_hold_end on release). Returns a stoppable handle, or None if
    unsupported on this OS / not a keyboard combo."""
    b = _backend_module()
    fn = getattr(b, "register_tap_hold_keyboard", None)
    return (fn(trigger, on_tap, on_hold_start, on_hold_end, hold_seconds)
            if callable(fn) else None)


def make_typer():
    """Return the platform's incremental Typer (types confirmed words live into
    the focused app for STREAMING mode), or None if the OS backend lacks one.
    Windows: SendInput KEYEVENTF_UNICODE. macOS: CGEvent unicode. Linux:
    wtype/xdotool/ydotool."""
    b = _backend_module()
    fn = getattr(b, "make_typer", None)
    return fn() if callable(fn) else None


def make_permissions() -> Permissions:
    """Return the platform's Permissions object (Windows: always-ok; Linux:
    reports /dev/input readability, /dev/uinput access, ydotoold, etc.)."""
    return _backend_module().PermissionsImpl()


def diagnostics() -> dict:
    """Return a UI-ready diagnostics dict for the current platform.

    On Linux this includes the in-app checks required by the plan: is
    /dev/input readable? is the user in group 'input'? is /dev/uinput
    accessible? is ydotoold running? plus the per-session clipboard/paste tool
    availability and a 'can_paste' summary with human-readable hints. On other
    platforms it returns the Permissions.check() mapping."""
    b = _backend_module()
    fn = getattr(b, "diagnostics", None)
    if callable(fn):
        return fn()
    return b.PermissionsImpl().check()
