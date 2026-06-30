"""
voiceflow.ui.first_run - the macOS permission-guidance screen.

macOS gates VoiceFlow behind three TCC permissions (see docs/PRODUCTION_PLAN.md
sections 2.4 / 3.2):

  * Accessibility    - to POST the Cmd+V paste keystroke (AXIsProcessTrusted).
  * Input Monitoring - to LISTEN for the global hotkey.
  * Microphone       - to record audio.

A frozen .app often does not auto-prompt for these and may silently no-op, so we
ship this first-run screen: it detects which permissions are missing, gives a
button that opens the exact Settings pane (and, where possible, triggers the
system prompt), and lets the user re-check after granting -- the grant only
"takes" once the app is trusted, and re-checking confirms it without a restart in
the common case.

Cross-platform behaviour: this screen is macOS-focused but no-op-friendly
elsewhere. ``permissions_needed()`` returns False on Windows/Linux (the Windows
Permissions backend reports everything OK), so callers can simply skip showing
the dialog. If shown anyway on a non-macOS box it renders a short "no extra
permissions required" panel and an OK button, never erroring.

The Permissions backend comes from the active platform via
``voiceflow.platform.make_backends()`` (the 5th element), so this UI talks only
to the ABC -- it never imports pyobjc directly.
"""

from __future__ import annotations

import sys

import customtkinter as ctk

from . import theme as T
from . import widgets as W


# ---------------------------------------------------------------------------
# Permission metadata: order, label, why, which request() name opens its pane.
# ---------------------------------------------------------------------------
_PERMS = [
    {
        "key": "accessibility",
        "title": "Accessibility",
        "why": "Lets OpenVerba paste the transcribed text into the app you're "
               "typing in (it synthesizes Command-V).",
        "request": "accessibility",
        "pane": "System Settings -> Privacy & Security -> Accessibility",
    },
    {
        "key": "input_monitoring",
        "title": "Input Monitoring",
        "why": "Lets OpenVerba hear your global trigger (hotkey or mouse "
               "button) while other apps are focused.",
        "request": "input_monitoring",
        "pane": "System Settings -> Privacy & Security -> Input Monitoring",
    },
    {
        "key": "mic",
        "title": "Microphone",
        "why": "Lets OpenVerba record your voice so it can be transcribed "
               "locally on your machine.",
        "request": "mic",
        "pane": "System Settings -> Privacy & Security -> Microphone",
    },
]


def _make_permissions():
    """Return the active platform's Permissions backend (5th factory element).
    Never raises: on any failure returns a stub that reports all-OK so the
    first-run screen degrades to a no-op."""
    try:
        from voiceflow import platform as _platform
        _, _, _, _, perms = _platform.make_backends()
        return perms
    except Exception:
        class _AllOk:
            def check(self):
                return {"accessibility": True, "input_monitoring": True,
                        "mic": True}

            def request(self, name):
                return None

            def all_ok(self):
                return True
        return _AllOk()


def permissions_needed() -> bool:
    """True only when the current OS actually has an outstanding permission the
    user must grant. False on Windows/Linux and on a fully-granted Mac, so a
    caller can gate the dialog with a single cheap check."""
    if sys.platform != "darwin":
        return False
    try:
        return not _make_permissions().all_ok()
    except Exception:
        return False


