"""
voiceflow.platform.macos - the macOS platform backend.

Implements the same surface as ``voiceflow.platform.windows`` (so the engine,
GUI and ``voiceflow.triggers`` shim plug in unchanged) on top of native macOS
APIs via pyobjc + pynput:

  * Clipboard      -> ``NSPasteboard`` (AppKit): format-aware snapshot of every
                      eager UTI payload (``pasteboardItems`` -> ``types`` ->
                      ``dataForType:``) and a faithful restore that re-declares
                      those exact types. ``paste_text`` mirrors the verified
                      Windows paste cycle: snapshot -> set text -> Cmd+V ->
                      settle -> restore.
  * Paster         -> Cmd+V synthesized with Quartz ``CGEvent`` (configurable
                      chord, e.g. "cmd+v" or "shift+cmd+v"); falls back to
                      ``pynput`` if Quartz is unavailable. Needs the
                      **Accessibility** TCC permission to post events.
  * Triggers       -> ``pynput`` global hotkeys for keyboard combos (needs the
                      **Input Monitoring** TCC permission) and a Quartz
                      ``CGEventTap`` for mouse side buttons + the left+right
                      chord, with the same suppression / hold-and-forward intent
                      as the Windows ``WH_MOUSE_LL`` hooks.
  * Permissions    -> ``AXIsProcessTrusted`` (Accessibility), a best-effort
                      Input-Monitoring probe, and a mic probe (AVFoundation),
                      plus ``open x-apple.systempreferences:...`` deep links to
                      the exact Settings panes.

IMPORTANT (cross-platform import safety): all pyobjc / pynput / Quartz imports
are performed lazily and guarded. Importing this module on a non-macOS box (or a
Mac without pyobjc installed) MUST NOT raise -- the factory in
``voiceflow.platform.__init__`` only loads it on ``sys.platform == "darwin"``,
but keeping it import-safe everywhere lets the test suite import and introspect
it and lets ``voiceflow.triggers`` fall back gracefully. Anything that genuinely
needs pyobjc raises a clear ``RuntimeError`` only when actually *used*.

What can be verified on the Windows dev box: this module *imports* cleanly and
its OS-agnostic logic (trigger classification, presets, chord parsing, the
clipboard adapter shapes) is exercisable. What CANNOT be verified here and needs
a real Mac (see docs/PRODUCTION_PLAN.md sections 2.4 and 7.2): the actual TCC
permission prompts/flow, NSPasteboard round-trips, CGEvent Cmd+V into a focused
app, the CGEventTap side-button suppression, and notarized-.app grant binding.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
import traceback
from typing import Callable

from .base import (
    ClipboardBackend,
    HotkeyBackend,
    MouseBackend,
    Paster,
    Permissions,
    TriggerBackend,
)
from .base import TriggerHandle as _TriggerHandleABC

log = logging.getLogger("voiceflow.platform.macos")


# ===========================================================================
# === Lazy / guarded pyobjc imports =========================================
# ===========================================================================
# We never import pyobjc at module top level: that would make `import
# voiceflow.platform.macos` fail on any machine without pyobjc (every Windows
# dev box). Instead each helper imports what it needs on first use and caches
# the result. `_require(...)` raises a clear, actionable error only when a
# pyobjc-dependent feature is actually exercised.

_PYOBJC_ERR = (
    "pyobjc is required for OpenVerba on macOS. Install the GUI build's deps "
    "(it bundles pyobjc-core + pyobjc-framework-{Cocoa,Quartz,AVFoundation,"
    "ApplicationServices}); for a source checkout run "
    "`pip install pyobjc-framework-Cocoa pyobjc-framework-Quartz "
    "pyobjc-framework-AVFoundation`."
)


def _try_import(modname: str):
    """Import a module, returning None instead of raising. Used so a missing
    pyobjc framework degrades to a clear runtime error at the call site rather
    than an ImportError at module import time."""
    try:
        return __import__(modname, fromlist=["*"])
    except Exception:
        return None


def _appkit():
    return _try_import("AppKit")


def _quartz():
    return _try_import("Quartz")


def _require(mod, what: str):
    if mod is None:
        raise RuntimeError(f"{what} unavailable: {_PYOBJC_ERR}")
    return mod


# ===========================================================================
# === Clipboard: NSPasteboard format-aware snapshot/restore =================
# ===========================================================================
class PasteboardManager:
    """macOS counterpart of the Windows ``ClipboardManager``.

    Snapshots EVERY eager UTI payload on the general pasteboard so the user's
    clipboard (rich text, HTML, images, file URLs, ...) is faithfully restored
    after we paste the transcript. Like the Windows manager it serializes all
    access under a lock and uses the same snapshot -> set text -> paste ->
    settle -> restore cycle.

    Snapshot shape: ``{uti_string: bytes}`` (plus the convenience key
    ``"public.utf8-plain-text"`` always present when text exists). This satisfies
    the ClipboardBackend ABC's ``dict[str, bytes | str]`` contract. Lazily-
    promised / delayed-provided types that report no eager ``dataForType:`` are
    skipped (documented loss), exactly as the Windows backend skips lazy formats.
    """

    # The UTI for plain UTF-8 text (NSPasteboardTypeString on modern macOS).
    TEXT_UTI = "public.utf8-plain-text"

    def __init__(self, restore_delay_ms: int = 200, read_timeout_ms: int = 2500):
        self.restore_floor_s = max(restore_delay_ms / 1000.0, 0.0)
        self.read_timeout_s = max(read_timeout_ms / 1000.0, 0.3)
        self._lock = threading.RLock()
        # Paster used by the paste cycle (Cmd+V). Created on first use.
        self._paster: MacPaster | None = None

    # ---- low-level ----
    def _general_pasteboard(self):
        AppKit = _require(_appkit(), "Clipboard (NSPasteboard)")
        return AppKit.NSPasteboard.generalPasteboard()

    @staticmethod
    def _nsdata_to_bytes(nsdata) -> bytes | None:
        if nsdata is None:
            return None
        try:
            # NSData supports the buffer protocol via bytes() under pyobjc.
            return bytes(nsdata)
        except Exception:
            try:
                return nsdata.bytes().tobytes()  # older pyobjc
            except Exception:
                return None

    # ---- save / snapshot ----
    def save(self) -> dict[str, bytes]:
        """Snapshot all eager pasteboard payloads -> {uti: bytes}. Returns {} on
        failure (the app still works; it just can't restore the prior clipboard).

        We iterate ``pasteboardItems`` and, for each item, every declared
        ``types()``; ``dataForType:`` returns ``None`` for types whose owner only
        promises them lazily, which we skip (round-trip would corrupt them)."""
        saved: dict[str, bytes] = {}
        with self._lock:
            try:
                pb = self._general_pasteboard()
            except Exception as exc:
                log.warning("NSPasteboard unavailable for snapshot: %s", exc)
                return saved
            try:
                items = pb.pasteboardItems() or []
                # Single-item is the overwhelmingly common case; if multiple
                # items exist we still capture the first (NSPasteboard write of
                # multiple items needs distinct NSPasteboardItem objects, handled
                # in restore). Most clipboards have exactly one item.
                for item in items:
                    for uti in (item.types() or []):
                        if uti in saved:
                            continue
                        try:
                            data = item.dataForType_(uti)
                        except Exception:
                            data = None
                        raw = self._nsdata_to_bytes(data)
                        if raw is not None:
                            saved[str(uti)] = raw
                    # Only the first item is restored verbatim; stop after it to
                    # keep restore deterministic.
                    break
            except Exception:
                log.error("NSPasteboard snapshot error:\n%s",
                          traceback.format_exc())
        return saved

    def snapshot(self) -> dict[str, bytes]:
        return self.save()

    # ---- set text ----
    def _set_text_immediate(self, text: str) -> None:
        """Replace the pasteboard contents with the transcript as plain text
        (concrete data, declared eagerly -> available the instant Cmd+V fires)."""
        AppKit = _require(_appkit(), "Clipboard (NSPasteboard)")
        with self._lock:
            pb = AppKit.NSPasteboard.generalPasteboard()
            pb.clearContents()
            # setString:forType: writes concrete bytes (not a lazy promise).
            pb.setString_forType_(text, AppKit.NSPasteboardTypeString)

    def set_text(self, text: str) -> None:
        self._set_text_immediate(text)

    # ---- restore ----
    def restore(self, saved: dict[str, bytes | str]) -> None:
        """Re-declare and write back every snapshotted UTI as concrete data."""
        if not saved:
            return
        AppKit = _require(_appkit(), "Clipboard (NSPasteboard)")
        with self._lock:
            pb = AppKit.NSPasteboard.generalPasteboard()
            try:
                pb.clearContents()
                types = list(saved.keys())
                # declareTypes:owner: must precede setData:forType: writes.
                pb.declareTypes_owner_(types, None)
                for uti, data in saved.items():
                    try:
                        if isinstance(data, str):
                            pb.setString_forType_(data, uti)
                        else:
                            nsdata = AppKit.NSData.dataWithBytes_length_(
                                bytes(data), len(data))
                            pb.setData_forType_(nsdata, uti)
                    except Exception:
                        continue  # one bad type must not kill the rest
            except Exception:
                log.error("NSPasteboard restore error:\n%s",
                          traceback.format_exc())

    def _clear(self) -> None:
        try:
            AppKit = _require(_appkit(), "Clipboard (NSPasteboard)")
            with self._lock:
                AppKit.NSPasteboard.generalPasteboard().clearContents()
        except Exception:
            pass

    # ---- the full paste cycle (mirrors WindowsClipboardManager.paste_text) ----
    def paste_text(self, text: str) -> bool:
        """Save full clipboard -> set the transcript as concrete data -> Cmd+V ->
        settle so the target finishes pasting -> restore the original clipboard
        (in finally, so the original is always put back).

        Returns True if we put the text on the clipboard and sent the paste."""
        if not text:
            return False
        if self._paster is None:
            self._paster = MacPaster()
        saved = self.save()
        try:
            self._set_text_immediate(text)
            time.sleep(0.03)            # let the pasteboard settle before paste
            self._paster.paste()
            # Give the target app time to read the pasteboard before restore.
            time.sleep(max(self.restore_floor_s, 0.15))
        finally:
            if saved:
                self.restore(saved)
            else:
                self._clear()
        return True


class MacClipboard(ClipboardBackend):
    """ClipboardBackend ABC adapter around ``PasteboardManager`` (parity with
    ``WindowsClipboard``). The engine uses the richer ``PasteboardManager``
    directly via ``make_clipboard``; this satisfies callers wanting the ABC."""

    def __init__(self, restore_delay_ms: int = 200, read_timeout_ms: int = 2500):
        self._mgr = PasteboardManager(restore_delay_ms, read_timeout_ms)

    def snapshot(self) -> dict[str, bytes | str]:
        return self._mgr.snapshot()

    def restore(self, snap: dict[str, bytes | str]) -> None:
        self._mgr.restore(snap)

    def set_text(self, text: str) -> None:
        self._mgr.set_text(text)

    # passthroughs so this can stand in for a PasteboardManager if desired
    def save(self) -> dict[str, bytes]:
        return self._mgr.save()

    def paste_text(self, text: str) -> bool:
        return self._mgr.paste_text(text)


# ===========================================================================
# === Paster: Cmd+V via Quartz CGEvent (configurable chord) =================
# ===========================================================================
# Virtual keycodes (ANSI, layout-independent) for the chord parser.
_VK = {
    "v": 0x09,
    "insert": 0x72,   # help/insert key (rarely useful on Mac, kept for parity)
}
# Modifier flag masks (Quartz CGEventFlags). Filled lazily from Quartz to avoid
# importing it at module top; these literal values match the public constants.
_CG_FLAG = {
    "cmd": 1 << 20,     # kCGEventFlagMaskCommand
    "command": 1 << 20,
    "shift": 1 << 17,   # kCGEventFlagMaskShift
    "ctrl": 1 << 18,    # kCGEventFlagMaskControl
    "control": 1 << 18,
    "alt": 1 << 19,     # kCGEventFlagMaskAlternate (Option)
    "option": 1 << 19,
}
# Virtual keycodes for the modifier keys themselves (for the pynput fallback).
_MOD_KEYS = ("cmd", "command", "shift", "ctrl", "control", "alt", "option")


def _parse_chord(chord: str) -> tuple[int, int, str]:
    """"cmd+v" / "shift+cmd+v" / "ctrl+v" -> (keycode, flags_mask, keychar).

    Returns the Quartz keycode for the non-modifier key, the OR of the modifier
    flag masks, and the final key char (for the pynput fallback)."""
    parts = [p.strip().lower() for p in (chord or "cmd+v").split("+") if p.strip()]
    flags = 0
    keychar = "v"
    for p in parts:
        if p in _CG_FLAG:
            flags |= _CG_FLAG[p]
        else:
            keychar = p
    keycode = _VK.get(keychar, _VK["v"])
    return keycode, flags, keychar


class MacPaster(Paster):
    """Synthesizes the paste chord (default Cmd+V).

    Primary path: Quartz ``CGEventCreateKeyboardEvent`` (down+up) with the
    modifier flags set on the event, posted to ``kCGHIDEventTap``. This needs the
    **Accessibility** TCC permission; without it macOS silently drops the event
    (handled by the first-run permission UI). Fallback: ``pynput`` keyboard
    controller (same permission requirement, simpler API)."""

    def __init__(self):
        self._chord = "cmd+v"
        self._keycode, self._flags, self._keychar = _parse_chord(self._chord)

    def set_chord(self, chord: str) -> None:
        self._chord = chord or "cmd+v"
        self._keycode, self._flags, self._keychar = _parse_chord(self._chord)

    def paste(self) -> None:
        if self._paste_quartz():
            return
        self._paste_pynput()

    # -- Quartz CGEvent path --
    def _paste_quartz(self) -> bool:
        Quartz = _quartz()
        if Quartz is None:
            return False
        try:
            src = None
            try:
                src = Quartz.CGEventSourceCreate(
                    Quartz.kCGEventSourceStateHIDSystemState)
            except Exception:
                src = None
            down = Quartz.CGEventCreateKeyboardEvent(src, self._keycode, True)
            up = Quartz.CGEventCreateKeyboardEvent(src, self._keycode, False)
            Quartz.CGEventSetFlags(down, self._flags)
            Quartz.CGEventSetFlags(up, self._flags)
            tap = Quartz.kCGHIDEventTap
            Quartz.CGEventPost(tap, down)
            time.sleep(0.005)
            Quartz.CGEventPost(tap, up)
            return True
        except Exception as exc:
            log.warning("Quartz Cmd+V failed (%s); trying pynput.", exc)
            return False

    # -- pynput fallback --
    def _paste_pynput(self) -> None:
        pynput = _try_import("pynput.keyboard")
        if pynput is None:
            log.error("Cannot paste: neither Quartz nor pynput is available. %s",
                      _PYOBJC_ERR)
            return
        try:
            kb = pynput.keyboard
            controller = kb.Controller()
            mods = []
            for p in self._chord.split("+"):
                p = p.strip().lower()
                if p in ("cmd", "command"):
                    mods.append(kb.Key.cmd)
                elif p in ("shift",):
                    mods.append(kb.Key.shift)
                elif p in ("ctrl", "control"):
                    mods.append(kb.Key.ctrl)
                elif p in ("alt", "option"):
                    mods.append(kb.Key.alt)
            key = self._keychar
            with _pressed(controller, mods):
                controller.press(key)
                controller.release(key)
        except Exception as exc:
            log.warning("pynput Cmd+V fallback failed: %s", exc)


class _pressed:
    """Context manager: hold a list of pynput modifier keys for the body."""

    def __init__(self, controller, mods):
        self._c = controller
        self._mods = mods

    def __enter__(self):
        for m in self._mods:
            self._c.press(m)
        return self

    def __exit__(self, *exc):
        for m in reversed(self._mods):
            try:
                self._c.release(m)
            except Exception:
                pass
        return False


# ===========================================================================
# === Triggers: pynput keyboard hotkeys + Quartz CGEventTap mouse buttons ===
# ===========================================================================
# macOS button numbers in CGEvent / NSEvent: 0=left, 1=right, 2=middle,
# 3=back (x1 / "other"), 4=forward (x2). The CGEventTap reports the button via
# kCGMouseEventButtonNumber on the OtherMouseDown/Up events; left/right/middle
# have their own event types.
_MAC_BUTTON_NUMBER = {"middle": 2, "x1": 3, "x2": 4}
_SINGLE_MOUSE_BUTTONS = ("middle", "x1", "x2")


class TriggerHandle(_TriggerHandleABC):
    """Uniform 'stop me' wrapper over keyboard / mouse triggers (parity with the
    Windows ``TriggerHandle``)."""

    def __init__(self, kind: str, handle):
        self.kind = kind          # "keyboard" | "mouse" | "chord"
        self._handle = handle

    def stop(self) -> None:
        try:
            if self._handle is not None:
                self._handle.stop()
        except Exception:
            pass
        self._handle = None


# ---------------------------------------------------------------------------
# Keyboard hotkey via pynput GlobalHotKeys (needs Input Monitoring TCC).
# ---------------------------------------------------------------------------
class _KeyboardHotkey:
    """Wraps a pynput ``GlobalHotKeys`` listener for a single combo. pynput is
    the plan's default macOS hotkey lib; it requires the **Input Monitoring** TCC
    grant and won't fire without it (surfaced by the first-run permission UI)."""

    def __init__(self, combo: str, callback: Callable[[], None]):
        self._listener = None
        kbmod = _try_import("pynput.keyboard")
        if kbmod is None:
            raise RuntimeError(_PYOBJC_ERR + " (pynput is also required)")
        kb = kbmod.keyboard
        spec = self._to_pynput_spec(combo)
        self._listener = kb.GlobalHotKeys({spec: self._guard(callback)})

    @staticmethod
    def _guard(callback):
        def _fire():
            try:
                callback()
            except Exception:
                log.error("Keyboard trigger callback error:\n%s",
                          traceback.format_exc())
        return _fire

    @staticmethod
    def _to_pynput_spec(combo: str) -> str:
        """Convert "ctrl+shift+space" -> pynput's "<ctrl>+<shift>+<space>".

        On macOS we map "ctrl" to <ctrl> as written; users wanting Command should
        write "cmd". Single character keys pass through verbatim."""
        out = []
        special = {
            "ctrl": "<ctrl>", "control": "<ctrl>",
            "cmd": "<cmd>", "command": "<cmd>", "win": "<cmd>", "super": "<cmd>",
            "alt": "<alt>", "option": "<alt>",
            "shift": "<shift>",
            "space": "<space>", "tab": "<tab>", "enter": "<enter>",
            "return": "<enter>", "esc": "<esc>", "escape": "<esc>",
        }
        for raw in (combo or "").split("+"):
            p = raw.strip().lower()
            if not p:
                continue
            if p in special:
                out.append(special[p])
            elif len(p) == 1:
                out.append(p)
            elif p.startswith("f") and p[1:].isdigit():
                out.append("<%s>" % p)        # function keys: <f9>
            else:
                out.append("<%s>" % p)         # best-effort named key
        return "+".join(out)

    def start(self) -> None:
        if self._listener is not None:
            self._listener.start()

    def stop(self) -> None:
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:
                pass
            self._listener = None


# ---------------------------------------------------------------------------
# Mouse side-button + left+right chord via Quartz CGEventTap.
#
# This is the macOS analogue of the Windows WH_MOUSE_LL hooks. A CGEventTap at
# kCGHIDEventTap can OBSERVE and (when created with the default, non-listenonly
# option) SUPPRESS events by returning NULL from the callback -- the same
# "suppress both down and up so the app never sees the trigger click" behaviour
# the Windows SingleButtonHook/MouseChordHook implement. It needs the
# Accessibility (event-tap) permission.
#
# NOTE: a CGEventTap must run with a CFRunLoop. We run that loop on a dedicated
# daemon thread (parity with the Windows mouse-hook message loop on its own
# thread) and stop it by disabling the tap + stopping the run loop.
# ---------------------------------------------------------------------------
class _MouseTapBase:
    """Runs a CGEventTap on its own CFRunLoop thread and dispatches button
    events to a subclass ``_on_event(event_type, button_number) -> bool`` where a
    True return SUPPRESSES the event (callback returns NULL to the tap)."""

    def __init__(self):
        self._thread = None
        self._tap = None
        self._runloop = None
        self._source = None
        self._stop = False
        self._Quartz = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="macmousetap")
        self._thread.start()

    def stop(self) -> None:
        self._stop = True
        Q = self._Quartz
        try:
            if Q is not None and self._tap is not None:
                Q.CGEventTapEnable(self._tap, False)
            if Q is not None and self._runloop is not None:
                Q.CFRunLoopStop(self._runloop)
        except Exception:
            pass

    # subclasses override: return True to SUPPRESS this event.
    def _on_event(self, event_type: int, button_number: int) -> bool:
        return False

    def _event_mask(self, Q) -> int:
        # Watch other-mouse (side/middle) down+up AND left/right down+up (for the
        # chord). CGEventMaskBit(t) == 1 << t.
        types = [
            Q.kCGEventOtherMouseDown, Q.kCGEventOtherMouseUp,
            Q.kCGEventLeftMouseDown, Q.kCGEventLeftMouseUp,
            Q.kCGEventRightMouseDown, Q.kCGEventRightMouseUp,
        ]
        mask = 0
        for t in types:
            mask |= (1 << int(t))
        return mask

    def _run(self) -> None:
        Q = _quartz()
        if Q is None:
            log.error("Quartz unavailable; mouse triggers disabled. %s",
                      _PYOBJC_ERR)
            return
        self._Quartz = Q

        def _callback(proxy, etype, event, refcon):
            try:
                # tap-disabled events (timeout / user input) -> re-enable.
                if etype in (Q.kCGEventTapDisabledByTimeout,
                             Q.kCGEventTapDisabledByUserInput):
                    if self._tap is not None:
                        Q.CGEventTapEnable(self._tap, True)
                    return event
                btn = 0
                try:
                    btn = int(Q.CGEventGetIntegerValueField(
                        event, Q.kCGMouseEventButtonNumber))
                except Exception:
                    btn = 0
                if self._on_event(int(etype), btn):
                    return None     # suppress: app never sees this event
            except Exception:
                log.error("mouse tap handler error:\n%s",
                          traceback.format_exc())
            return event

        try:
            self._tap = Q.CGEventTapCreate(
                Q.kCGHIDEventTap,
                Q.kCGHeadInsertEventTap,
                Q.kCGEventTapOptionDefault,   # default = can suppress (not listenonly)
                self._event_mask(Q),
                _callback,
                None,
            )
        except Exception:
            self._tap = None
        if not self._tap:
            log.error("Failed to create CGEventTap for mouse triggers (needs "
                      "Accessibility / Input Monitoring permission).")
            return
        self._source = Q.CFMachPortCreateRunLoopSource(None, self._tap, 0)
        self._runloop = Q.CFRunLoopGetCurrent()
        Q.CFRunLoopAddSource(self._runloop, self._source,
                             Q.kCFRunLoopCommonModes)
        Q.CGEventTapEnable(self._tap, True)
        log.info("CGEventTap installed (%s).", type(self).__name__)
        # Run the loop until stop() stops it (or in short slices so _stop is seen).
        while not self._stop:
            try:
                Q.CFRunLoopRunInMode(Q.kCFRunLoopDefaultMode, 0.25, False)
            except Exception:
                break
        try:
            Q.CGEventTapEnable(self._tap, False)
        except Exception:
            pass
        self._tap = None


