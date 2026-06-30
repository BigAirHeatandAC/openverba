"""
voiceflow.platform.linux_common - shared Linux input/trigger machinery.

Both the X11 and Wayland backends share the same global-input story: the only
session-independent way to read the keyboard and mouse on Linux (and the only
one that works under *native* Wayland, where pynput silently no-ops and cannot
see mouse side-buttons) is to read the raw evdev devices under ``/dev/input``.
This module implements that shared layer:

  * an evdev-based global input listener that distinguishes a REAL key release
    from a key-repeat (evdev value 2 == autorepeat) -- the thing pynput can't do
    -- so hold/push-to-talk is reliable;
  * trigger parsing for VoiceFlow's full trigger model (keyboard combos AND
    single mouse buttons middle/x1/x2 AND the left+right chord);
  * ``classify_trigger`` / ``PRESETS`` / ``TriggerRecorder`` mirroring the
    Windows backend so the GUI picker is identical across platforms;
  * the ``LinuxTriggers`` / ``LinuxHotkeys`` / ``LinuxMouse`` ABC bases;
  * a ``LinuxPermissions`` base reporting the Linux-specific facts the UI needs
    (is ``/dev/input`` readable? are we in group ``input``? is ``/dev/uinput``
    accessible? is ``ydotoold`` running?);
  * diagnostics helpers usable directly by the UI.

IMPORTANT: ``evdev`` is a Linux-only C extension. It is imported LAZILY (inside
functions / on first listener start), never at module import time, so that
importing :mod:`voiceflow.platform` on Windows/macOS for the factory's selection
logic never fails. The factory only imports this module on Linux anyway, but the
lazy import keeps even a stray ``import`` on the wrong OS harmless.

CANNOT BE VERIFIED ON THE WINDOWS DEV MACHINE: the evdev grab/read loop, real
device enumeration, /dev/uinput access, and ydotoold detection all require a
real Linux session (X11 and Wayland). What IS exercised here on Windows is the
OS-agnostic logic: trigger parsing/classification, the presets, the diagnostics
shape, and that the module imports cleanly with evdev absent.
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
import traceback
from typing import Callable

# pwd/grp are Unix-only. Guard them so this module IMPORTS on Windows (for CI /
# the factory's selection logic / unit tests). On Linux they are always present.
try:
    import pwd
    import grp
except Exception:  # pragma: no cover - exercised only on non-Unix
    pwd = None  # type: ignore
    grp = None  # type: ignore

from .base import (
    HotkeyBackend,
    MouseBackend,
    Permissions,
    TriggerBackend,
    TriggerHandle as _TriggerHandleABC,
)

log = logging.getLogger("voiceflow.platform.linux")


# ===========================================================================
# === evdev availability (lazy) =============================================
# ===========================================================================
def _import_evdev():
    """Import python-evdev lazily. Returns the module or None if unavailable
    (e.g. running on Windows, or the dep isn't installed). NEVER raises."""
    try:
        import evdev  # type: ignore
        return evdev
    except Exception:  # pragma: no cover - evdev absent on the dev box
        return None


def evdev_available() -> bool:
    """True if python-evdev can be imported (Linux + dep installed)."""
    return _import_evdev() is not None


# ===========================================================================
# === Diagnostics (usable directly by the UI) ===============================
# ===========================================================================
def _username() -> str:
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except Exception:
        return os.environ.get("USER") or os.environ.get("LOGNAME") or "?"


def _user_groups() -> list[str]:
    """All group names the current user belongs to (primary + supplementary)."""
    names: set[str] = set()
    try:
        for g in grp.getgrall():
            if _username() in g.gr_mem:
                names.add(g.gr_name)
    except Exception:
        pass
    try:
        prim = grp.getgrgid(pwd.getpwuid(os.getuid()).pw_gid).gr_name
        names.add(prim)
    except Exception:
        pass
    return sorted(names)


def in_input_group() -> bool:
    """True if the current user is in the ``input`` group (the supported way to
    read /dev/input without root or a per-device udev rule)."""
    return "input" in _user_groups()


def input_devices_readable() -> bool:
    """True if at least one /dev/input/event* node is readable by this process
    (i.e. evdev can actually open a device). This is the real test the hotkey
    backend depends on -- being in group ``input`` is the usual cause but a udev
    rule or running as root also works."""
    try:
        nodes = [n for n in os.listdir("/dev/input") if n.startswith("event")]
    except Exception:
        return False
    for n in nodes:
        path = os.path.join("/dev/input", n)
        if os.access(path, os.R_OK):
            return True
    return False


def uinput_accessible() -> bool:
    """True if /dev/uinput exists and is writable -- required by ydotool/ydotoold
    (and any uinput-based virtual input) to synthesize the paste keystroke."""
    return os.path.exists("/dev/uinput") and os.access("/dev/uinput", os.W_OK)


def ydotoold_running() -> bool:
    """True if the ydotoold daemon appears to be running. ydotool needs its
    daemon up (and the socket reachable) to inject input; without it the paste
    keystroke silently fails. Best-effort: check the default socket, the
    YDOTOOL_SOCKET env, then scan /proc for the process."""
    sock = os.environ.get("YDOTOOL_SOCKET")
    candidates = [sock] if sock else []
    candidates += [
        "/run/user/%d/.ydotool_socket" % _safe_uid(),
        "/tmp/.ydotool_socket",
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return True
    # Fall back to scanning the process table.
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open("/proc/%s/comm" % pid, "r") as fh:
                    if fh.read().strip() == "ydotoold":
                        return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def _safe_uid() -> int:
    try:
        return os.getuid()
    except Exception:
        return 0


def tool_present(name: str) -> bool:
    """True if an external CLI tool (xclip, wl-copy, ydotool, wtype, xdotool) is
    on PATH."""
    return shutil.which(name) is not None


def base_diagnostics() -> dict[str, object]:
    """Session-independent diagnostics shared by both Linux backends. The
    session-specific backend extends this (clipboard/paste tool checks)."""
    ev = evdev_available()
    readable = input_devices_readable()
    return {
        "session": os.environ.get("XDG_SESSION_TYPE") or "unknown",
        "evdev_installed": ev,
        "input_readable": readable,
        "in_input_group": in_input_group(),
        "user_groups": _user_groups(),
        "uinput_accessible": uinput_accessible(),
        "ydotoold_running": ydotoold_running(),
        # Human-readable hint surfaced by the UI when hotkeys won't work.
        "input_hint": (
            None if readable else
            "Cannot read /dev/input. Add your user to the 'input' group "
            "(sudo usermod -aG input %s) and re-login, or add a udev rule."
            % _username()
        ),
    }


# ===========================================================================
# === Trigger parsing (keyboard combo / mouse button / chord) ===============
# ===========================================================================
_SINGLE_MOUSE_BUTTONS = ("middle", "x1", "x2")


def _normalize(trigger: str | None) -> str:
    return (trigger or "").strip()


def parse_trigger(trigger: str) -> dict:
    """Classify a VoiceFlow trigger string into something the evdev listener can
    match. Returns one of:
        {"kind": "chord"}                              # left+right mouse
        {"kind": "mouse", "button": "middle|x1|x2"}
        {"kind": "keyboard", "keys": ["ctrl","shift","space"]}
    or {"kind": "invalid", "reason": str}.
    """
    hk = _normalize(trigger)
    if not hk:
        return {"kind": "invalid", "reason": "empty trigger"}
    flat = hk.replace(" ", "").lower()

    if flat in ("mouse:left+right", "mouse:right+left"):
        return {"kind": "chord"}

    if hk.lower().startswith("mouse:"):
        btn = hk.split(":", 1)[1].strip().lower()
        btn = {"x": "x1", "back": "x1", "forward": "x2"}.get(btn, btn)
        if btn not in _SINGLE_MOUSE_BUTTONS:
            return {"kind": "invalid",
                    "reason": "unsupported mouse button %r (use middle/x1/x2 or "
                              "left+right)" % btn}
        return {"kind": "mouse", "button": btn}

    # Keyboard combo: "ctrl+shift+space" -> ["ctrl","shift","space"].
    keys = [k.strip().lower() for k in hk.split("+") if k.strip()]
    if not keys:
        return {"kind": "invalid", "reason": "no keys"}
    return {"kind": "keyboard", "keys": keys}


# Map VoiceFlow key tokens -> evdev key-code NAMES (resolved to ints lazily so
# this table needs no evdev import on Windows). Modifiers list BOTH left/right
# variants; a combo modifier is satisfied if EITHER side is held.
_MODIFIER_ALIASES = {
    "ctrl": ("KEY_LEFTCTRL", "KEY_RIGHTCTRL"),
    "control": ("KEY_LEFTCTRL", "KEY_RIGHTCTRL"),
    "alt": ("KEY_LEFTALT", "KEY_RIGHTALT"),
    "shift": ("KEY_LEFTSHIFT", "KEY_RIGHTSHIFT"),
    "super": ("KEY_LEFTMETA", "KEY_RIGHTMETA"),
    "win": ("KEY_LEFTMETA", "KEY_RIGHTMETA"),
    "windows": ("KEY_LEFTMETA", "KEY_RIGHTMETA"),
    "cmd": ("KEY_LEFTMETA", "KEY_RIGHTMETA"),
    "meta": ("KEY_LEFTMETA", "KEY_RIGHTMETA"),
}

# Non-modifier key tokens that don't map to KEY_<UPPER> directly.
_KEY_NAME_ALIASES = {
    "space": "KEY_SPACE",
    "spacebar": "KEY_SPACE",
    "enter": "KEY_ENTER",
    "return": "KEY_ENTER",
    "esc": "KEY_ESC",
    "escape": "KEY_ESC",
    "tab": "KEY_TAB",
    "backspace": "KEY_BACKSPACE",
    "delete": "KEY_DELETE",
    "del": "KEY_DELETE",
    "insert": "KEY_INSERT",
    "ins": "KEY_INSERT",
    "home": "KEY_HOME",
    "end": "KEY_END",
    "pageup": "KEY_PAGEUP",
    "pagedown": "KEY_PAGEDOWN",
    "up": "KEY_UP",
    "down": "KEY_DOWN",
    "left": "KEY_LEFT",
    "right": "KEY_RIGHT",
    "capslock": "KEY_CAPSLOCK",
    "minus": "KEY_MINUS",
    "equal": "KEY_EQUAL",
    "comma": "KEY_COMMA",
    "dot": "KEY_DOT",
    "period": "KEY_DOT",
    "slash": "KEY_SLASH",
    "semicolon": "KEY_SEMICOLON",
}


def _evdev_key_names(token: str) -> tuple[str, ...]:
    """Return the candidate evdev key-code NAME(s) for a VoiceFlow key token.
    Modifiers return both left/right variants; everything else returns a single
    best-guess name. Resolution to ints happens at listener start."""
    t = token.strip().lower()
    if t in _MODIFIER_ALIASES:
        return _MODIFIER_ALIASES[t]
    if t in _KEY_NAME_ALIASES:
        return (_KEY_NAME_ALIASES[t],)
    if len(t) == 1 and t.isalnum():
        return ("KEY_" + t.upper(),)
    # function keys f1..f24, numpad etc. -> KEY_<UPPER>
    return ("KEY_" + t.upper(),)


def is_modifier_token(token: str) -> bool:
    return token.strip().lower() in _MODIFIER_ALIASES


# ===========================================================================
# === The shared evdev global-input listener ================================
# ===========================================================================
# evdev event values: 0 == key UP, 1 == key DOWN, 2 == AUTOREPEAT.
_EV_UP, _EV_DOWN, _EV_REPEAT = 0, 1, 2

# evdev BTN_* names for the mouse buttons we care about.
_BTN_NAMES = {
    "left": "BTN_LEFT",
    "right": "BTN_RIGHT",
    "middle": "BTN_MIDDLE",
    "x1": "BTN_SIDE",     # "back" / thumb 1
    "x2": "BTN_EXTRA",    # "forward" / thumb 2
}


class _EvdevListener:
    """A single background thread that reads ALL readable keyboard+mouse evdev
    devices and dispatches matched triggers to registered callbacks.

    One listener is shared by every trigger (keyboard combos, mouse buttons, and
    the left+right chord) so we open each device once. Registrations are added
    with :meth:`add` and removed with the returned token via :meth:`remove`.

    Suppression note: evdev can *grab* a device (EVIOCGRAB) to stop events
    reaching apps, but grabbing the keyboard/mouse wholesale would swallow ALL
    input, not just the trigger key -- unusable for a mouse, and dangerous for a
    keyboard. So on Linux we do NOT suppress the trigger event (unlike the
    Windows WH_MOUSE_LL hook which can suppress a single button). This is a
    documented platform difference: pick a side button / spare key as the
    trigger. The Windows backend's per-button suppression has no portable evdev
    equivalent.
    """

    _singleton: "_EvdevListener | None" = None
    _singleton_lock = threading.Lock()

    @classmethod
    def shared(cls) -> "_EvdevListener":
        with cls._singleton_lock:
            if cls._singleton is None:
                cls._singleton = cls()
            return cls._singleton

    def __init__(self):
        self._evdev = None
        self._ecodes = None
        self._devices: list = []
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        # Registrations.
        self._kb_regs: dict[int, dict] = {}     # token -> {keycodes:set[int],
        #                                            mods:list[set[int]], on_press,
        #                                            on_release, active:bool}
        self._mouse_regs: dict[int, dict] = {}  # token -> {code:int, on_press,
        #                                            on_release}
        self._chord_regs: dict[int, dict] = {}  # token -> {on_press}
        self._next_token = 1
        # Live key/button state (set of held evdev codes).
        self._held: set[int] = set()
        self._left_down = False
        self._right_down = False

    # ---- lifecycle ----
    def _ensure_loaded(self) -> bool:
        if self._evdev is not None:
            return True
        ev = _import_evdev()
        if ev is None:
            log.error("python-evdev is not available; Linux global triggers "
                      "cannot work. Install it and ensure /dev/input is readable.")
            return False
        self._evdev = ev
        self._ecodes = ev.ecodes
        return True

    def _resolve(self, name: str) -> int | None:
        """Resolve an evdev code NAME ('KEY_SPACE'/'BTN_SIDE') to its int."""
        try:
            return int(getattr(self._ecodes, name))
        except Exception:
            return None

    def _open_devices(self) -> None:
        """Open every readable input device that emits keys or mouse buttons."""
        ev = self._evdev
        self._devices = []
        try:
            paths = ev.list_devices()
        except Exception as exc:
            log.error("evdev list_devices failed: %s", exc)
            paths = []
        for path in paths:
            try:
                dev = ev.InputDevice(path)
                caps = dev.capabilities()
                if ev.ecodes.EV_KEY in caps:
                    self._devices.append(dev)
                else:
                    dev.close()
            except Exception:
                # Unreadable device (permissions) -> skip; diagnostics explain it.
                continue
        if not self._devices:
            log.error("No readable input devices (evdev). Check /dev/input "
                      "permissions / group 'input'.")

    def _maybe_start(self) -> bool:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return True
            if not self._ensure_loaded():
                return False
            self._open_devices()
            if not self._devices:
                return False
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, daemon=True, name="evdev-listener")
            self._thread.start()
            return True

    def _run(self) -> None:
        ev = self._evdev
        try:
            from select import select
        except Exception:
            return
        fd_to_dev = {d.fd: d for d in self._devices}
        while not self._stop.is_set():
            try:
                r, _, _ = select(list(fd_to_dev), [], [], 0.2)
            except Exception:
                break
            for fd in r:
                dev = fd_to_dev.get(fd)
                if dev is None:
                    continue
                try:
                    for event in dev.read():
                        if event.type == ev.ecodes.EV_KEY:
                            self._on_key_event(event.code, event.value)
                except OSError:
                    # Device disappeared (unplug). Drop it.
                    fd_to_dev.pop(fd, None)
                except Exception:
                    log.debug("evdev read error:\n%s", traceback.format_exc())
        for d in self._devices:
            try:
                d.close()
            except Exception:
                pass

    # ---- event handling ----
    def _on_key_event(self, code: int, value: int) -> None:
        # Mouse buttons first (BTN_*).
        if code == self._resolve("BTN_LEFT"):
            self._left_down = (value != _EV_UP)
            self._check_chord(value)
            return
        if code == self._resolve("BTN_RIGHT"):
            self._right_down = (value != _EV_UP)
            self._check_chord(value)
            return
        for tok, reg in list(self._mouse_regs.items()):
            if code == reg["code"]:
                if value == _EV_DOWN:
                    self._fire(reg.get("on_press"))
                elif value == _EV_UP:
                    self._fire(reg.get("on_release"))
                return

        # Keyboard. Track held set (ignore autorepeat for the held set so a held
        # key doesn't spam, but a real DOWN edge is what arms a combo).
        if value == _EV_DOWN:
            self._held.add(code)
            self._check_keyboard_combos(trigger_code=code)
        elif value == _EV_UP:
            self._held.discard(code)
            self._check_keyboard_release(code)
        # value == _EV_REPEAT: deliberately ignored (real-release vs autorepeat).

    def _check_chord(self, value: int) -> None:
        if self._left_down and self._right_down:
            for reg in list(self._chord_regs.values()):
                if not reg.get("active"):
                    reg["active"] = True
                    self._fire(reg.get("on_press"))
        else:
            for reg in list(self._chord_regs.values()):
                reg["active"] = False

    def _combo_satisfied(self, reg: dict, trigger_code: int | None) -> bool:
        """A keyboard combo fires when the non-modifier 'main' key goes down AND
        every modifier group has at least one of its codes currently held."""
        main = reg["keycodes"]
        # The main (non-modifier) key must be the one that just went down (or be
        # held, for modifier-only combos which we treat as press-of-last).
        if trigger_code is not None and main and trigger_code not in main:
            return False
        if main and not (main & self._held):
            return False
        for group in reg["mods"]:
            if not (group & self._held):
                return False
        return True

    def _check_keyboard_combos(self, trigger_code: int) -> None:
        for reg in list(self._kb_regs.values()):
            if reg.get("active"):
                continue
            if self._combo_satisfied(reg, trigger_code):
                reg["active"] = True
                self._fire(reg.get("on_press"))

    def _check_keyboard_release(self, code: int) -> None:
        for reg in list(self._kb_regs.values()):
            if not reg.get("active"):
                continue
            # The combo is "released" once its main key (or any required key) is
            # no longer fully held.
            if not self._combo_satisfied(reg, trigger_code=None):
                reg["active"] = False
                self._fire(reg.get("on_release"))

    @staticmethod
    def _fire(cb: Callable[[], None] | None) -> None:
        if cb is None:
            return
        try:
            cb()
        except Exception:
            log.error("trigger callback error:\n%s", traceback.format_exc())

    # ---- registration ----
    def add_keyboard(self, keys: list[str], on_press, on_release=None):
        if not self._maybe_start():
            return None
        mods: list[set[int]] = []
        main: set[int] = set()
        for tok in keys:
            names = _evdev_key_names(tok)
            codes = {c for c in (self._resolve(n) for n in names) if c is not None}
            if not codes:
                log.warning("Unknown key token %r in combo %r", tok, keys)
                continue
            if is_modifier_token(tok):
                mods.append(codes)
            else:
                main |= codes
        with self._lock:
            token = self._next_token
            self._next_token += 1
            self._kb_regs[token] = {
                "keycodes": main, "mods": mods,
                "on_press": on_press, "on_release": on_release,
                "active": False,
            }
        return ("kb", token)

    def add_mouse(self, button: str, on_press, on_release=None):
        if not self._maybe_start():
            return None
        name = _BTN_NAMES.get(button)
        if not name:
            return None
        code = self._resolve(name)
        if code is None:
            log.warning("evdev has no %s code for mouse button %r", name, button)
            return None
        with self._lock:
            token = self._next_token
            self._next_token += 1
            self._mouse_regs[token] = {
                "code": code, "on_press": on_press, "on_release": on_release,
            }
        return ("mouse", token)

    def add_chord(self, on_press):
        if not self._maybe_start():
            return None
        with self._lock:
            token = self._next_token
            self._next_token += 1
            self._chord_regs[token] = {"on_press": on_press, "active": False}
        return ("chord", token)

    def remove(self, ref) -> None:
        if not ref:
            return
        kind, token = ref
        with self._lock:
            if kind == "kb":
                self._kb_regs.pop(token, None)
            elif kind == "mouse":
                self._mouse_regs.pop(token, None)
            elif kind == "chord":
                self._chord_regs.pop(token, None)
            # Stop the listener entirely if nothing is registered.
            if not (self._kb_regs or self._mouse_regs or self._chord_regs):
                self._stop.set()


# ===========================================================================
# === TriggerHandle / TriggerBackend / Hotkeys / Mouse (shared) =============
# ===========================================================================
class TriggerHandle(_TriggerHandleABC):
    """Uniform 'stop me' wrapper over an evdev registration ref."""

    def __init__(self, kind: str, ref):
        self.kind = kind                # "keyboard" | "mouse" | "chord"
        self._ref = ref

    def stop(self) -> None:
        try:
            _EvdevListener.shared().remove(self._ref)
        except Exception:
            pass
        self._ref = None


def register_trigger(trigger: str, callback: Callable[[], None]):
    """(Re)register a global trigger via the shared evdev listener. Returns a
    :class:`TriggerHandle` on success, or None. Mirrors the Windows
    register_trigger contract used by the engine."""
    parsed = parse_trigger(trigger)
    listener = _EvdevListener.shared()
    kind = parsed["kind"]
    if kind == "invalid":
        log.error("Invalid trigger %r: %s", trigger, parsed.get("reason"))
        return None
    if kind == "chord":
        ref = listener.add_chord(callback)
        return TriggerHandle("chord", ref) if ref else None
    if kind == "mouse":
        ref = listener.add_mouse(parsed["button"], callback)
        return TriggerHandle("mouse", ref) if ref else None
    # keyboard
    ref = listener.add_keyboard(parsed["keys"], callback)
    return TriggerHandle("keyboard", ref) if ref else None


# ---------------------------------------------------------------------------
# Classification + presets for the GUI picker (kept identical to windows.py so
# the picker UX is the same on every OS).
# ---------------------------------------------------------------------------
_MOUSE_LABELS = {
    "mouse:middle": "Middle mouse button",
    "mouse:x1": "Mouse side button (back / thumb 1)",
    "mouse:x2": "Mouse side button (forward / thumb 2)",
    "mouse:left+right": "Left + Right click (chord)",
}

_CONFLICT_PRONE = {
    "mouse:left+right": ("Left+Right chord can fire on normal clicking and "
                         "swallows your right-click context menu while held."),
    "mouse:middle": ("Middle click is also paste-on-Linux / open-in-new-tab in "
                     "browsers; using it here triggers dictation everywhere. "
                     "(On Linux the click is NOT suppressed.)"),
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


def classify_trigger(trigger: str) -> dict:
    """Return {"trigger","label","clean","warning"} for a trigger string."""
    hk = _normalize(trigger)
    flat = hk.replace(" ", "").lower()
    if flat in ("mouse:right+left",):
        flat = "mouse:left+right"
    label = _MOUSE_LABELS.get(flat) or (hk if hk else "(none)")
    warning = _CONFLICT_PRONE.get(flat)
    clean = warning is None
    return {"trigger": hk, "label": label, "clean": clean, "warning": warning}


# ---------------------------------------------------------------------------
# TriggerRecorder: live "press your trigger" capture for the GUI picker.
#
# Uses a transient evdev listener that reports the FIRST recognizable key combo
# or mouse button, then stops. Unlike the Windows recorder it cannot swallow the
# reporting click (no per-event suppression on Linux), so the click also lands
# in the focused app -- acceptable for a one-shot picker.
# ---------------------------------------------------------------------------
class TriggerRecorder:
    """Capture the NEXT trigger for the GUI picker.

    on_detect(info): called once (background thread) with the classify dict.
    on_status(text): optional live status string.
    """

    def __init__(self, on_detect, on_status=None):
        self.on_detect = on_detect
        self.on_status = on_status
        self._evdev = None
        self._ecodes = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
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
        try:
            self.on_detect(classify_trigger(trigger))
        except Exception:
            log.error("TriggerRecorder on_detect error:\n%s",
                      traceback.format_exc())
        self.stop()

    def start(self):
        ev = _import_evdev()
        if ev is None:
            self._status("Cannot capture: python-evdev unavailable.")
            return
        if not input_devices_readable():
            self._status("Cannot read /dev/input (add user to group 'input').")
            return
        self._evdev = ev
        self._ecodes = ev.ecodes
        self._status("Press a key combo or click a mouse button...")
        self._thread = threading.Thread(target=self._worker, daemon=True,
                                        name="trigrec-evdev")
        self._thread.start()

    def _name_for(self, code: int) -> str | None:
        """Best-effort reverse map an evdev code int -> a VoiceFlow trigger
        string for the picker."""
        ec = self._ecodes
        # Mouse buttons.
        rev = {
            getattr(ec, "BTN_MIDDLE", None): "mouse:middle",
            getattr(ec, "BTN_SIDE", None): "mouse:x1",
            getattr(ec, "BTN_EXTRA", None): "mouse:x2",
            getattr(ec, "BTN_LEFT", None): "mouse:left+right",
            getattr(ec, "BTN_RIGHT", None): "mouse:left+right",
        }
        if code in rev:
            return rev[code]
        # Keyboard: turn KEY_SPACE -> "space", KEY_A -> "a", KEY_F9 -> "f9".
        try:
            names = ec.KEY[code]
        except Exception:
            names = None
        name = names[0] if isinstance(names, list) else names
        if isinstance(name, str) and name.startswith("KEY_"):
            return name[4:].lower()
        return None

    def _worker(self):
        ev = self._evdev
        try:
            from select import select
        except Exception:
            return
        devs = []
        try:
            for path in ev.list_devices():
                try:
                    d = ev.InputDevice(path)
                    if ev.ecodes.EV_KEY in d.capabilities():
                        devs.append(d)
                    else:
                        d.close()
                except Exception:
                    continue
        except Exception:
            devs = []
        fd_to_dev = {d.fd: d for d in devs}
        while not self._stop.is_set() and not self._fired.is_set():
            try:
                r, _, _ = select(list(fd_to_dev), [], [], 0.2)
            except Exception:
                break
            for fd in r:
                dev = fd_to_dev.get(fd)
                if not dev:
                    continue
                try:
                    for event in dev.read():
                        if event.type == ev.ecodes.EV_KEY and \
                                event.value == _EV_DOWN:
                            trig = self._name_for(event.code)
                            if trig:
                                self._emit(trig)
                                break
                except Exception:
                    continue
        for d in devs:
            try:
                d.close()
            except Exception:
                pass

    def stop(self):
        if self._stop.is_set():
            return
        self._stop.set()


# ---------------------------------------------------------------------------
# ABC implementations shared by both Linux session backends.
# ---------------------------------------------------------------------------
class LinuxTriggers(TriggerBackend):
    """The single, stable trigger API the engine uses on Linux: register ANY
    VoiceFlow trigger string (keyboard combo, mouse button, or left+right chord)
    behind one register(), backed by the shared evdev listener."""

    def register(self, trigger, callback):
        return register_trigger(trigger, callback)

    def classify(self, trigger):
        return classify_trigger(trigger)

    @property
    def presets(self):
        return list(PRESETS)


class LinuxHotkeys(HotkeyBackend):
    """HotkeyBackend for plain keyboard combos via evdev."""

    def __init__(self):
        self._registered: list[tuple[str, Callable, Callable | None]] = []
        self._handles: list[TriggerHandle] = []

    def register(self, combo, on_press, on_release=None):
        self._registered.append((combo, on_press, on_release))

    def start(self):
        live = []
        for combo, press, release in self._registered:
            parsed = parse_trigger(combo)
            if parsed["kind"] != "keyboard":
                log.warning("LinuxHotkeys ignoring non-keyboard combo %r", combo)
                continue
            ref = _EvdevListener.shared().add_keyboard(
                parsed["keys"], press, release)
            if ref:
                live.append(TriggerHandle("keyboard", ref))
        self._handles = live

    def stop(self):
        for h in self._handles:
            try:
                h.stop()
            except Exception:
                pass
        self._handles = []

    @property
    def supports_hold_mode(self):
        # evdev distinguishes a real key-up from autorepeat, so push-to-talk is
        # reliable (the listener tracks press/release edges).
        return True


class LinuxMouse(MouseBackend):
    """MouseBackend for mouse-button + chord triggers via evdev. NOTE: Linux
    cannot suppress a single button event (see _EvdevListener docstring), so
    supports_side_buttons means 'can detect', not 'can suppress'."""

    supports_side_buttons = True

    def __init__(self):
        self._registered: list[tuple[str, Callable, Callable | None]] = []
        self._handles: list[TriggerHandle] = []

    def register(self, button, on_press, on_release=None):
        self._registered.append((button, on_press, on_release))

    def start(self):
        live = []
        for button, press, release in self._registered:
            trig = button if button.startswith("mouse:") else "mouse:" + button
            parsed = parse_trigger(trig)
            if parsed["kind"] == "chord":
                ref = _EvdevListener.shared().add_chord(press)
                if ref:
                    live.append(TriggerHandle("chord", ref))
            elif parsed["kind"] == "mouse":
                ref = _EvdevListener.shared().add_mouse(
                    parsed["button"], press, release)
                if ref:
                    live.append(TriggerHandle("mouse", ref))
            else:
                log.warning("LinuxMouse ignoring %r (%s)", button,
                            parsed.get("reason"))
        self._handles = live

    def stop(self):
        for h in self._handles:
            try:
                h.stop()
            except Exception:
                pass
        self._handles = []


class LinuxPermissions(Permissions):
    """Linux has no macOS-style TCC grants; the real 'permissions' are: can we
    read /dev/input (group 'input' / udev) for hotkeys, and can we synthesize
    paste (uinput for ydotool / an X11 or Wayland tool). check() maps these onto
    the ABC's {"accessibility","input_monitoring","mic"} keys plus extra Linux
    keys, and exposes full diagnostics() for the UI."""

    def check(self):
        diag = self.diagnostics()
        return {
            # 'input_monitoring' == can we read the global input (hotkeys).
            "input_monitoring": bool(diag.get("input_readable")),
            # 'accessibility' == can we synthesize the paste keystroke.
            "accessibility": bool(diag.get("can_paste")),
            # Mic permission isn't gated on Linux (PortAudio/ALSA/Pulse handle
            # device access); report True unless a backend overrides.
            "mic": True,
            # Linux extras (the UI can show these directly):
            "input_readable": bool(diag.get("input_readable")),
            "in_input_group": bool(diag.get("in_input_group")),
            "uinput_accessible": bool(diag.get("uinput_accessible")),
            "ydotoold_running": bool(diag.get("ydotoold_running")),
        }

    def request(self, name):
        # There is no programmatic permission prompt on Linux. The UI should show
        # the guidance string from diagnostics() (e.g. usermod -aG input).
        log.info("Linux permission %r must be granted manually; see diagnostics "
                 "for the exact command.", name)
        return None

    def all_ok(self):
        diag = self.diagnostics()
        return bool(diag.get("input_readable")) and bool(diag.get("can_paste"))

    def diagnostics(self) -> dict:
        """Full, UI-ready diagnostics. Subclasses extend with their paste/
        clipboard tool checks and set 'can_paste'."""
        d = dict(base_diagnostics())
        d["can_paste"] = False  # overridden by the session backend
        return d
