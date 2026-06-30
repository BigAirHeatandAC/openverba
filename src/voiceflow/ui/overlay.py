"""
gui.overlay - the Live-preview floating bar.

A borderless, always-on-top, NON-ACTIVATING bar shown while the user dictates in
"preview" mode. It displays rough live words as they form, but NEVER touches the
target document and -- critically -- NEVER steals keyboard focus from the app the
user is dictating into. If the overlay activated, the user's target app would
lose focus and the final paste would land in the wrong window.

How focus theft is prevented (Windows):
  * overrideredirect(True)         -> borderless, no titlebar
  * attributes("-topmost", True)   -> stays above other windows
  * WS_EX_NOACTIVATE  (0x08000000) -> the window can NEVER be activated / take
                                      focus, even when clicked or raised
  * WS_EX_TOOLWINDOW  (0x00000080) -> no taskbar button, no Alt-Tab entry
  These extended styles are (re)applied via SetWindowLongPtrW(GWL_EXSTYLE) every
  show(), because overrideredirect/deiconify can reset extended styles.
  lift()/tkraise() on a WS_EX_NOACTIVATE window raises WITHOUT activating, so
  show()/set_text() never move focus off the target app. This module NEVER calls
  focus_force()/focus_set().

Fail-open: every public method (show/set_text/hide) wraps its body in try/except
so an overlay failure can never crash the UI thread or break dictation. The
engine's preview path additionally falls back to plain batch on any error.

Created on the UI (Tk) thread only -- the engine reaches it via the app's
.after/schedule marshalling.
"""

from __future__ import annotations

import os
import logging

import customtkinter as ctk

from . import theme as T

log = logging.getLogger("voiceflow.overlay")

# --- Win32 extended-window-style constants (see module docstring) ---
GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000   # window can't be activated / take focus
WS_EX_TOOLWINDOW = 0x00000080   # no taskbar button, no alt-tab entry
WS_EX_TOPMOST = 0x00000008

# Default max chars shown in the bar (config "preview_max_chars" overrides).
_DEFAULT_MAX_CHARS = 120
_PLACEHOLDER = "Listening…"


def _parse_pos(s):
    """Parse a saved "x,y" position string into (int, int), or None if absent /
    malformed. Pure + defensive so it's unit-testable without a window."""
    try:
        if isinstance(s, str) and "," in s:
            a, b = s.split(",", 1)
            return int(a.strip()), int(b.strip())
    except Exception:
        pass
    return None