class MacSingleButtonTap(_MouseTapBase):
    """Fire ``callback`` on the DOWN of a single side/middle button (middle / x1
    / x2), suppressing BOTH its down and up so no browser back/forward or
    middle-click action leaks (parity with the Windows ``SingleButtonHook``)."""

    def __init__(self, button: str, callback: Callable[[], None]):
        super().__init__()
        self.button = button
        self.callback = callback
        self._want = _MAC_BUTTON_NUMBER.get(button, 2)
        self._down_suppressed = False

    def _fire(self):
        try:
            self.callback()
        except Exception:
            log.error("Mouse-button callback error:\n%s",
                      traceback.format_exc())

    def _on_event(self, etype: int, btn: int) -> bool:
        Q = self._Quartz
        if Q is None:
            return False
        if etype == int(Q.kCGEventOtherMouseDown) and btn == self._want:
            self._fire()
            self._down_suppressed = True
            return True
        if etype == int(Q.kCGEventOtherMouseUp) and btn == self._want:
            self._down_suppressed = False
            return True
        return False


class MacChordTap(_MouseTapBase):
    """Fire ``callback`` when Left and Right are held simultaneously. The first
    button passes through as an ordinary click; the second button that completes
    the chord is fully suppressed (down AND up). We only suppress a button's UP
    if we suppressed its DOWN (parity with the Windows ``MouseChordHook``)."""

    def __init__(self, callback: Callable[[], None]):
        super().__init__()
        self.callback = callback
        self._l_down = self._r_down = False
        self._l_seen = self._r_seen = False

    def _fire(self):
        try:
            self.callback()
        except Exception:
            log.error("Mouse-chord callback error:\n%s",
                      traceback.format_exc())

    def _on_event(self, etype: int, btn: int) -> bool:
        Q = self._Quartz
        if Q is None:
            return False
        suppress = False
        if etype == int(Q.kCGEventLeftMouseDown):
            if self._r_down:
                self._fire(); suppress = True; self._l_seen = False
            else:
                self._l_seen = True
            self._l_down = True
        elif etype == int(Q.kCGEventRightMouseDown):
            if self._l_down:
                self._fire(); suppress = True; self._r_seen = False
            else:
                self._r_seen = True
            self._r_down = True
        elif etype == int(Q.kCGEventLeftMouseUp):
            self._l_down = False
            if not self._l_seen:
                suppress = True
            self._l_seen = False
        elif etype == int(Q.kCGEventRightMouseUp):
            self._r_down = False
            if not self._r_seen:
                suppress = True
            self._r_seen = False
        return suppress


