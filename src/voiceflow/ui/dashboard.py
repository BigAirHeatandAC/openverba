"""
gui.dashboard - the main window content once setup is done.

Shows: a big status indicator (Idle / Recording… / Transcribing…), the active
model + device, the active trigger prominently with a "Change trigger" button,
the last transcript preview, a Start/Pause toggle, a mic level meter, and a link
to settings.

The dashboard is purely view + callbacks into the App (which owns the engine).
The App pushes engine state to it via set_state / set_level / set_transcript /
set_model_info — all already on the UI thread (the App marshals them).
"""

from __future__ import annotations

import customtkinter as ctk

from . import theme as T
from . import widgets as W
from voiceflow.triggers import classify_trigger


class DashboardView(ctk.CTkFrame):
    def __init__(self, master, app, **kw):
        kw.setdefault("fg_color", T.BG)
        super().__init__(master, **kw)
        self.app = app

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)
        self._build()

    # ------------------------------------------------------------- layout
    def _build(self):
        # Top bar: title + settings link.
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=24, pady=(20, 6))
        top.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(top, image=W.make_ctk_icon(34), text="").grid(
            row=0, column=0, padx=(0, 10))
        ctk.CTkLabel(top, text="OpenVerba", font=T.font("h1"),
                     text_color=T.TEXT, anchor="w").grid(row=0, column=1,
                                                         sticky="w")
        W.ghost_button(top, "📄  Transcribe file",
                       command=self.app.show_file_transcriber,
                       width=140).grid(row=0, column=2, padx=(0, 6))
        W.ghost_button(top, "🕘  History", command=self.app.show_history,
                       width=110).grid(row=0, column=3, padx=(0, 6))
        W.ghost_button(top, "⚙  Settings", command=self.app.show_settings,
                       width=110).grid(row=0, column=4)

        # Status hero card.
        hero = W.Card(self)
        hero.grid(row=1, column=0, sticky="ew", padx=24, pady=10)
        hero.grid_columnconfigure(0, weight=1)
        inner = ctk.CTkFrame(hero, fg_color="transparent")
        inner.grid(row=0, column=0, sticky="ew", padx=24, pady=24)
        inner.grid_columnconfigure(1, weight=1)

        # Big status dot.
        self._dot = ctk.CTkLabel(inner, text="●", font=ctk.CTkFont(size=54),
                                 text_color=T.STATE_COLORS["idle"][0])
        self._dot.grid(row=0, column=0, rowspan=2, padx=(0, 20))

        self._status_lbl = ctk.CTkLabel(inner, text="Idle", font=T.font("title"),
                                        text_color=T.TEXT, anchor="w")
        self._status_lbl.grid(row=0, column=1, sticky="sw")
        self._status_sub = ctk.CTkLabel(
            inner, text="Press your trigger to start dictating.",
            font=T.font("body"), text_color=T.TEXT_MUTED, anchor="w")
        self._status_sub.grid(row=1, column=1, sticky="nw")

        # Start/Pause toggle.
        self._toggle_btn = W.accent_button(inner, "Pause", width=130,
                                           command=self._toggle_dictation)
        self._toggle_btn.grid(row=0, column=2, rowspan=2, padx=(16, 0))

        # Mic level meter (under hero).
        meter_row = ctk.CTkFrame(hero, fg_color="transparent")
        meter_row.grid(row=1, column=0, sticky="ew", padx=24, pady=(0, 18))
        meter_row.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(meter_row, text="🎙", font=T.font("body"),
                     text_color=T.TEXT_MUTED).grid(row=0, column=0, padx=(0, 8))
        self._meter = W.LevelMeter(meter_row, segments=28)
        self._meter.grid(row=0, column=1, sticky="w")

        # Info row: model/device + trigger.
        info = ctk.CTkFrame(self, fg_color="transparent")
        info.grid(row=2, column=0, sticky="nsew", padx=24, pady=(2, 8))
        info.grid_columnconfigure(0, weight=1)
        info.grid_columnconfigure(1, weight=1)
        info.grid_rowconfigure(1, weight=1)

        # Model/device card.
        mcard = W.Card(info, title="Active model")
        mcard.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        mcard.grid_columnconfigure(0, weight=1)
        self._model_lbl = ctk.CTkLabel(mcard, text="—", font=T.font("h3"),
                                       text_color=T.TEXT, anchor="w")
        self._model_lbl.grid(row=1, column=0, sticky="w", padx=18)
        self._device_badge = W.Badge(mcard, "Loading…", color=T.SURFACE_3,
                                     text_color=T.TEXT)
        self._device_badge.grid(row=2, column=0, sticky="w", padx=18,
                                pady=(8, 18))

        # Trigger card.
        tcard = W.Card(info, title="Trigger")
        tcard.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        tcard.grid_columnconfigure(0, weight=1)
        self._trigger_lbl = ctk.CTkLabel(tcard, text="—", font=T.font("h3"),
                                         text_color=T.ACCENT, anchor="w")
        self._trigger_lbl.grid(row=1, column=0, sticky="w", padx=18)
        self._trigger_warn = W.hint_label(tcard, "", color=T.WARN,
                                          wraplength=240)
        self._trigger_warn.grid(row=2, column=0, sticky="w", padx=18,
                                pady=(2, 4))
        W.secondary_button(tcard, "Change trigger",
                           command=self.app.open_trigger_picker,
                           height=34).grid(row=3, column=0, sticky="w",
                                           padx=18, pady=(4, 16))

        # Last transcript card.
        lcard = W.Card(info, title="Last transcript")
        lcard.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(16, 0))
        lcard.grid_columnconfigure(0, weight=1)
        lcard.grid_rowconfigure(1, weight=1)
        self._transcript = ctk.CTkTextbox(
            lcard, font=T.font("body"), fg_color=T.SURFACE_2,
            text_color=T.TEXT, border_width=1, border_color=T.BORDER,
            corner_radius=T.RADIUS_SM, wrap="word", height=90)
        self._transcript.grid(row=1, column=0, sticky="nsew", padx=18,
                              pady=(0, 16))
        self._transcript.insert("end", "Your dictated text will appear here.")
        self._transcript.configure(state="disabled")

        # Log / status line at the bottom.
        self._log_lbl = ctk.CTkLabel(self, text="", font=T.font("small"),
                                     text_color=T.TEXT_FAINT, anchor="w")
        self._log_lbl.grid(row=3, column=0, sticky="ew", padx=26, pady=(0, 10))

    # ------------------------------------------------------- view updates
    def set_state(self, state_label):
        color, text = T.STATE_COLORS.get(state_label,
                                         T.STATE_COLORS["idle"])
        self._dot.configure(text_color=color)
        self._status_lbl.configure(text=text)
        subs = {
            "idle": "Press your trigger to start dictating.",
            "recording": "Listening… trigger again to type what you said.",
            "transcribing": "Transcribing your speech…",
        }
        self._status_sub.configure(text=subs.get(state_label, ""))
        if state_label != "recording":
            self._meter.set_level(0.0)

    def set_level(self, level):
        self._meter.set_level(level)

    def set_transcript(self, text):
        try:
            self._transcript.configure(state="normal")
            self._transcript.delete("1.0", "end")
            self._transcript.insert("end", text or "(empty)")
            self._transcript.configure(state="disabled")
        except Exception:
            pass

    def set_log(self, message):
        self._log_lbl.configure(text=message or "")

    def set_model_info(self, info):
        if not info:
            return
        loaded = info.get("loaded_name") or info.get("model") or "—"
        self._model_lbl.configure(text=loaded)
        device = info.get("device")
        if device == "gpu":
            self._device_badge.configure(
                text="GPU · %s" % (info.get("compute_type") or ""),
                fg_color=T.ACCENT_SOFT, text_color=T.ACCENT)
        elif device == "cpu":
            self._device_badge.configure(
                text="CPU · %s" % (info.get("compute_type") or ""),
                fg_color=T.SURFACE_3, text_color=T.TEXT_MUTED)
        else:
            self._device_badge.configure(text="Loading…",
                                         fg_color=T.SURFACE_3,
                                         text_color=T.TEXT_MUTED)

    def set_trigger(self, trigger):
        info = classify_trigger(trigger) if trigger else {}
        self._trigger_lbl.configure(text=info.get("label") or trigger or "—")
        if info.get("clean", True):
            self._trigger_warn.configure(text="")
        else:
            self._trigger_warn.configure(
                text="⚠ " + (info.get("warning") or "May conflict with normal "
                             "mouse/keyboard use."))

    def set_armed(self, armed):
        if armed:
            self._toggle_btn.configure(text="Pause", fg_color=T.ACCENT,
                                       hover_color=T.ACCENT_DARK,
                                       text_color="#06201d")
        else:
            self._toggle_btn.configure(text="Resume", fg_color=T.WARN,
                                       hover_color="#d98c08",
                                       text_color="#231a00")

    # ------------------------------------------------------------- actions
    def _toggle_dictation(self):
        self.app.toggle_dictation()
