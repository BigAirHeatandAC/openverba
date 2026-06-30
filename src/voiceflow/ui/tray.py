"""
gui.tray - a system-tray icon for the GUI process (Open / Pause / Quit) plus a
state-colored dot (idle/recording/transcribing).

pystray runs its own event loop on a daemon thread. Menu callbacks fire on that
thread, so they hand work back to the GUI via the App's thread-safe schedule()
(customtkinter .after). If pystray/pillow aren't available, GuiTray is a no-op.
"""

from __future__ import annotations

import threading

from voiceflow.constants import (
    IDLE, RECORDING, TRANSCRIBING, STATE_LABELS, APP_DISPLAY_NAME,
)


_STATE_COLORS = {
    STATE_LABELS[IDLE]:         (120, 120, 120),
    STATE_LABELS[RECORDING]:    (239, 68, 68),
    STATE_LABELS[TRANSCRIBING]: (245, 158, 11),
}


class GuiTray:
    """Tray controller for the GUI. on_open/on_toggle_pause/on_quit are callables
    the App provides; they are invoked from the tray thread and should marshal to
    the UI thread themselves (the App does). available is False if pystray/pillow
    are missing."""

    def __init__(self, app, on_open, on_toggle_pause, on_quit):
        self.app = app
        self._on_open = on_open
        self._on_toggle_pause = on_toggle_pause
        self._on_quit = on_quit
        self.icon = None
        self._paused = False
        self._icons = {}
        self.available = False
        self._build()

    def _build(self):
        try:
            import pystray
            from PIL import Image, ImageDraw
        except Exception:
            return
        try:
            from .widgets import make_icon_image
            base = make_icon_image(64).convert("RGBA")
        except Exception:
            base = None

        def _icon(color):
            from PIL import Image, ImageDraw
            if base is not None:
                img = base.copy()
                d = ImageDraw.Draw(img)
                # status dot in the corner
                r = 14
                d.ellipse([64 - r - 2, 64 - r - 2, 64 - 2, 64 - 2],
                          fill=color + (255,))
                return img
            img = Image.new("RGB", (64, 64), (24, 29, 38))
            d = ImageDraw.Draw(img)
            d.ellipse([16, 16, 48, 48], fill=color)
            return img

        self._icons = {k: _icon(v) for k, v in _STATE_COLORS.items()}

        def _open(icon, item):
            self._on_open()

        def _toggle(icon, item):
            self._paused = not self._paused
            self._on_toggle_pause(self._paused)
            try:
                icon.update_menu()
            except Exception:
                pass

        def _quit(icon, item):
            try:
                icon.stop()
            except Exception:
                pass
            self._on_quit()

        menu = pystray.Menu(
            pystray.MenuItem("Open " + APP_DISPLAY_NAME, _open, default=True),
            pystray.MenuItem(
                lambda item: "Resume dictation" if self._paused
                else "Pause dictation", _toggle),
            pystray.MenuItem("Quit", _quit),
        )
        self.icon = pystray.Icon(
            "voiceflow", self._icons[STATE_LABELS[IDLE]], APP_DISPLAY_NAME, menu)
        self.available = True

    def run(self):
        if not self.available:
            return
        threading.Thread(target=self.icon.run, daemon=True,
                         name="gui-tray").start()

    def set_state(self, state_label):
        if not self.available or self.icon is None:
            return
        try:
            self.icon.icon = self._icons.get(
                state_label, self._icons[STATE_LABELS[IDLE]])
            self.icon.title = APP_DISPLAY_NAME + ": " + state_label
        except Exception:
            pass

    def set_paused(self, paused):
        self._paused = bool(paused)
        try:
            if self.icon is not None:
                self.icon.update_menu()
        except Exception:
            pass

    def stop(self):
        try:
            if self.icon is not None:
                self.icon.stop()
        except Exception:
            pass