# ---------------------------------------------------------------------------
# register_trigger: the verbatim-parity entry point (mirrors windows.py).
# ---------------------------------------------------------------------------
def _normalize(trigger: str) -> str:
    return (trigger or "").strip()


def register_trigger(trigger: str, callback: Callable[[], None]):
    """(Re)register the global trigger. Returns a ``TriggerHandle`` on success,
    or None. Caller should ``.stop()`` the previous handle first. Mirrors the
    Windows ``register_trigger`` dispatch exactly (chord / single mouse button /
    keyboard combo)."""
    hk = _normalize(trigger)
    if not hk:
        return None

    flat = hk.replace(" ", "").lower()

    # Left+Right chord -> CGEventTap that handles suppression itself.
    if flat in ("mouse:left+right", "mouse:right+left"):
        try:
            tap = MacChordTap(callback)
            tap.start()
            return TriggerHandle("chord", tap)
        except Exception:
            log.error("Could not start mouse-chord tap:\n%s",
                      traceback.format_exc())
            return None

    # Single mouse button (middle / x1 / x2) -> suppressing CGEventTap.
    if hk.lower().startswith("mouse:"):
        btn = hk.split(":", 1)[1].strip().lower()
        btn = {"x": "x1", "back": "x1", "forward": "x2"}.get(btn, btn)
        if btn not in _SINGLE_MOUSE_BUTTONS:
            log.error("Unsupported mouse trigger '%s' (use middle/x1/x2 or "
                      "left+right).", hk)
            return None
        try:
            tap = MacSingleButtonTap(btn, callback)
            tap.start()
            return TriggerHandle("mouse", tap)
        except Exception:
            log.error("Could not register mouse button '%s':\n%s",
                      btn, traceback.format_exc())
            return None

    # Keyboard combo -> pynput GlobalHotKeys (needs Input Monitoring TCC).
    try:
        hot = _KeyboardHotkey(hk, callback)
        hot.start()
        return TriggerHandle("keyboard", hot)
    except Exception:
        log.error("Could not register trigger '%s':\n%s",
                  hk, traceback.format_exc())
        return None