class PreviewOverlay:
    """A reusable non-activating floating preview bar owned by the main window.

    Construct once (lazily) with the CTk root as parent; reuse for the app
    lifetime via show()/set_text()/hide(). Call destroy() on app quit.
    """

    def __init__(self, parent, max_chars: int | None = None):
        self._parent = parent
        self._max_chars = int(max_chars or _DEFAULT_MAX_CHARS)
        self._win = None
        self._text_lbl = None
        self._build()

    # ------------------------------------------------------------------ build
    def _build(self):
        try:
            win = ctk.CTkToplevel(self._parent)
            win.withdraw()                      # never flash on creation
            win.overrideredirect(True)          # borderless, no titlebar
            win.configure(fg_color=T.BG)
            win.attributes("-topmost", True)
            try:
                win.attributes("-alpha", 0.96)  # subtle, optional
            except Exception:
                pass

            # Rounded card matching the dashboard surface look.
            card = ctk.CTkFrame(win, fg_color=T.SURFACE, corner_radius=T.RADIUS,
                                border_width=1, border_color=T.BORDER)
            card.grid(row=0, column=0, sticky="nsew", padx=2, pady=2)
            card.grid_columnconfigure(2, weight=1)
            win.grid_columnconfigure(0, weight=1)
            win.grid_rowconfigure(0, weight=1)

            # Recording dot (the "recording" red) + a small "Live" label.
            ctk.CTkLabel(card, text="●", text_color=T.DANGER,
                         font=T.font("body_bold")).grid(
                row=0, column=0, padx=(16, 4), pady=12)
            ctk.CTkLabel(card, text="Live", text_color=T.TEXT_MUTED,
                         font=T.font("small_bold")).grid(
                row=0, column=1, padx=(0, 10), pady=12)

            self._text_lbl = ctk.CTkLabel(
                card, text=_PLACEHOLDER, font=T.font("body"),
                text_color=T.TEXT, wraplength=520, justify="left", anchor="w")
            self._text_lbl.grid(row=0, column=2, padx=(0, 16), pady=12,
                                sticky="w")
            self._win = win
            self._drag_off = None
            # Make the whole bar draggable. Because it's WS_EX_NOACTIVATE, the
            # user can drag it WITHOUT pulling focus off their target app.
            for wdg in (win, card) + tuple(card.winfo_children()):
                try:
                    wdg.bind("<Button-1>", self._on_drag_start)
                    wdg.bind("<B1-Motion>", self._on_drag_move)
                    wdg.bind("<ButtonRelease-1>", self._on_drag_end)
                    wdg.configure(cursor="fleur")   # "move" cursor affordance
                except Exception:
                    pass
        except Exception:
            log.debug("overlay build failed", exc_info=True)
            self._win = None
            self._text_lbl = None

    # ------------------------------------------------------- noactivate style
    def _apply_noactivate(self):
        """Apply WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TOPMOST so the bar
        can never take focus. Re-applied on every show() (overrideredirect /
        deiconify can reset extended styles). No-op off Windows / on error."""
        if os.name != "nt" or self._win is None:
            return
        try:
            import ctypes
            hwnd = self._win.winfo_id()           # the activatable child HWND
            u32 = ctypes.windll.user32
            # 64-bit-safe prototypes: use the *Ptr variants with c_void_p so the
            # extended style is not truncated on 64-bit handles.
            u32.GetWindowLongPtrW.restype = ctypes.c_void_p
            u32.GetWindowLongPtrW.argtypes = (ctypes.c_void_p, ctypes.c_int)
            u32.SetWindowLongPtrW.restype = ctypes.c_void_p
            u32.SetWindowLongPtrW.argtypes = (ctypes.c_void_p, ctypes.c_int,
                                              ctypes.c_void_p)
            ex = u32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE) or 0
            ex |= WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TOPMOST
            u32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ex)
        except Exception:
            # fail-open: a styling failure must not break dictation
            log.debug("_apply_noactivate failed", exc_info=True)

    # --------------------------------------------------------- public API
    def show(self):
        """Make the bar visible, reposition bottom-center, reset to placeholder.
        Idempotent and no-throw (the UI half of fail-open)."""
        if self._win is None:
            return
        try:
            self.set_text(_PLACEHOLDER)
            self._win.deiconify()
            # Re-apply the non-activating styles AFTER deiconify (deiconify /
            # overrideredirect can reset the extended style).
            self._apply_noactivate()
            # lift() on a WS_EX_NOACTIVATE window raises WITHOUT activating it.
            self._win.lift()
            self._win.attributes("-topmost", True)
            self._reposition()
        except Exception:
            log.debug("overlay show failed", exc_info=True)

    def set_text(self, t: str):
        """Update the live label. Keeps the LAST ~max_chars so the most recent
        words stay visible. No-throw."""
        if self._win is None or self._text_lbl is None:
            return
        try:
            s = (t or "").strip() or _PLACEHOLDER
            n = self._max_chars
            if n > 0 and len(s) > n:
                s = "…" + s[-(n - 1):]
            self._text_lbl.configure(text=s)
        except Exception:
            log.debug("overlay set_text failed", exc_info=True)

    def hide(self):
        """Withdraw the bar (kept alive for reuse; not destroyed). No-throw."""
        if self._win is None:
            return
        try:
            self._win.withdraw()
        except Exception:
            log.debug("overlay hide failed", exc_info=True)

    def destroy(self):
        """Real teardown on app quit."""
        if self._win is None:
            return
        try:
            self._win.destroy()
        except Exception:
            log.debug("overlay destroy failed", exc_info=True)
        finally:
            self._win = None
            self._text_lbl = None

    # ----------------------------------------------------- positioning + drag
    def _saved_pos(self):
        cfg = getattr(self._parent, "cfg", None)
        return _parse_pos(cfg.get("preview_pos")) if isinstance(cfg, dict) else None

    def _save_pos(self):
        """Persist the current bar position to config so it reopens where the
        user left it. No-throw."""
        try:
            cfg = getattr(self._parent, "cfg", None)
            if not isinstance(cfg, dict):
                return
            cfg["preview_pos"] = "%d,%d" % (self._win.winfo_x(),
                                            self._win.winfo_y())
            from voiceflow import config as vf_config
            vf_config.save_config(cfg)
        except Exception:
            log.debug("overlay save_pos failed", exc_info=True)

    def _on_drag_start(self, event):
        try:
            self._drag_off = (event.x_root - self._win.winfo_x(),
                              event.y_root - self._win.winfo_y())
        except Exception:
            self._drag_off = None

    def _on_drag_move(self, event):
        if not self._drag_off:
            return
        try:
            x = event.x_root - self._drag_off[0]
            y = event.y_root - self._drag_off[1]
            self._win.geometry("+%d+%d" % (x, y))
        except Exception:
            pass

    def _on_drag_end(self, event):
        self._drag_off = None
        self._save_pos()

    def _reposition(self):
        """Place the bar where the user last dragged it (clamped on-screen), or
        centered near the bottom of the primary screen the first time."""
        try:
            self._win.update_idletasks()
            sw = self._win.winfo_screenwidth()
            sh = self._win.winfo_screenheight()
            w = self._win.winfo_width() or 560
            h = self._win.winfo_height() or 64
            saved = self._saved_pos()
            if saved is not None:
                x, y = saved
                # Clamp so a stale / off-screen saved position can't hide the bar
                # (e.g. after unplugging a monitor).
                x = max(0, min(x, sw - 80))
                y = max(0, min(y, sh - 40))
            else:
                x = (sw - w) // 2
                y = sh - h - 80          # ~80px above the taskbar
            self._win.geometry("+%d+%d" % (max(0, x), max(0, y)))
        except Exception:
            log.debug("overlay reposition failed", exc_info=True)