class FirstRunPermissions(ctk.CTkToplevel):
    """The permission-guidance dialog.

    Usage::

        if first_run.permissions_needed():
            first_run.FirstRunPermissions(root).show()

    ``show()`` blocks until the dialog closes and returns True if every
    permission is granted at close time, else False. ``on_complete(all_ok)`` is
    an optional callback invoked (on the UI thread) when the user finishes.
    """

    def __init__(self, master, on_complete=None):
        super().__init__(master)
        self.title("OpenVerba - Permissions")
        self.configure(fg_color=T.BG)
        self.geometry("560x600")
        self.minsize(520, 540)
        self.resizable(False, True)

        self._on_complete = on_complete
        self._perms = _make_permissions()
        self._rows: dict[str, dict] = {}
        self._result = False

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        self._build()
        self.transient(master)
        self.after(10, self._center_on_parent)
        self.protocol("WM_DELETE_WINDOW", self._finish)
        self._refresh()

    # -- layout ------------------------------------------------------------
    def _build(self):
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=22, pady=(22, 6))
        header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(header, text="One-time macOS setup", font=T.font("h1"),
                     text_color=T.TEXT, anchor="w").grid(
            row=0, column=0, sticky="w")

        if sys.platform != "darwin":
            W.hint_label(
                header,
                "No extra permissions are required on this operating system. "
                "You're all set.", color=T.TEXT_MUTED).grid(
                row=1, column=0, sticky="ew", pady=(6, 0))
            footer = ctk.CTkFrame(self, fg_color="transparent")
            footer.grid(row=4, column=0, sticky="ew", padx=22, pady=(12, 18))
            footer.grid_columnconfigure(0, weight=1)
            W.accent_button(footer, "OK", command=self._finish,
                            width=140).grid(row=0, column=1)
            return

        W.hint_label(
            header,
            "macOS needs you to grant OpenVerba three permissions. Click each "
            "\"Open Settings\" button, flip OpenVerba on, then come back and "
            "press \"Re-check\". Grants bind to this signed app.",
            color=T.TEXT_MUTED).grid(row=1, column=0, sticky="ew", pady=(4, 0))

        body = ctk.CTkScrollableFrame(self, fg_color="transparent")
        body.grid(row=2, column=0, sticky="nsew", padx=18, pady=(10, 4))
        body.grid_columnconfigure(0, weight=1)
        for i, meta in enumerate(_PERMS):
            self._perm_card(body, meta).grid(
                row=i, column=0, sticky="ew", padx=4, pady=6)

        # Footer: Re-check + Continue.
        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.grid(row=4, column=0, sticky="ew", padx=22, pady=(8, 18))
        footer.grid_columnconfigure(0, weight=1)
        self._status = ctk.CTkLabel(footer, text="", font=T.font("small"),
                                    text_color=T.TEXT_MUTED, anchor="w")
        self._status.grid(row=0, column=0, sticky="w")
        W.secondary_button(footer, "Re-check", command=self._refresh,
                           width=120).grid(row=0, column=1, padx=(8, 0))
        self._continue_btn = W.accent_button(
            footer, "Continue", command=self._finish, width=140)
        self._continue_btn.grid(row=0, column=2, padx=(8, 0))

    def _perm_card(self, master, meta):
        card = ctk.CTkFrame(master, fg_color=T.SURFACE_2,
                            corner_radius=T.RADIUS_SM, border_width=1,
                            border_color=T.BORDER)
        card.grid_columnconfigure(0, weight=1)

        top = ctk.CTkFrame(card, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 2))
        top.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(top, text=meta["title"], font=T.font("body_bold"),
                     text_color=T.TEXT, anchor="w").grid(
            row=0, column=0, sticky="w")
        badge = W.Badge(top, "Checking...", color=T.SURFACE_3,
                        text_color=T.TEXT_MUTED)
        badge.grid(row=0, column=1, sticky="e")

        W.hint_label(card, meta["why"], color=T.TEXT_MUTED,
                     wraplength=460).grid(row=1, column=0, sticky="w",
                                          padx=14, pady=(2, 2))
        W.hint_label(card, meta["pane"], color=T.TEXT_FAINT,
                     wraplength=460).grid(row=2, column=0, sticky="w",
                                          padx=14, pady=(0, 6))

        btn = W.secondary_button(
            card, "Open Settings",
            command=lambda m=meta: self._open(m), width=150)
        btn.grid(row=3, column=0, sticky="w", padx=14, pady=(0, 12))

        self._rows[meta["key"]] = {"badge": badge, "button": btn}
        return card

    # -- behaviour ---------------------------------------------------------
    def _open(self, meta):
        try:
            self._perms.request(meta["request"])
        except Exception:
            pass
        # Re-check shortly after; the user may grant while the pane is open.
        self.after(1200, self._refresh)

    def _refresh(self):
        try:
            state = self._perms.check()
        except Exception:
            state = {}
        all_ok = True
        for meta in _PERMS:
            ok = bool(state.get(meta["key"], False))
            all_ok = all_ok and ok
            row = self._rows.get(meta["key"])
            if not row:
                continue
            if ok:
                row["badge"].configure(text="Granted", fg_color=T.ACCENT_SOFT,
                                       text_color=T.OK)
                row["button"].configure(text="Granted", state="disabled")
            else:
                row["badge"].configure(text="Needed", fg_color="#3a1f23",
                                       text_color=T.DANGER)
                row["button"].configure(text="Open Settings", state="normal")
        if hasattr(self, "_status"):
            if all_ok:
                self._status.configure(
                    text="All permissions granted.", text_color=T.OK)
            else:
                self._status.configure(
                    text="Grant the items marked \"Needed\", then Re-check.",
                    text_color=T.TEXT_MUTED)
        self._result = all_ok

    def _finish(self):
        try:
            self._result = bool(self._perms.all_ok())
        except Exception:
            pass
        if self._on_complete is not None:
            try:
                self._on_complete(self._result)
            except Exception:
                pass
        self._close()

    def _close(self):
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()

    def _center_on_parent(self):
        try:
            self.update_idletasks()
            m = self.master
            px, py = m.winfo_rootx(), m.winfo_rooty()
            pw, ph = m.winfo_width(), m.winfo_height()
            w, h = self.winfo_width(), self.winfo_height()
            x = px + (pw - w) // 2
            y = py + (ph - h) // 2
            self.geometry("+%d+%d" % (max(0, x), max(0, y)))
        except Exception:
            pass
        try:
            self.grab_set()
        except Exception:
            pass

    def show(self) -> bool:
        """Block until the dialog closes; return True if all permissions are
        granted at close time."""
        self.wait_window()
        return self._result