# ===========================================================================
# === Classification + presets (parity with windows.py) =====================
# ===========================================================================
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
                     "browsers; using it here suppresses that everywhere."),
}

# macOS presets: a side button is still the most ergonomic; the safe keyboard
# combos use the Mac-native Command key (and Control+Option) rather than the
# Windows Ctrl-centric defaults, but the engine's stored default ("ctrl+shift+
# space") still works.
PRESETS = [
    {"trigger": "mouse:x1", "label": _MOUSE_LABELS["mouse:x1"],
     "clean": True,
     "note": "A thumb/side button is the most ergonomic, conflict-free choice."},
    {"trigger": "mouse:x2", "label": _MOUSE_LABELS["mouse:x2"],
     "clean": True, "note": "The other thumb/side button."},
    {"trigger": "f9", "label": "F9 key", "clean": True,
     "note": "A free function key that rarely clashes with apps."},
    {"trigger": "cmd+shift+space", "label": "Cmd + Shift + Space",
     "clean": True, "note": "A safe global combo on macOS."},
    {"trigger": "ctrl+option+d", "label": "Control + Option + D", "clean": True,
     "note": "Another safe global combo."},
]


def classify_trigger(trigger: str) -> dict:
    """Return {"trigger","label","clean","warning"} for a trigger string
    (identical contract to the Windows classifier)."""
    hk = _normalize(trigger)
    flat = hk.replace(" ", "").lower()
    if flat in ("mouse:right+left",):
        flat = "mouse:left+right"
    label = _MOUSE_LABELS.get(flat) or (hk if hk else "(none)")
    warning = _CONFLICT_PRONE.get(flat)
    clean = warning is None
    return {"trigger": hk, "label": label, "clean": clean, "warning": warning}


