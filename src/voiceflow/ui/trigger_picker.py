"""
voiceflow.ui.trigger_picker - the "Change trigger" dialog.

Live-detects the user's next key combo or mouse button via
voiceflow.triggers.TriggerRecorder, shows exactly what was detected in real time
(so an undetectable button visibly registers nothing), warns when a choice is
conflict-prone, offers clean presets, and returns the chosen trigger string.

The TriggerRecorder callbacks fire on BACKGROUND threads, so every UI update is
marshalled with .after(0, ...).
"""

from __future__ import annotations

import customtkinter as ctk

from . import theme as T
from . import widgets as W

from voiceflow.triggers import TriggerRecorder, classify_trigger, PRESETS


class TriggerPicker(ctk.CTkToplevel):
    """Modal-ish dialog. Call .show() (blocks until closed) -> returns the chosen
    trigger string or None if cancelled.

    on_apply(trigger) is optional: if provided it is called (on the UI thread)
    with the chosen trigger when the user clicks Save, BEFORE the dialog closes,
    and its truthy/falsey return decides whether to keep the dialog open on
    failure (so the live engine.set_trigger result can be surfaced).
    """

    def __init__(self, master, current: str | None = None, on_apply=None):
        super().__init__(master)
        self.title("Choose a trigger")
        self.configure(fg_color=T.BG)
        self.geometry("540x620")
        self.minsize(500, 560)
        self.resizable(False, True)

        self._on_apply = on_apply
        self._current = current
        self._chosen = current
        self._result = None
        self._recorder = None

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        self._build()

        # Center over parent.
        self.transient(master)
        self.after(10, self._center_on_parent)
        self.protocol("WM_DELETE_WINDOW", self._cancel)

    # -- layout ------------------------------------------------------------
    def _build(self):
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=22, pady=(22, 6))
        header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(header, text="Choose your dictation trigger",
                     font=T.font("h1"), text_color=T.TEXT, anchor="w").grid(
            row=0, column=0, sticky="w")
        W.hint_label(
            header,
            "Press a key combo or click a mouse button. OpenVerba will record "
            "while you hold the recording state and type when you trigger again.",
            color=T.TEXT_MUTED).grid(row=1, column=0, sticky="ew", pady=(4, 0))

        # --- Live capture card ---
        cap = W.Card(self)
        cap.grid(row=1, column=0, sticky="ew", padx=22, pady=10)
        cap.grid_columnconfigure(0, weight=1)

        self._capture_btn = W.accent_button(
            cap, "Click here, then press your trigger",
            command=self._toggle_record, height=46)
        self._capture_btn.grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 8))

        self._status = ctk.CTkLabel(
            cap, text="Detected: " + self._fmt_current(),
            font=T.font("body_bold"), text_color=T.TEXT, anchor="center")
        self._status.grid(row=1, column=0, sticky="ew", padx=16, pady=(2, 4))

        self._warn = ctk.CTkLabel(
            cap, text="", font=T.font("small"), text_color=T.WARN,
            anchor="center", wraplength=460, justify="center")
        self._warn.grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 14))
        self._refresh_detected(classify_trigger(self._current) if self._current
                               else None, initial=True)

        # --- Presets ---
        ctk.CTkLabel(self, text="Clean presets", font=T.font("h3"),
                     text_color=T.TEXT_MUTED, anchor="w").grid(
            row=2, column=0, sticky="nw", padx=24, pady=(8, 0))
        presets = ctk.CTkScrollableFrame(self, fg_color="transparent")
        presets.grid(row=3, column=0, sticky="nsew", padx=18, pady=(2, 4))
        presets.grid_columnconfigure(0, weight=1)
        for i, p in enumerate(PRESETS):
            self._preset_row(presets, p).grid(
                row=i, column=0, sticky="ew", padx=4, pady=4)
        self.grid_rowconfigure(3, weight=1)

        # --- Footer buttons ---
        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.grid(row=4, column=0, sticky="ew", padx=22, pady=(8, 18))
        footer.grid_columnconfigure(0, weight=1)
        W.secondary_button(footer, "Cancel", command=self._cancel,
                           width=110).grid(row=0, column=1, padx=(8, 0))
        self._save_btn = W.accent_button(footer, "Save trigger",
                                         command=self._save, width=140)
        self._save_btn.grid(row=0, column=2, padx=(8, 0))

    def _preset_row(self, master, preset):
        row = ctk.CTkFrame(master, fg_color=T.SURFACE_2, corner_radius=T.RADIUS_SM,
                           border_width=1, border_color=T.BORDER)
        row.grid_columnconfigure(0, weight=1)
        info = ctk.CTkFrame(row, fg_color="transparent")
        info.grid(row=0, column=0, sticky="ew", padx=14, pady=10)
        info.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(info, text=preset["label"], font=T.font("body_bold"),
                     text_color=T.TEXT, anchor="w").grid(
            row=0, column=0, sticky="w")
        W.hint_label(info, preset.get("note", ""), color=T.TEXT_MUTED,
                     wraplength=320).grid(row=1, column=0, sticky="w", pady=(2, 0))
        W.secondary_button(row, "Use", width=70,
                           command=lambda t=preset["trigger"]: self._pick_preset(t)
                           ).grid(row=0, column=1, padx=(0, 12))
        return row

    # -- recording ---------------------------------------------------------
    def _toggle_record(self):
        if self._recorder is not None:
            self._stop_record()
            return
        self._capture_btn.configure(text="Listening... press your trigger now",
                                    fg_color=T.WARN, hover_color=T.WARN,
                                    text_color="#231a00")
        self._status.configure(text="Listening...", text_color=T.WARN)
        self._warn.configure(text="If nothing appears, that button can't be "
                                  "detected — try another.")
        self._recorder = TriggerRecorder(
            on_detect=self._on_detect, on_status=self._on_status)
        try:
            self._recorder.start()
        except Exception as exc:
            self._recorder = None
            self._reset_capture_btn()
            self._status.configure(text="Could not start capture: %s" % exc,
                                   text_color=T.DANGER)

    def _stop_record(self):
        rec, self._recorder = self._recorder, None
        if rec is not None:
            try:
                rec.stop()
            except Exception:
                pass
        self._reset_capture_btn()

    def _reset_capture_btn(self):
        self._capture_btn.configure(text="Click here, then press your trigger",
                                    fg_color=T.ACCENT, hover_color=T.ACCENT_DARK,
                                    text_color="#06201d")

    def _on_status(self, text):
        self.after(0, lambda: self._status.configure(text=text,
                                                     text_color=T.WARN))

    def _on_detect(self, info):
        # Recorder auto-stops after first detection (bg thread). Marshal to UI.
        self.after(0, lambda: self._handle_detected(info))

    def _handle_detected(self, info):
        self._recorder = None
        self._reset_capture_btn()
        self._chosen = info.get("trigger")
        self._refresh_detected(info)

    def _refresh_detected(self, info, initial=False):
        if not info:
            self._status.configure(text="Detected: (none yet)",
                                   text_color=T.TEXT_MUTED)
            self._warn.configure(text="")
            return
        label = info.get("label") or info.get("trigger") or "(none)"
        if info.get("clean", True):
            self._status.configure(text="Detected:  %s  ✓" % label,
                                   text_color=T.OK)
            self._warn.configure(text="Clean choice — no conflicts expected.",
                                 text_color=T.TEXT_MUTED)
        else:
            self._status.configure(text="Detected:  %s  ⚠" % label,
                                   text_color=T.WARN)
            self._warn.configure(
                text=info.get("warning") or "This trigger can conflict with "
                "normal mouse/keyboard use.", text_color=T.WARN)

    def _pick_preset(self, trigger):
        self._stop_record()
        self._chosen = trigger
        self._refresh_detected(classify_trigger(trigger))

    # -- save / cancel -----------------------------------------------------
    def _fmt_current(self):
        if not self._current:
            return "(none yet)"
        return classify_trigger(self._current).get("label") or self._current

    def _save(self):
        self._stop_record()
        if not self._chosen:
            self._status.configure(text="Pick or capture a trigger first.",
                                   text_color=T.DANGER)
            return
        if self._on_apply is not None:
            try:
                ok = self._on_apply(self._chosen)
            except Exception as exc:
                self._status.configure(
                    text="Could not apply: %s" % exc, text_color=T.DANGER)
                return
            if ok is False:
                self._status.configure(
                    text="That trigger could not be registered. "
                         "Try another.", text_color=T.DANGER)
                return
        self._result = self._chosen
        self._close()

    def _cancel(self):
        self._stop_record()
        self._result = None
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

    def show(self):
        """Block until the dialog closes; return the chosen trigger or None."""
        self.wait_window()
        return self._result