# ===========================================================================
# === TriggerRecorder: live-detect the next trigger for the GUI picker ======
# ===========================================================================
class TriggerRecorder:
    """Capture the NEXT trigger for the GUI picker (parity with the Windows
    recorder's public API: on_detect(info) / on_status(text), start(), stop(),
    auto-stop after the first detection).

    Implementation: a pynput keyboard listener captures the next key combo, and a
    short-lived CGEventTap (listen-only style: it reports the one button then
    stops) reports any standard mouse button. Callbacks fire on background
    threads, so the GUI marshals them with ``.after`` (the picker already does)."""

    def __init__(self, on_detect, on_status=None):
        self.on_detect = on_detect
        self.on_status = on_status
        self._mouse_tap = None
        self._kb_listener = None
        self._stopped = threading.Event()
        self._fired = threading.Event()
        self._pressed_mods: set = set()

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
        self.stop()

    # -- mouse capture: a one-shot reporting tap --
    class _RecorderMouseTap(_MouseTapBase):
        def __init__(self, report):
            super().__init__()
            self._report = report

        def _on_event(self, etype: int, btn: int) -> bool:
            Q = self._Quartz
            if Q is None:
                return False
            trig = None
            if etype == int(Q.kCGEventOtherMouseDown):
                if btn == 2:
                    trig = "mouse:middle"
                elif btn == 3:
                    trig = "mouse:x1"
                elif btn == 4:
                    trig = "mouse:x2"
            elif etype in (int(Q.kCGEventLeftMouseDown),
                           int(Q.kCGEventRightMouseDown)):
                trig = "mouse:left+right"   # offered as the chord option
            if trig is not None:
                try:
                    self._report(trig)
                except Exception:
                    pass
                return True   # swallow just this reporting click
            return False

    def start(self):
        self._status("Press a key combo or click a mouse button...")
        # Mouse: short-lived reporting tap.
        try:
            self._mouse_tap = self._RecorderMouseTap(self._emit)
            self._mouse_tap.start()
        except Exception:
            self._mouse_tap = None
        # Keyboard: a pynput listener that reports on the first combo.
        self._start_keyboard()

    def _start_keyboard(self):
        kbmod = _try_import("pynput.keyboard")
        if kbmod is None:
            return
        kb = kbmod.keyboard

        mod_names = {
            kb.Key.cmd: "cmd", kb.Key.cmd_l: "cmd", kb.Key.cmd_r: "cmd",
            kb.Key.ctrl: "ctrl", kb.Key.ctrl_l: "ctrl", kb.Key.ctrl_r: "ctrl",
            kb.Key.alt: "option", kb.Key.alt_l: "option", kb.Key.alt_r: "option",
            kb.Key.shift: "shift", kb.Key.shift_l: "shift",
            kb.Key.shift_r: "shift",
        }

        def _key_name(key):
            if key in mod_names:
                return None  # tracked separately as a modifier
            try:
                if hasattr(key, "char") and key.char:
                    return key.char.lower()
            except Exception:
                pass
            name = getattr(key, "name", None) or str(key).replace("Key.", "")
            return name.lower()

        def on_press(key):
            if self._stopped.is_set() or self._fired.is_set():
                return False
            if key in mod_names:
                self._pressed_mods.add(mod_names[key])
                return None
            base = _key_name(key)
            if not base:
                return None
            # Build "cmd+shift+space" style combo in a stable order.
            order = ["cmd", "ctrl", "option", "shift"]
            mods = [m for m in order if m in self._pressed_mods]
            combo = "+".join(mods + [base])
            self._emit(combo)
            return False

        def on_release(key):
            if key in mod_names:
                self._pressed_mods.discard(mod_names[key])

        try:
            self._kb_listener = kb.Listener(on_press=on_press,
                                            on_release=on_release)
            self._kb_listener.start()
        except Exception:
            self._kb_listener = None

    def stop(self):
        if self._stopped.is_set():
            return
        self._stopped.set()
        if self._mouse_tap is not None:
            try:
                self._mouse_tap.stop()
            except Exception:
                pass
            self._mouse_tap = None
        if self._kb_listener is not None:
            try:
                self._kb_listener.stop()
            except Exception:
                pass
            self._kb_listener = None


# ===========================================================================
# === Permissions: TCC (Accessibility + Input Monitoring + mic) =============
# ===========================================================================
# Settings deep links (the exact panes). Modern macOS still honors these
# x-apple.systempreferences URLs.
_PANE_ACCESSIBILITY = (
    "x-apple.systempreferences:com.apple.preference.security"
    "?Privacy_Accessibility")
_PANE_INPUT_MONITORING = (
    "x-apple.systempreferences:com.apple.preference.security"
    "?Privacy_ListenEvent")
_PANE_MIC = (
    "x-apple.systempreferences:com.apple.preference.security"
    "?Privacy_Microphone")


def open_accessibility_pane() -> None:
    subprocess.run(["open", _PANE_ACCESSIBILITY], check=False)


def open_input_monitoring_pane() -> None:
    subprocess.run(["open", _PANE_INPUT_MONITORING], check=False)


def open_microphone_pane() -> None:
    subprocess.run(["open", _PANE_MIC], check=False)


class MacPermissions(Permissions):
    """macOS TCC permission state + pane-opening.

    ``check()`` keys: "accessibility", "input_monitoring", "mic".
      * accessibility   -> AXIsProcessTrusted() (truth source for posting Cmd+V).
      * input_monitoring-> best-effort: macOS exposes IOHIDCheckAccess for the
        ListenEvent service; if unavailable we conservatively mirror the
        accessibility grant (they are usually granted together for this app and
        Input Monitoring has no clean public read API in older OSes).
      * mic             -> AVCaptureDevice authorization status (AVFoundation).
    """

    def check(self) -> dict[str, bool]:
        return {
            "accessibility": self._accessibility_ok(),
            "input_monitoring": self._input_monitoring_ok(),
            "mic": self._mic_ok(),
        }

    # -- accessibility (AXIsProcessTrusted) --
    def _accessibility_ok(self) -> bool:
        mod = _try_import("ApplicationServices") or _try_import("HIServices")
        if mod is None:
            mod = _try_import("Quartz")  # AXIsProcessTrusted is re-exported here
        if mod is None:
            return False
        try:
            fn = getattr(mod, "AXIsProcessTrusted", None)
            if fn is None:
                return False
            return bool(fn())
        except Exception:
            return False

    def _prompt_accessibility(self) -> None:
        """Trigger the system Accessibility prompt for this process via
        AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True})."""
        mod = (_try_import("ApplicationServices") or _try_import("HIServices")
               or _try_import("Quartz"))
        if mod is None:
            return
        try:
            fn = getattr(mod, "AXIsProcessTrustedWithOptions", None)
            key = getattr(mod, "kAXTrustedCheckOptionPrompt", None)
            if fn is not None and key is not None:
                fn({key: True})
        except Exception:
            pass

    # -- input monitoring (IOHIDCheckAccess) --
    def _input_monitoring_ok(self) -> bool:
        Q = _quartz()
        # IOHIDCheckAccess(kIOHIDRequestTypeListenEvent) -> 0 == granted.
        if Q is not None:
            fn = getattr(Q, "IOHIDCheckAccess", None)
            rtype = getattr(Q, "kIOHIDRequestTypeListenEvent", 1)
            granted = getattr(Q, "kIOHIDAccessTypeGranted", 0)
            if fn is not None:
                try:
                    return int(fn(rtype)) == int(granted)
                except Exception:
                    pass
        # No clean public read API available -> fall back to the accessibility
        # grant as a conservative proxy (documented).
        return self._accessibility_ok()

    def _request_input_monitoring(self) -> None:
        Q = _quartz()
        if Q is not None:
            fn = getattr(Q, "IOHIDRequestAccess", None)
            rtype = getattr(Q, "kIOHIDRequestTypeListenEvent", 1)
            if fn is not None:
                try:
                    fn(rtype)   # triggers the system prompt once
                    return
                except Exception:
                    pass
        open_input_monitoring_pane()

    # -- microphone (AVCaptureDevice) --
    def _mic_ok(self) -> bool:
        av = _try_import("AVFoundation")
        if av is None:
            return False
        try:
            AVCaptureDevice = av.AVCaptureDevice
            # AVMediaTypeAudio == "soun"; status 3 == authorized.
            media = getattr(av, "AVMediaTypeAudio", "soun")
            status = AVCaptureDevice.authorizationStatusForMediaType_(media)
            return int(status) == 3
        except Exception:
            return False

    def _request_mic(self) -> None:
        av = _try_import("AVFoundation")
        if av is not None:
            try:
                media = getattr(av, "AVMediaTypeAudio", "soun")
                av.AVCaptureDevice.requestAccessForMediaType_completionHandler_(
                    media, lambda granted: None)
                return
            except Exception:
                pass
        open_microphone_pane()

    # -- ABC: request a named permission --
    def request(self, name: str) -> None:
        n = (name or "").lower()
        if n in ("accessibility", "accessibility_post", "post"):
            # Try the in-process prompt first, then open the pane as a guide.
            self._prompt_accessibility()
            open_accessibility_pane()
        elif n in ("input_monitoring", "listen", "listenevent"):
            self._request_input_monitoring()
            open_input_monitoring_pane()
        elif n in ("mic", "microphone"):
            self._request_mic()
        else:
            log.warning("Unknown permission '%s' requested.", name)

    def all_ok(self) -> bool:
        c = self.check()
        # Mic is genuinely required to record; accessibility to paste; input
        # monitoring to hear the hotkey. All three matter for full function.
        return bool(c.get("accessibility") and c.get("input_monitoring")
                    and c.get("mic"))


# ===========================================================================
# === ABC implementations exposed to the platform factory ===================
# ===========================================================================
class MacHotkeys(HotkeyBackend):
    """HotkeyBackend for plain keyboard combos (pynput GlobalHotKeys)."""

    def __init__(self):
        self._registered: list = []
        self._handles: list = []

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
        for h in self._handles:
            try:
                h.stop()
            except Exception:
                pass
        self._handles = []

    @property
    def supports_hold_mode(self) -> bool:
        # pynput can distinguish press/release on macOS, so push-to-talk is
        # feasible; the current toggle-based engine doesn't use it, matching the
        # Windows backend's reported capability for now.
        return False


class MacMouse(MouseBackend):
    """MouseBackend for side-button + chord triggers (CGEventTap suppression)."""

    supports_side_buttons = True

    def __init__(self):
        self._registered: list = []
        self._handles: list = []

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
        for h in self._handles:
            try:
                h.stop()
            except Exception:
                pass
        self._handles = []


class MacTriggers(TriggerBackend):
    """The single, stable trigger API the engine uses on macOS: register ANY
    VoiceFlow trigger string (keyboard combo, side button, or the left+right
    chord) and get a stoppable handle. Delegates to ``register_trigger``."""

    def register(self, trigger, callback):
        return register_trigger(trigger, callback)

    def classify(self, trigger):
        return classify_trigger(trigger)

    @property
    def presets(self):
        return list(PRESETS)


# ---------------------------------------------------------------------------
# diagnostics(): UI-ready macOS status (parity with the Linux backend's richer
# diagnostics). Consumed by voiceflow.platform.diagnostics() and the first-run /
# settings UI. Surfaces the three TCC grants plus a 'can_paste' summary and
# human-readable hints pointing at the exact Settings panes.
# ---------------------------------------------------------------------------
def diagnostics() -> dict:
    """Return {"accessibility","input_monitoring","mic","can_paste","pyobjc",
    "hints":[...]} describing macOS readiness. Never raises."""
    perms = MacPermissions()
    try:
        state = perms.check()
    except Exception:
        state = {"accessibility": False, "input_monitoring": False, "mic": False}
    have_pyobjc = _appkit() is not None and _quartz() is not None
    hints: list[str] = []
    if not have_pyobjc:
        hints.append("pyobjc frameworks are missing; install the GUI build's "
                     "dependencies (Cocoa, Quartz, AVFoundation).")
    if not state.get("accessibility"):
        hints.append("Grant Accessibility so OpenVerba can paste "
                     "(System Settings -> Privacy & Security -> Accessibility).")
    if not state.get("input_monitoring"):
        hints.append("Grant Input Monitoring so OpenVerba hears your trigger "
                     "(System Settings -> Privacy & Security -> Input "
                     "Monitoring).")
    if not state.get("mic"):
        hints.append("Grant Microphone so OpenVerba can record "
                     "(System Settings -> Privacy & Security -> Microphone).")
    return {
        "session": "macos",
        "accessibility": bool(state.get("accessibility")),
        "input_monitoring": bool(state.get("input_monitoring")),
        "mic": bool(state.get("mic")),
        # 'can_paste' mirrors the Linux summary key: posting Cmd+V needs the
        # Accessibility grant (and pyobjc/Quartz present to do it).
        "can_paste": bool(state.get("accessibility") and have_pyobjc),
        "pyobjc": have_pyobjc,
        "hints": hints,
    }


# ---------------------------------------------------------------------------
# Module-level factory hooks (consumed by voiceflow.platform.__init__ and
# voiceflow.triggers). Names/signatures match windows.py exactly.
# ---------------------------------------------------------------------------
def make_clipboard(restore_delay_ms: int = 200, read_timeout_ms: int = 2500):
    """Return the clipboard object the engine uses (the richer PasteboardManager
    with the full paste cycle), parity with the Windows factory."""
    return PasteboardManager(restore_delay_ms, read_timeout_ms)


def Hotkeys():
    return MacHotkeys()


def Mouse():
    return MacMouse()


def Clipboard():
    return MacClipboard()


def PasterImpl():
    return MacPaster()


def PermissionsImpl():
    return MacPermissions()


def Triggers():
    return MacTriggers()
