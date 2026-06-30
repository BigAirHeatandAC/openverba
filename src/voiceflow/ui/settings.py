"""
gui.settings - the Settings view: model manager (catalog + installed,
download/delete, switch active model with confirm + reload), trigger, behavior
toggles (beep, trailing space, hallucination filter, autostart at login), open
log folder, and about/version.

Long operations (download/delete/switch+reload) run on background threads; UI
updates are marshalled via .after. The view talks to the App for engine-level
actions (active model switch reloads the engine) and persists config itself.
"""

from __future__ import annotations

import os
import threading
import traceback

import customtkinter as ctk

from . import theme as T
from . import widgets as W
from . import autostart

from voiceflow import __version__
from voiceflow import bugreport as vf_bugreport
from voiceflow import updater as vf_updater
from voiceflow import models as vf_models
from voiceflow import config as vf_config
from voiceflow import transcribe as vf_transcribe
from voiceflow import cuda
from voiceflow import ai as vf_ai
from voiceflow import ai_setup as vf_ai_setup
from voiceflow import hardware as vf_hardware
from voiceflow import history as vf_history
from voiceflow import snippets as vf_snippets
from voiceflow import modes as vf_modes
from voiceflow.constants import (
    DATA_DIR, LOG_PATH, RECORDINGS_DIR, CORRECTIONS_PATH, PERSONAL_VOCAB_PATH,
    APP_DISPLAY_NAME,
)
from voiceflow.triggers import classify_trigger


# ---------------------------------------------------------------------------
# Pure status-decision helpers (no Tk) -- kept out of the widget so the probe
# results can be turned into display text/colour deterministically and unit-
# tested without a display. The async probe worker computes (rec, avail, gpu)
# off the UI thread; these turn that into what each card shows.
# ---------------------------------------------------------------------------
def _ai_status(rec, avail, model):
    """AI-edit card status. Returns (text, color_key, show_enable_button).

    color_key is one of "ok"/"muted" (mapped to theme colors by the caller);
    show_enable_button is True when the PC is capable but AI isn't set up yet."""
    if avail:
        return ("Ready (%s)" % model, "ok", False)
    if (rec or {}).get("capable"):
        return ("Not set up yet", "muted", True)
    return ("Not recommended for this PC", "muted", False)


def _cleanup_status(avail):
    """Auto-cleanup card status. Light is always available (rule-based, offline);
    medium/high need Ollama. Returns (text, color_key)."""
    return ("Ready" if avail else "Not set up", "ok" if avail else "muted")


def _gpu_status(present):
    """System-card GPU runtime status. Returns (text, color_key, show_enable)."""
    return ("Installed" if present else "Not installed",
            "ok" if present else "muted", not present)


class SettingsView(ctk.CTkFrame):
    def __init__(self, master, app, **kw):
        kw.setdefault("fg_color", T.BG)
        super().__init__(master, **kw)
        self.app = app
        self.cfg = app.cfg
        self.download_root = vf_config.resolve_download_root(self.cfg)

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        self._model_rows = {}
        self._busy = False
        self._build()

    # ------------------------------------------------------------- layout
    def _build(self):
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=24, pady=(20, 6))
        top.grid_columnconfigure(1, weight=1)
        W.ghost_button(top, "←  Back", command=self.app.show_dashboard,
                       width=90).grid(row=0, column=0)
        ctk.CTkLabel(top, text="Settings", font=T.font("h1"),
                     text_color=T.TEXT).grid(row=0, column=1, sticky="w",
                                             padx=12)

        scroll = ctk.CTkScrollableFrame(self, fg_color="transparent")
        scroll.grid(row=1, column=0, sticky="nsew", padx=18, pady=(2, 16))
        scroll.grid_columnconfigure(0, weight=1)

        self._build_trigger(scroll).grid(row=0, column=0, sticky="ew", pady=6)
        self._build_streaming(scroll).grid(row=1, column=0, sticky="ew", pady=6)
        self._build_modes(scroll).grid(row=2, column=0, sticky="ew", pady=6)
        self._build_language(scroll).grid(row=3, column=0, sticky="ew", pady=6)
        self._build_snippets(scroll).grid(row=4, column=0, sticky="ew", pady=6)
        self._build_ai(scroll).grid(row=5, column=0, sticky="ew", pady=6)
        self._build_cleanup(scroll).grid(row=6, column=0, sticky="ew", pady=6)
        self._build_models(scroll).grid(row=7, column=0, sticky="ew", pady=6)
        self._build_behavior(scroll).grid(row=8, column=0, sticky="ew", pady=6)
        self._build_history(scroll).grid(row=9, column=0, sticky="ew", pady=6)
        self._build_system(scroll).grid(row=10, column=0, sticky="ew", pady=6)
        self._build_updates(scroll).grid(row=11, column=0, sticky="ew", pady=6)
        self._build_help(scroll).grid(row=12, column=0, sticky="ew", pady=6)
        self._build_about(scroll).grid(row=13, column=0, sticky="ew", pady=6)

        # Status toast line.
        self._toast = ctk.CTkLabel(self, text="", font=T.font("small_bold"),
                                   text_color=T.ACCENT, anchor="w")
        self._toast.grid(row=2, column=0, sticky="ew", padx=26, pady=(0, 8))

        # The AI/cleanup/system cards above render "Checking…" placeholders; run
        # the (potentially slow) hardware/Ollama/GPU-runtime probes on a daemon
        # thread and fill them in via .after so opening Settings is instant.
        threading.Thread(target=self._probe_async, daemon=True,
                         name="settings-probe").start()

    # ---------------------------------------------------- async probes (B.1)
    def _probe_async(self):
        """Off-thread: run the probes once and marshal the results back to the UI
        thread. detect_hardware()/gpu_runtime_present() are session-cached and
        is_available() is TTL-cached, so re-opens are essentially free."""
        try:
            hw = vf_hardware.detect_hardware()       # cached after 1st probe
            rec = vf_hardware.recommend_ai(hw)
            avail = vf_ai.is_available(self.cfg)     # ONE probe, shared by cards
            gpu = cuda.gpu_runtime_present()         # cached
        except Exception:
            rec, avail, gpu = {}, False, False
        try:
            self.after(0, lambda: self._apply_probes(rec, avail, gpu))
        except Exception:
            pass

    def _apply_probes(self, rec, avail, gpu):
        """UI thread: fill the placeholder widgets from the probe results. Guards
        against the view being destroyed (user navigated Back mid-probe)."""
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return
        try:
            self._apply_ai_probe(rec, avail)
            self._apply_cleanup_probe(avail)
            self._apply_gpu_probe(gpu)
        except Exception:
            pass

    _COLOR_KEYS = None

    def _color(self, key):
        return {"ok": T.OK, "muted": T.TEXT_MUTED}.get(key, T.TEXT_MUTED)

    # --------------------------------------------------------- language card
    # (label, whisper language code | None). None = auto-detect.
    _LANG_OPTIONS = [
        ("Auto-detect", None),
        ("English", "en"),
        ("Spanish", "es"),
        ("French", "fr"),
        ("German", "de"),
        ("Italian", "it"),
        ("Portuguese", "pt"),
        ("Dutch", "nl"),
        ("Russian", "ru"),
        ("Chinese", "zh"),
        ("Japanese", "ja"),
    ]

    def _build_language(self, master):
        """Language picker + translate-to-English toggle + English-only warning."""
        card = W.Card(master, title="Language")
        card.grid_columnconfigure(0, weight=1)
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 16))
        inner.grid_columnconfigure(0, weight=1)

        # --- recognition language dropdown ---
        ctk.CTkLabel(inner, text="Recognition language",
                     font=T.font("body_bold"), text_color=T.TEXT,
                     anchor="w").grid(row=0, column=0, sticky="w")
        W.hint_label(
            inner,
            "Auto-detect lets the model guess the language from your audio. "
            "Pick a specific language if you always speak the same one "
            "(slightly faster and more reliable).",
            color=T.TEXT_MUTED, wraplength=560).grid(
                row=1, column=0, columnspan=2, sticky="w", pady=(1, 8))

        current = self.cfg.get("language")
        self._lang_var = ctk.StringVar(
            value=next((label for label, code in self._LANG_OPTIONS
                        if code == current), "Auto-detect"))
        ctk.CTkOptionMenu(
            inner, values=[label for label, _ in self._LANG_OPTIONS],
            variable=self._lang_var, font=T.font("body"),
            command=self._on_language_change).grid(
                row=2, column=0, sticky="w", pady=(0, 12))

        # --- translate-to-English toggle ---
        ctk.CTkLabel(inner, text="Translate to English",
                     font=T.font("body_bold"), text_color=T.TEXT,
                     anchor="w").grid(row=3, column=0, sticky="w")
        W.hint_label(
            inner,
            "On (needs a multilingual model): speech is recognized in the "
            "selected language, then translated to English before pasting. "
            "Off: paste in the original language.",
            color=T.TEXT_MUTED, wraplength=560).grid(
                row=4, column=0, sticky="w", pady=(1, 8))
        self._translate_var = ctk.BooleanVar(
            value=bool(self.cfg.get("translate_to_english", False)))
        ctk.CTkSwitch(
            inner, text="", variable=self._translate_var,
            onvalue=True, offvalue=False, progress_color=T.ACCENT,
            command=self._on_translate_toggle).grid(
                row=3, column=1, rowspan=2, sticky="e", padx=(12, 0))

        # --- English-only model warning ---
        model = self.cfg.get("model") or ""
        if vf_transcribe.is_english_only_model(model):
            W.hint_label(
                inner,
                "⚠ Active model (%s) is English-only. To recognize other "
                "languages or translate, switch to a multilingual model "
                "(e.g. small, medium, or large-v3) in Models below." % model,
                color=T.WARN, wraplength=560).grid(
                    row=5, column=0, columnspan=2, sticky="w", pady=(8, 0))
        return card

    def _on_language_change(self, selection):
        """Persist the language config when the dropdown changes."""
        code = next((c for label, c in self._LANG_OPTIONS if label == selection),
                    None)
        self.cfg["language"] = code
        vf_config.save_config(self.cfg)
        self._toast_msg("Language set to %s." % selection, T.OK)

    def _on_translate_toggle(self):
        """Persist the translate-to-English config when the toggle changes."""
        val = bool(self._translate_var.get())
        self.cfg["translate_to_english"] = val
        vf_config.save_config(self.cfg)
        self._toast_msg(
            "Translate to English: ON" if val else "Translate to English: OFF",
            T.OK)

    # --------------------------------------------------------- trigger card
    def _build_trigger(self, master):
        card = W.Card(master, title="Trigger")
        card.grid_columnconfigure(0, weight=1)
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 16))
        inner.grid_columnconfigure(0, weight=1)
        self._trig_lbl = ctk.CTkLabel(inner, text="", font=T.font("h3"),
                                      text_color=T.ACCENT, anchor="w")
        self._trig_lbl.grid(row=0, column=0, sticky="w")
        self._trig_warn = W.hint_label(inner, "", color=T.WARN, wraplength=480)
        self._trig_warn.grid(row=1, column=0, sticky="w", pady=(2, 0))
        W.secondary_button(inner, "Change trigger",
                           command=self.app.open_trigger_picker, width=160,
                           height=36).grid(row=0, column=1, rowspan=2,
                                           sticky="e")
        self.refresh_trigger()
        return card

    def refresh_trigger(self):
        trig = self.cfg.get("trigger")
        info = classify_trigger(trig) if trig else {}
        self._trig_lbl.configure(text=info.get("label") or trig or "—")
        if info.get("clean", True):
            self._trig_warn.configure(text="Clean choice — no conflicts "
                                           "expected.", text_color=T.TEXT_MUTED)
        else:
            self._trig_warn.configure(text="⚠ " + (info.get("warning") or ""),
                                      text_color=T.WARN)

    # -------------------------------------------------------- real-time card
    # 3-way mode control: segment label <-> internal mode string.
    _MODE_SEGMENTS = ("Off", "Live-type", "Live-preview")
    _SEGMENT_TO_MODE = {
        "Off": "batch",
        "Live-type": "streaming",
        "Live-preview": "preview",
    }
    _MODE_TO_SEGMENT = {v: k for k, v in _SEGMENT_TO_MODE.items()}

    def _build_streaming(self, master):
        card = W.Card(master, title="Real-time mode (beta)")
        card.grid_columnconfigure(0, weight=1)
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 16))
        inner.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(inner, text="See words as you speak",
                     font=T.font("body_bold"), text_color=T.TEXT,
                     anchor="w").grid(row=0, column=0, sticky="w")
        W.hint_label(
            inner,
            "Off: the normal press → speak → paste flow (default). "
            "Live-type: confirmed words are typed into the focused app live, a "
            "beat behind your voice (clipboard untouched). "
            "Live-preview: a floating bar shows rough words as you speak; when "
            "you stop, the clean, accurate text is pasted once (nothing rough "
            "ever lands in your document). A smaller/faster model (e.g. base.en) "
            "feels best for real-time.",
            color=T.TEXT_MUTED, wraplength=560).grid(row=1, column=0, sticky="w",
                                                     pady=(1, 0))
        init_mode = self.cfg.get("mode", "batch")
        init_seg = self._MODE_TO_SEGMENT.get(init_mode, "Off")
        self._mode_var = ctk.StringVar(value=init_seg)
        seg = ctk.CTkSegmentedButton(
            inner, values=list(self._MODE_SEGMENTS), variable=self._mode_var,
            selected_color=T.ACCENT, selected_hover_color=T.ACCENT_DARK,
            unselected_color=T.SURFACE_2, unselected_hover_color=T.SURFACE_3,
            text_color=T.TEXT, command=self._on_mode_toggle)
        seg.grid(row=2, column=0, sticky="w", pady=(12, 0))
        return card

    def _on_mode_toggle(self, _segment=None):
        seg = self._mode_var.get()
        mode = self._SEGMENT_TO_MODE.get(seg, "batch")
        self.cfg["mode"] = mode
        vf_config.save_config(self.cfg)
        self.app.apply_mode_change(mode)
        msg = {
            "streaming": "Live-type ON — words type as you speak.",
            "preview": "Live-preview ON — a floating bar shows words; clean "
                       "text is pasted when you stop.",
            "batch": "Real-time off (back to press → speak → paste).",
        }.get(mode, "Mode updated.")
        self._toast_msg(msg, T.OK)

    # ---------------------------------------------------- per-app modes card
    def _build_modes(self, master):
        """Per-app context modes: a toggle to auto-format by the active app + a
        button to manage (add/edit/delete) the modes."""
        card = W.Card(master, title="Per-app context modes (beta)")
        card.grid_columnconfigure(0, weight=1)
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 16))
        inner.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(inner, text="Auto-format by app",
                     font=T.font("body_bold"), text_color=T.TEXT,
                     anchor="w").grid(row=0, column=0, sticky="w")
        W.hint_label(
            inner,
            "On: dictation is auto-formatted for the active app (e.g. Email is "
            "formal, Slack/Discord is casual, Code is left untouched). Off: uses "
            "one static prompt. Modes are fully editable. Falls back safely if "
            "the app can't be detected.",
            color=T.TEXT_MUTED, wraplength=560).grid(
                row=1, column=0, sticky="w", pady=(1, 0))

        init = bool(self.cfg.get("per_app_modes", False))
        self._modes_var = ctk.BooleanVar(value=init)
        sw = ctk.CTkSwitch(
            inner, text="", variable=self._modes_var, onvalue=True,
            offvalue=False, progress_color=T.ACCENT, button_color=T.TEXT,
            button_hover_color=T.TEXT, command=self._on_modes_toggle)
        sw.grid(row=0, column=1, rowspan=2, sticky="e", padx=(12, 0))

        W.secondary_button(inner, "Manage modes", width=150, height=36,
                           command=self._open_modes_dialog).grid(
                               row=2, column=0, sticky="w", pady=(12, 0))
        return card

    def _on_modes_toggle(self):
        enabled = bool(self._modes_var.get())
        self.cfg["per_app_modes"] = enabled
        vf_config.save_config(self.cfg)
        try:
            self.app.apply_behavior_change("per_app_modes", enabled)
        except Exception:
            pass
        self._toast_msg(
            "Per-app modes ON — formatting adapts to your app."
            if enabled else "Per-app modes off (using static prompt).",
            T.OK)

    def _open_modes_dialog(self):
        """Open the Modes manager dialog (list / add / edit / delete)."""
        changed = _ModesDialog(self).show()
        if changed:
            try:
                self.app.reload_modes()
            except Exception:
                pass
            self._toast_msg("Modes updated.", T.OK)

    # -------------------------------------------------------- snippets card
    def _build_snippets(self, master):
        """Voice snippets (text expansion): enable toggle + a table of
        trigger -> expansion pairs with add/edit/delete/clear."""
        card = W.Card(master, title="Voice snippets")
        card.grid_columnconfigure(0, weight=1)
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 16))
        inner.grid_columnconfigure(0, weight=1)

        W.hint_label(
            inner,
            "Say a short trigger and it auto-expands to a phrase before pasting "
            "-- e.g. say \"brb\" → \"be right back\". Expansions happen "
            "after corrections, in the normal press → speak → paste "
            "mode only (not real-time mode).",
            color=T.TEXT_MUTED, wraplength=600).grid(row=0, column=0, sticky="w",
                                                     pady=(0, 10))

        # Enable toggle.
        toggle = ctk.CTkFrame(inner, fg_color="transparent")
        toggle.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        toggle.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(toggle, text="Enable snippets", font=T.font("body_bold"),
                     text_color=T.TEXT, anchor="w").grid(row=0, column=0,
                                                         sticky="w")
        self._snippets_enabled_var = ctk.BooleanVar(
            value=bool(self.cfg.get("snippets_enabled", True)))
        ctk.CTkSwitch(
            toggle, text="", variable=self._snippets_enabled_var,
            onvalue=True, offvalue=False, progress_color=T.ACCENT,
            button_color=T.TEXT, button_hover_color=T.TEXT,
            command=self._on_snippets_toggle).grid(row=0, column=1, sticky="e",
                                                   padx=(12, 0))

        # Table header.
        hdr = ctk.CTkFrame(inner, fg_color="transparent")
        hdr.grid(row=2, column=0, sticky="ew", pady=(0, 4))
        hdr.grid_columnconfigure(0, weight=1)
        hdr.grid_columnconfigure(1, weight=2)
        ctk.CTkLabel(hdr, text="Trigger", font=T.font("small_bold"),
                     text_color=T.TEXT_MUTED, anchor="w").grid(row=0, column=0,
                                                               sticky="w")
        ctk.CTkLabel(hdr, text="Expansion", font=T.font("small_bold"),
                     text_color=T.TEXT_MUTED, anchor="w").grid(
            row=0, column=1, sticky="w", padx=(12, 0))

        # Scrollable rows.
        self._snippets_list = vf_snippets.load_snippets()
        self._snippets_table_rows = []
        self._snippets_scroll = ctk.CTkScrollableFrame(
            inner, fg_color="transparent", height=130)
        self._snippets_scroll.grid(row=3, column=0, sticky="ew", pady=(0, 8))
        self._snippets_scroll.grid_columnconfigure(0, weight=1)
        self._snippets_scroll.grid_columnconfigure(1, weight=2)
        self._refresh_snippets_table()

        # Buttons.
        btns = ctk.CTkFrame(inner, fg_color="transparent")
        btns.grid(row=4, column=0, sticky="w", pady=(4, 0))
        W.secondary_button(btns, "Add snippet", width=140, height=34,
                           command=self._add_snippet).grid(row=0, column=0,
                                                           padx=(0, 8))
        W.danger_button(btns, "Clear all", width=120,
                        command=self._clear_snippets).grid(row=0, column=1)
        return card

    def _refresh_snippets_table(self):
        """(Re)draw the snippet rows from self._snippets_list."""
        for r in self._snippets_table_rows:
            try:
                r.destroy()
            except Exception:
                pass
        self._snippets_table_rows = []
        if not self._snippets_list:
            empty = ctk.CTkLabel(self._snippets_scroll,
                                 text="No snippets yet. Add one to get started.",
                                 font=T.font("small"), text_color=T.TEXT_FAINT,
                                 anchor="w")
            empty.grid(row=0, column=0, columnspan=3, sticky="w", pady=4)
            self._snippets_table_rows.append(empty)
            return
        for i, snip in enumerate(self._snippets_list):
            row = ctk.CTkFrame(self._snippets_scroll, fg_color="transparent")
            row.grid(row=i, column=0, columnspan=3, sticky="ew", pady=(0, 4))
            row.grid_columnconfigure(0, weight=1)
            row.grid_columnconfigure(1, weight=2)
            ctk.CTkLabel(row, text=str(snip.get("trigger", "")),
                         font=T.font("body"), text_color=T.TEXT,
                         anchor="w").grid(row=0, column=0, sticky="w")
            exp = str(snip.get("expansion", ""))
            exp_display = (exp[:40] + "…") if len(exp) > 40 else exp
            ctk.CTkLabel(row, text=exp_display, font=T.font("body"),
                         text_color=T.TEXT_MUTED, anchor="w").grid(
                row=0, column=1, sticky="w", padx=(12, 0))
            actions = ctk.CTkFrame(row, fg_color="transparent")
            actions.grid(row=0, column=2, sticky="e", padx=(12, 0))
            W.ghost_button(actions, "Edit", width=52, height=28,
                           command=lambda idx=i: self._edit_snippet(idx)).grid(
                row=0, column=0, padx=(0, 4))
            W.danger_button(actions, "Delete", width=66, height=28,
                            command=lambda idx=i: self._delete_snippet(idx)).grid(
                row=0, column=1)
            self._snippets_table_rows.append(row)

    def _on_snippets_toggle(self):
        enabled = bool(self._snippets_enabled_var.get())
        self.cfg["snippets_enabled"] = enabled
        vf_config.save_config(self.cfg)
        try:
            self.app.on_snippets_changed()
        except Exception:
            pass
        self._toast_msg("Snippets enabled." if enabled else "Snippets disabled.",
                        T.OK)

    def _add_snippet(self):
        self._show_snippet_dialog(None, -1)

    def _edit_snippet(self, index):
        if 0 <= index < len(self._snippets_list):
            self._show_snippet_dialog(self._snippets_list[index], index)

    def _delete_snippet(self, index):
        if 0 <= index < len(self._snippets_list):
            if self._confirm("Delete this snippet?",
                             "Remove this snippet? You can add it back later."):
                del self._snippets_list[index]
                self._save_and_refresh_snippets()

    def _clear_snippets(self):
        if not self._snippets_list:
            self._toast_msg("No snippets to clear.", T.WARN)
            return
        if self._confirm("Clear all snippets?",
                         "Delete every snippet? This can't be undone. (You can "
                         "add them back later.)"):
            self._snippets_list = []
            self._save_and_refresh_snippets()

    def _show_snippet_dialog(self, snippet, index):
        result = _SnippetDialog(self, snippet, edit=(index >= 0)).show()
        if result is None:
            return
        trigger, expansion = result
        if index >= 0:
            self._snippets_list[index] = {
                "trigger": trigger, "expansion": expansion, "enabled": True}
        else:
            self._snippets_list.append({
                "trigger": trigger, "expansion": expansion, "enabled": True})
        self._save_and_refresh_snippets()

    def _save_and_refresh_snippets(self):
        vf_snippets.save_snippets(self._snippets_list)
        try:
            self.app.on_snippets_changed()
        except Exception:
            pass
        self._refresh_snippets_table()
        self._toast_msg("Saved.", T.OK)

    # -------------------------------------------------------- AI edit card
    def _build_ai(self, master):
        card = W.Card(master, title="Smart AI editing (optional)")
        card.grid_columnconfigure(0, weight=1)
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 16))
        inner.grid_columnconfigure(0, weight=1)
        W.hint_label(
            inner,
            "In command mode, say an instruction instead of a key command and "
            "the AI rewrites your SELECTED text -- e.g. \"make this sound more "
            "natural\", \"remove the word dog\", \"make it shorter\". Runs 100% "
            "locally and free (via Ollama) -- nothing is sent to the cloud.",
            color=T.TEXT_MUTED, wraplength=600).grid(row=0, column=0, sticky="w")
        # Placeholders -- the slow recommend_ai()/is_available() probes run on a
        # daemon thread and fill these in via _apply_ai_probe (B.1).
        self._ai_inner = inner
        self._ai_status_row = W.StatRow(inner, "Status", "Checking…",
                                        value_color=T.TEXT_MUTED)
        self._ai_status_row.grid(row=1, column=0, sticky="ew", pady=(8, 2))
        self._ai_reason_lbl = W.hint_label(inner, "", color=T.TEXT_FAINT,
                                           wraplength=600)
        self._ai_reason_lbl.grid(row=2, column=0, sticky="w")
        self._ai_enable_btn = None
        return card

    def _apply_ai_probe(self, rec, avail):
        text, color_key, show_enable = _ai_status(
            rec, avail, self.cfg.get("ai_model"))
        try:
            self._ai_status_row.set_value(text, self._color(color_key))
        except Exception:
            # StatRow may not expose set_value; rebuild defensively.
            pass
        try:
            self._ai_reason_lbl.configure(text=(rec or {}).get("reason", ""))
        except Exception:
            pass
        if show_enable and self._ai_enable_btn is None:
            self._ai_enable_btn = W.secondary_button(
                self._ai_inner, "Enable smart AI editing", width=220, height=36,
                command=lambda: self._setup_ai(
                    (rec or {}).get("model") or "qwen2.5:3b"))
            self._ai_enable_btn.grid(row=3, column=0, sticky="w", pady=(10, 0))

    def _setup_ai(self, model):
        ok = _AiSetupDialog(self, model).show()
        if ok:
            self.cfg["ai_edit"] = True
            self.cfg["ai_provider"] = "ollama"
            self.cfg["ai_model"] = model
            vf_config.save_config(self.cfg)
            try:
                self.app.apply_behavior_change("ai_edit", True)
            except Exception:
                pass
            self._toast_msg("Smart AI editing is ready.", T.OK)
        self.app.show_settings()

    # ------------------------------------------------------- auto-cleanup card
    def _build_cleanup(self, master):
        card = W.Card(master, title="AI auto-cleanup (optional)")
        card.grid_columnconfigure(0, weight=1)
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 16))
        inner.grid_columnconfigure(0, weight=1)
        W.hint_label(
            inner,
            "Automatically polish raw dictation: fix punctuation, "
            "capitalization, remove 'um/uh' filler, and optionally rephrase. "
            "Runs locally via Ollama (the same AI engine as smart editing). "
            "Falls back gracefully if Ollama isn't available or times out. "
            "Normal press → speak → paste mode only (not real-time mode).",
            color=T.TEXT_MUTED, wraplength=600).grid(row=0, column=0, sticky="w")

        # Status placeholder -- filled by _apply_cleanup_probe off-thread (B.1).
        self._cleanup_status_row = W.StatRow(inner, "Status", "Checking…",
                                             value_color=T.TEXT_MUTED)
        self._cleanup_status_row.grid(row=1, column=0, sticky="ew", pady=(8, 2))

        # Enable toggle.
        self._cleanup_var = ctk.BooleanVar(
            value=bool(self.cfg.get("auto_cleanup", False)))
        ctk.CTkSwitch(
            inner, text="Enable auto-cleanup", variable=self._cleanup_var,
            onvalue=True, offvalue=False,
            progress_color=T.ACCENT, button_color=T.TEXT,
            button_hover_color=T.TEXT,
            command=self._on_cleanup_toggle).grid(
            row=2, column=0, sticky="w", pady=(8, 0))

        # Aggressiveness level (off / light / medium / high).
        level_frame = ctk.CTkFrame(inner, fg_color="transparent")
        level_frame.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        ctk.CTkLabel(level_frame, text="Aggressiveness:",
                     font=T.font("body_bold"), text_color=T.TEXT,
                     anchor="w").grid(row=0, column=0, sticky="w")
        self._cleanup_level_var = ctk.StringVar(
            value=self.cfg.get("cleanup_level", "light"))
        for col, (val, label) in enumerate([
            ("off", "Off"),
            ("light", "Light"),
            ("medium", "Medium"),
            ("high", "High"),
        ]):
            ctk.CTkRadioButton(
                level_frame, text=label, variable=self._cleanup_level_var,
                value=val, command=self._on_cleanup_level_change,
                text_color=T.TEXT).grid(row=0, column=col + 1, sticky="w",
                                        padx=(8, 0))

        W.hint_label(
            inner,
            "Light: punctuation, caps, filler only. Medium: + false-starts, "
            "light grammar. High: + rephrase for clarity.",
            color=T.TEXT_FAINT, wraplength=600).grid(
            row=4, column=0, sticky="w", pady=(6, 0))
        W.hint_label(
            inner,
            "Light is instant and works fully offline (no AI model needed). "
            "Medium and High use a small local model (%s) kept warm for speed."
            % self.cfg.get("cleanup_model", "qwen2.5:1.5b"),
            color=T.TEXT_FAINT, wraplength=600).grid(
            row=5, column=0, sticky="w", pady=(4, 0))
        return card

    def _apply_cleanup_probe(self, avail):
        text, color_key = _cleanup_status(avail)
        try:
            self._cleanup_status_row.set_value(text, self._color(color_key))
        except Exception:
            pass

    def _on_cleanup_toggle(self):
        val = bool(self._cleanup_var.get())
        self.cfg["auto_cleanup"] = val
        vf_config.save_config(self.cfg)
        self._toast_msg(
            "AI auto-cleanup enabled." if val else "AI auto-cleanup disabled.",
            T.OK)

    def _on_cleanup_level_change(self):
        level = self._cleanup_level_var.get()
        self.cfg["cleanup_level"] = level
        vf_config.save_config(self.cfg)
        self._toast_msg("Cleanup aggressiveness: %s." % level, T.OK)

    # ---------------------------------------------------------- models card
    def _build_models(self, master):
        card = W.Card(master, title="Models")
        card.grid_columnconfigure(0, weight=1)
        W.hint_label(card, "Download more models, switch the active one, or "
                           "free up disk space. The active model loads when you "
                           "start dictation.", color=T.TEXT_MUTED,
                     wraplength=620).grid(row=1, column=0, sticky="w", padx=18)
        self._models_holder = ctk.CTkFrame(card, fg_color="transparent")
        self._models_holder.grid(row=2, column=0, sticky="ew", padx=12,
                                 pady=(8, 14))
        self._models_holder.grid_columnconfigure(0, weight=1)
        self.refresh_models()
        return card

    def refresh_models(self):
        for w in self._models_holder.winfo_children():
            w.destroy()
        self._model_rows = {}
        active = self.cfg.get("model")
        for i, meta in enumerate(vf_models.MODEL_CATALOG):
            row = self._model_row(self._models_holder, meta, active)
            row.grid(row=i, column=0, sticky="ew", padx=4, pady=4)

    def _model_row(self, master, meta, active):
        mid = meta["id"]
        installed = vf_models.is_downloaded(mid, self.download_root)
        is_active = (mid == active)
        size_disk = vf_models.model_disk_size_mb(mid, self.download_root)

        row = ctk.CTkFrame(master, fg_color=T.SURFACE_2, corner_radius=T.RADIUS_SM,
                           border_width=2,
                           border_color=T.ACCENT if is_active else T.BORDER)
        row.grid_columnconfigure(0, weight=1)

        left = ctk.CTkFrame(row, fg_color="transparent")
        left.grid(row=0, column=0, sticky="ew", padx=14, pady=10)
        left.grid_columnconfigure(2, weight=1)
        ctk.CTkLabel(left, text=meta["label"], font=T.font("body_bold"),
                     text_color=T.TEXT, anchor="w").grid(row=0, column=0,
                                                         sticky="w")
        if is_active:
            W.Badge(left, "Active", color=T.ACCENT,
                    text_color="#06201d").grid(row=0, column=1, padx=8)
        elif installed:
            W.Badge(left, "Installed", color=T.OK,
                    text_color="#06231a").grid(row=0, column=1, padx=8)

        disk = meta.get("disk_mb")
        size_s = ("%.1f GB" % (disk / 1024.0)) if disk and disk >= 1024 \
            else ("%s MB" % disk if disk else "?")
        on_disk = (" · %s MB on disk" % size_disk) if size_disk else ""
        lang = "English" if meta.get("languages") == "en" else "Multilingual"
        W.hint_label(
            left,
            "%s  ·  %s  ·  ~%s download%s" % (
                meta.get("notes", "").split(".")[0], lang, size_s, on_disk),
            color=T.TEXT_MUTED, wraplength=560).grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(2, 0))

        # Action buttons.
        actions = ctk.CTkFrame(row, fg_color="transparent")
        actions.grid(row=0, column=1, padx=(0, 12))
        if not installed:
            btn = W.secondary_button(actions, "Download", width=110,
                                     command=lambda m=mid: self._download(m))
            btn.grid(row=0, column=0, padx=4)
        else:
            if not is_active:
                W.accent_button(actions, "Use", width=80,
                                command=lambda m=mid: self._switch_active(m)
                                ).grid(row=0, column=0, padx=4)
                W.danger_button(actions, "Delete", width=90,
                                command=lambda m=mid: self._delete(m)).grid(
                    row=0, column=1, padx=4)
            else:
                ctk.CTkLabel(actions, text="In use", font=T.font("small"),
                             text_color=T.TEXT_FAINT).grid(row=0, column=0,
                                                           padx=8)

        # Inline progress (download) — hidden until used.
        prog = ctk.CTkProgressBar(row, height=8, corner_radius=4,
                                  progress_color=T.ACCENT, fg_color=T.SURFACE_3)
        prog.set(0)
        self._model_rows[mid] = {"row": row, "actions": actions, "prog": prog}
        return row

    def _download(self, mid):
        if self._busy:
            self._toast_msg("Please wait for the current operation to finish.",
                            T.WARN)
            return
        self._busy = True
        slot = self._model_rows.get(mid)
        if slot:
            slot["prog"].grid(row=1, column=0, columnspan=2, sticky="ew",
                              padx=14, pady=(0, 10))
            for w in slot["actions"].winfo_children():
                w.configure(state="disabled")
        self._toast_msg("Downloading %s…" % mid, T.ACCENT)

        def progress(frac, done, total, desc):
            self.after(0, lambda: self._dl_progress(mid, frac, done, total))

        def work():
            try:
                vf_models.download_model(mid, progress_cb=progress,
                                         download_root=self.download_root)
                self.after(0, lambda: self._dl_done(mid, True, None))
            except Exception as exc:
                tb = traceback.format_exc()
                self.after(0, lambda: self._dl_done(mid, False, exc))
        threading.Thread(target=work, daemon=True).start()

    def _dl_progress(self, mid, frac, done, total):
        slot = self._model_rows.get(mid)
        if not slot:
            return
        slot["prog"].set(max(0.0, min(1.0, frac)))
        if total:
            self._toast_msg("Downloading %s… %d%% (%.0f/%.0f MB)"
                            % (mid, int(frac * 100), done / 1e6, total / 1e6),
                            T.ACCENT)

    def _dl_done(self, mid, ok, exc):
        self._busy = False
        if ok:
            self._toast_msg("%s downloaded." % mid, T.OK)
        else:
            self._toast_msg("Download failed: %s" % exc, T.DANGER)
        self.download_root = vf_config.resolve_download_root(self.cfg)
        self.refresh_models()

    def _switch_active(self, mid):
        if self._busy:
            return
        if not self._confirm("Switch active model?",
                             "OpenVerba will switch to '%s' and reload the "
                             "engine. Dictation pauses for a moment while the "
                             "model loads." % mid):
            return
        self._busy = True
        self._toast_msg("Switching to %s and reloading…" % mid, T.ACCENT)

        def done(ok, info, exc):
            self._busy = False
            if ok:
                self.cfg["model"] = mid
                self._toast_msg("Now using %s (%s)." % (
                    mid, (info or {}).get("device") or "cpu"), T.OK)
            else:
                self._toast_msg("Could not load %s: %s" % (mid, exc), T.DANGER)
            self.refresh_models()
        self.app.switch_active_model(mid, on_done=lambda ok, info, exc:
                                     self.after(0, lambda: done(ok, info, exc)))

    def _delete(self, mid):
        if self._busy:
            return
        if mid == self.cfg.get("model"):
            self._toast_msg("Can't delete the active model. Switch first.",
                            T.WARN)
            return
        if not self._confirm("Delete model?",
                             "Delete '%s' from disk? You can re-download it "
                             "later." % mid):
            return
        try:
            vf_models.delete_model(mid, self.download_root)
            self._toast_msg("%s deleted." % mid, T.OK)
        except Exception as exc:
            self._toast_msg("Delete failed: %s" % exc, T.DANGER)
        self.refresh_models()

    # -------------------------------------------------------- behavior card
    def _build_behavior(self, master):
        card = W.Card(master, title="Behavior")
        card.grid_columnconfigure(0, weight=1)
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 16))
        inner.grid_columnconfigure(0, weight=1)

        self._sw = {}
        toggles = [
            ("beep", "Sound feedback",
             "Play short beeps when recording starts, stops, and finishes."),
            ("add_trailing_space", "Add a trailing space",
             "Append a space after each dictation so words don't run together."),
            ("filter_hallucinations", "Filter hallucinations",
             "Drop Whisper's silence artifacts (e.g. \"Thank you.\") instead of "
             "typing them."),
            ("allow_multiline", "Allow line breaks",
             "Keep newlines in the transcript. Off is safer in chat/terminal "
             "fields where a newline acts as Enter."),
            ("vad_filter", "Voice activity detection",
             "Trim leading/trailing silence before transcribing."),
            ("save_recordings", "Save recordings for debugging",
             "Save each recording's audio + transcript to the recordings folder "
             "so you can review what was heard vs. typed and improve accuracy. "
             "Off by default (uses disk space)."),
        ]
        for i, (key, label, hint) in enumerate(toggles):
            self._toggle_row(inner, i, key, label, hint)

        # Autostart (registry Run key, not a config-only flag).
        self._toggle_row(inner, len(toggles), "autostart",
                         "Start OpenVerba at login",
                         "Launch the background dictation runtime when you sign "
                         "in to Windows.", is_autostart=True)
        return card

    def _toggle_row(self, master, row, key, label, hint, is_autostart=False):
        f = ctk.CTkFrame(master, fg_color="transparent")
        f.grid(row=row, column=0, sticky="ew", pady=6)
        f.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(f, text=label, font=T.font("body_bold"),
                     text_color=T.TEXT, anchor="w").grid(row=0, column=0,
                                                         sticky="w")
        W.hint_label(f, hint, color=T.TEXT_MUTED, wraplength=560).grid(
            row=1, column=0, sticky="w", pady=(1, 0))
        if is_autostart:
            init = autostart.is_enabled()
        else:
            init = bool(self.cfg.get(key))
        var = ctk.BooleanVar(value=init)
        sw = ctk.CTkSwitch(
            f, text="", variable=var, onvalue=True, offvalue=False,
            progress_color=T.ACCENT, button_color=T.TEXT,
            button_hover_color=T.TEXT,
            command=lambda k=key, v=var, a=is_autostart: self._on_toggle(k, v, a))
        sw.grid(row=0, column=1, rowspan=2, sticky="e", padx=(12, 0))
        self._sw[key] = var

    def _on_toggle(self, key, var, is_autostart):
        val = bool(var.get())
        if is_autostart:
            ok = autostart.set_enabled(val)
            self.cfg["autostart"] = val
            vf_config.save_config(self.cfg)
            if ok:
                self._toast_msg("Start at login %s."
                                % ("enabled" if val else "disabled"), T.OK)
            else:
                self._toast_msg("Could not update the login setting.", T.DANGER)
            return
        self.cfg[key] = val
        vf_config.save_config(self.cfg)
        # Push live to the engine cfg (it shares the same dict in the App).
        self.app.apply_behavior_change(key, val)
        # The vocab/correction toggles change recognition; reload the engine's
        # learned data so the change takes effect on the next utterance.
        if key in ("personal_vocab_enabled", "corrections_enabled"):
            try:
                self.app.on_learned()
            except Exception:
                pass
        self._toast_msg("Saved.", T.OK)

    # ------------------------------------------------------- history card
    def _build_history(self, master):
        card = W.Card(master, title="History & learning")
        card.grid_columnconfigure(0, weight=1)
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 16))
        inner.grid_columnconfigure(0, weight=1)

        toggles = [
            ("transcript_history", "Keep a history of your transcripts",
             "Saves the TEXT of what you dictate so you can review and edit it "
             "later. Text only — it stays on this PC and is never uploaded."),
            ("personal_vocab_enabled", "Learn your words",
             "Uses your edits to recognize names, brands, and jargon better "
             "(e.g. \"Big Air\", \"OpenVerba\")."),
            ("corrections_enabled", "Auto-fix learned mistakes",
             "Applies the corrections you've saved before pasting, so a word you "
             "fixed once is fixed every time."),
        ]
        for i, (key, label, hint) in enumerate(toggles):
            self._toggle_row(inner, i, key, label, hint)

        btns = ctk.CTkFrame(inner, fg_color="transparent")
        btns.grid(row=len(toggles), column=0, sticky="w", pady=(12, 0))
        W.secondary_button(btns, "View history", width=150, height=34,
                           command=self.app.show_history).grid(
            row=0, column=0, padx=(0, 8))
        W.danger_button(btns, "Clear history", width=150,
                        command=self._clear_history).grid(row=0, column=1,
                                                          padx=(0, 8))
        W.danger_button(btns, "Clear learned words", width=180,
                        command=self._clear_learned).grid(row=0, column=2)
        return card

    def _clear_history(self):
        if not self._confirm("Clear history?",
                             "Delete all saved transcripts from this PC? This "
                             "can't be undone. (Your learned words are kept.)"):
            return
        try:
            vf_history.clear()
            self._toast_msg("History cleared.", T.OK)
        except Exception as exc:
            self._toast_msg("Could not clear history: %s" % exc, T.DANGER)

    def _clear_learned(self):
        if not self._confirm("Clear learned words?",
                             "Forget every learned word and correction? "
                             "OpenVerba goes back to default recognition. This "
                             "can't be undone."):
            return
        ok = True
        for p in (CORRECTIONS_PATH, PERSONAL_VOCAB_PATH):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except Exception:
                ok = False
        # Drop them from the live engine immediately.
        try:
            self.app.on_learned()
        except Exception:
            pass
        self._toast_msg("Learned words cleared." if ok else
                        "Cleared (some files could not be removed).",
                        T.OK if ok else T.WARN)

    # ---------------------------------------------------------- system card
    def _build_system(self, master):
        card = W.Card(master, title="System")
        card.grid_columnconfigure(0, weight=1)
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 16))
        inner.grid_columnconfigure(0, weight=1)

        # GPU runtime status + enable button. The gpu_runtime_present() scan is
        # session-cached now, but render a placeholder and fill it off-thread so
        # the very first Settings open never blocks (B.1).
        self._gpu_inner = inner
        self._gpu_row = W.StatRow(inner, "GPU runtime", "Checking…",
                                  value_color=T.TEXT_MUTED)
        self._gpu_row.grid(row=0, column=0, sticky="ew", pady=4)
        # Holder for the conditional "Enable GPU acceleration" button + hint,
        # added by _apply_gpu_probe once the scan resolves.
        self._gpu_enable_holder = ctk.CTkFrame(inner, fg_color="transparent")
        self._gpu_enable_holder.grid(row=1, column=0, sticky="ew")
        self._gpu_enable_holder.grid_columnconfigure(0, weight=1)

        btns = ctk.CTkFrame(inner, fg_color="transparent")
        btns.grid(row=3, column=0, sticky="w", pady=(12, 0))
        W.secondary_button(btns, "Open data folder", width=160, height=34,
                           command=lambda: self._open_path(DATA_DIR)).grid(
            row=0, column=0, padx=(0, 8))
        W.secondary_button(btns, "Open log", width=130, height=34,
                           command=lambda: self._open_path(LOG_PATH)).grid(
            row=0, column=1, padx=(0, 8))
        W.secondary_button(btns, "Open recordings", width=160, height=34,
                           command=lambda: self._open_path(RECORDINGS_DIR)).grid(
            row=0, column=2)
        return card

    def _apply_gpu_probe(self, present):
        text, color_key, show_enable = _gpu_status(present)
        try:
            self._gpu_row.set_value(text, self._color(color_key))
        except Exception:
            pass
        if not show_enable:
            return
        try:
            W.secondary_button(
                self._gpu_enable_holder, "Enable GPU acceleration",
                width=210, height=34, command=self._enable_gpu).grid(
                row=0, column=0, sticky="w", pady=(2, 8))
            W.hint_label(self._gpu_enable_holder,
                         "Installs the NVIDIA CUDA libraries (a few hundred MB). "
                         "Restart OpenVerba afterward.",
                         color=T.TEXT_FAINT, wraplength=560).grid(
                row=1, column=0, sticky="w")
        except Exception:
            pass

    def _enable_gpu(self):
        dlg = _GpuInstallDialog(self)
        dlg.show()
        # Refresh the system section by rebuilding settings (cheap).
        self.app.show_settings()

    def _open_path(self, path):
        try:
            if os.path.isdir(path):
                os.startfile(path)  # noqa: P204 (Windows shell open)
            elif os.path.isfile(path):
                os.startfile(os.path.dirname(path))
            else:
                os.startfile(DATA_DIR)
        except Exception as exc:
            self._toast_msg("Could not open folder: %s" % exc, T.DANGER)

    # ----------------------------------------------------------- about card
    # --------------------------------------------------------- updates card
    def _build_updates(self, master):
        card = W.Card(master, title="Updates")
        card.grid_columnconfigure(0, weight=1)
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 16))
        inner.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(inner, text="OpenVerba %s" % __version__,
                     font=T.font("body_bold"), text_color=T.TEXT,
                     anchor="w").grid(row=0, column=0, sticky="w")
        W.hint_label(inner, "Check openverba.com for a newer version. Updates "
                            "are downloaded and verified (SHA-256), then the "
                            "installer runs and OpenVerba restarts.",
                     color=T.TEXT_MUTED, wraplength=560).grid(
            row=1, column=0, sticky="w", pady=(1, 0))

        # Auto-check toggle.
        self._upd_var = ctk.BooleanVar(value=bool(self.cfg.get("auto_update_check")))
        ctk.CTkSwitch(
            inner, text="", variable=self._upd_var, onvalue=True, offvalue=False,
            progress_color=T.ACCENT, command=self._on_autoupdate_toggle
        ).grid(row=0, column=1, rowspan=2, sticky="e", padx=(12, 0))

        self._upd_btn = W.secondary_button(inner, "Check now", width=140,
                                           command=self._check_updates)
        self._upd_btn.grid(row=2, column=0, sticky="w", pady=(12, 0))

        self._upd_prog = ctk.CTkProgressBar(inner, height=8, corner_radius=4,
                                            progress_color=T.ACCENT,
                                            fg_color=T.SURFACE_3)
        self._upd_prog.set(0)
        return card

    def _on_autoupdate_toggle(self):
        self.cfg["auto_update_check"] = bool(self._upd_var.get())
        vf_config.save_config(self.cfg)
        self._toast_msg("Saved.", T.OK)

    def _check_updates(self):
        if self._busy:
            self._toast_msg("Please wait for the current operation to finish.",
                            T.WARN)
            return
        self._busy = True
        self._upd_btn.configure(state="disabled")
        self._toast_msg("Checking for updates…", T.ACCENT)

        def work():
            res = vf_updater.check_for_updates(self.cfg, interactive=True)
            self.after(0, lambda: self._on_check_result(res))
        threading.Thread(target=work, daemon=True).start()

    def _on_check_result(self, res):
        if res.status == "error":
            self._busy = False
            self._upd_btn.configure(state="normal")
            self._toast_msg("Update check failed: %s" % res.error, T.DANGER)
        elif res.available:
            self._toast_msg("Version %s available — downloading…" % res.version,
                            T.ACCENT)
            self._upd_prog.grid(row=3, column=0, columnspan=2, sticky="ew",
                                pady=(8, 0))
            info = res.info

            def prog(frac, done, total):
                self.after(0, lambda: self._upd_prog.set(
                    max(0.0, min(1.0, frac))))

            def dl():
                path = vf_updater.download(info.url, info.sha256, progress_cb=prog)
                self.after(0, lambda: self._on_download_done(path))
            threading.Thread(target=dl, daemon=True).start()
        else:
            self._busy = False
            self._upd_btn.configure(state="normal")
            self._toast_msg("You're on the latest version (%s)." % res.current,
                            T.OK)

    def _on_download_done(self, path):
        self._busy = False
        self._upd_btn.configure(state="normal")
        if not path:
            self._toast_msg("Update download failed or did not verify.",
                            T.DANGER)
            return
        self._toast_msg("Starting the installer… OpenVerba will restart.", T.OK)
        # Hands off to Inno and exits this process so file locks release.
        vf_updater.launch_installer_and_exit(path)

    # ---------------------------------------------------- help & feedback card
    def _build_help(self, master):
        """Help & feedback: a short line + a "Report a bug" button that opens the
        bug-report dialog."""
        card = W.Card(master, title="Help & feedback")
        card.grid_columnconfigure(0, weight=1)
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 16))
        inner.grid_columnconfigure(0, weight=1)
        W.hint_label(
            inner,
            "Something not working? Send us a bug report — it helps us fix "
            "OpenVerba faster. You can include diagnostics (no audio or text).",
            color=T.TEXT_MUTED, wraplength=560).grid(row=0, column=0, sticky="w")
        W.secondary_button(inner, "Report a bug", width=160, height=36,
                           command=self._open_bug_report).grid(
                               row=1, column=0, sticky="w", pady=(12, 0))
        return card

    def _open_bug_report(self):
        """Open the bug-report dialog and toast the outcome."""
        status = _BugReportDialog(self, self.cfg).show()
        email = self.cfg.get("bug_report_email") or "the developer"
        if status == "sent":
            self._toast_msg("Thanks — your report was sent.", T.OK)
        elif status == "mailto":
            self._toast_msg("Opened your email app — just hit send.", T.OK)
        elif status == "failed":
            self._toast_msg("Couldn't send; please email %s." % email, T.DANGER)
        # status is None when the dialog was cancelled -> no toast.

    def _build_about(self, master):
        card = W.Card(master, title="About")
        card.grid_columnconfigure(0, weight=1)
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 16))
        inner.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(inner, image=W.make_ctk_icon(48), text="").grid(
            row=0, column=0, rowspan=2, padx=(0, 14))
        ctk.CTkLabel(inner, text="%s %s" % (APP_DISPLAY_NAME, __version__),
                     font=T.font("h3"), text_color=T.TEXT, anchor="w").grid(
            row=0, column=1, sticky="w")
        W.hint_label(
            inner, "Free, private, offline voice typing for Windows. "
                   "Everything runs locally — your audio never leaves your PC.",
            color=T.TEXT_MUTED, wraplength=560).grid(
            row=1, column=1, sticky="w", pady=(2, 0))
        return card

    # --------------------------------------------------------------- helpers
    def _toast_msg(self, text, color=T.ACCENT):
        self._toast.configure(text=text, text_color=color)

    def _confirm(self, title, message):
        return _ConfirmDialog(self, title, message).show()


# ---------------------------------------------------------------------------
# A tiny confirm dialog (customtkinter has no built-in styled one).
# ---------------------------------------------------------------------------
class _ConfirmDialog(ctk.CTkToplevel):
    def __init__(self, master, title, message):
        super().__init__(master)
        self.title(title)
        self.configure(fg_color=T.BG)
        self.geometry("420x200")
        self.resizable(False, False)
        self._result = False
        self.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(self, text=title, font=T.font("h2"),
                     text_color=T.TEXT).grid(row=0, column=0, padx=24,
                                             pady=(22, 6), sticky="w")
        W.hint_label(self, message, color=T.TEXT_MUTED, wraplength=370).grid(
            row=1, column=0, padx=24, sticky="w")
        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.grid(row=2, column=0, sticky="e", padx=24, pady=20)
        W.secondary_button(btns, "Cancel", width=100,
                           command=self._cancel).grid(row=0, column=0, padx=6)
        W.accent_button(btns, "Confirm", width=110,
                        command=self._ok).grid(row=0, column=1, padx=6)
        self.transient(master)
        self.after(10, self._center)
        self.protocol("WM_DELETE_WINDOW", self._cancel)

    def _center(self):
        try:
            self.update_idletasks()
            m = self.master
            x = m.winfo_rootx() + (m.winfo_width() - self.winfo_width()) // 2
            y = m.winfo_rooty() + (m.winfo_height() - self.winfo_height()) // 2
            self.geometry("+%d+%d" % (max(0, x), max(0, y)))
            self.grab_set()
        except Exception:
            pass

    def _ok(self):
        self._result = True
        self._close()

    def _cancel(self):
        self._result = False
        self._close()

    def _close(self):
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()

    def show(self):
        self.wait_window()
        return self._result


# ---------------------------------------------------------------------------
# Report-a-bug dialog (Settings -> Help & feedback -> Report a bug).
# Collects a short message + optional diagnostics + optional reply email and
# hands off to voiceflow.bugreport.report() on a background thread.
# ---------------------------------------------------------------------------
class _BugReportDialog(ctk.CTkToplevel):
    """Modal bug-report dialog. show() returns the delivery status string
    ("sent" | "mailto" | "failed") after a send, or None if cancelled."""

    def __init__(self, master, cfg):
        super().__init__(master)
        self.title("Report a bug")
        self.configure(fg_color=T.BG)
        self.geometry("520x460")
        self.resizable(False, True)
        self._cfg = cfg or {}
        self._result = None
        self._sending = False
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        ctk.CTkLabel(self, text="What went wrong?", font=T.font("h2"),
                     text_color=T.TEXT).grid(row=0, column=0, sticky="w",
                                             padx=24, pady=(20, 2))
        W.hint_label(
            self,
            "Describe the problem and what you were doing when it happened.",
            color=T.TEXT_MUTED, wraplength=460).grid(row=1, column=0, sticky="w",
                                                     padx=24, pady=(0, 6))

        self._msg_box = ctk.CTkTextbox(self, font=T.font("body"),
                                       fg_color=T.SURFACE_2, text_color=T.TEXT,
                                       border_width=1, border_color=T.BORDER,
                                       height=140)
        self._msg_box.grid(row=2, column=0, sticky="nsew", padx=24, pady=(0, 10))

        self._include_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            self, text="Include diagnostics (app version, OS, model, recent "
                       "log — no audio or text)",
            variable=self._include_var, onvalue=True, offvalue=False,
            font=T.font("small"), text_color=T.TEXT_MUTED,
            fg_color=T.ACCENT, hover_color=T.ACCENT_DARK).grid(
                row=3, column=0, sticky="w", padx=24, pady=(0, 8))

        ctk.CTkLabel(self, text="Your email (so we can follow up) — optional",
                     font=T.font("body_bold"), text_color=T.TEXT).grid(
            row=4, column=0, sticky="w", padx=24, pady=(0, 2))
        self._email_var = ctk.StringVar(value="")
        ctk.CTkEntry(self, textvariable=self._email_var,
                     placeholder_text="you@example.com").grid(
            row=5, column=0, sticky="ew", padx=24, pady=(0, 8))

        W.hint_label(
            self,
            "This sends your message + optional diagnostics to the developer "
            "over the internet.",
            color=T.TEXT_FAINT, wraplength=460).grid(row=6, column=0, sticky="w",
                                                     padx=24, pady=(0, 6))

        self._status = W.hint_label(self, "", color=T.ACCENT, wraplength=460)
        self._status.grid(row=7, column=0, sticky="w", padx=24)

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.grid(row=8, column=0, sticky="e", padx=24, pady=(10, 18))
        self._cancel_btn = W.secondary_button(btns, "Cancel", width=100,
                                              command=self._cancel)
        self._cancel_btn.grid(row=0, column=0, padx=6)
        self._send_btn = W.accent_button(btns, "Send", width=110,
                                         command=self._send)
        self._send_btn.grid(row=0, column=1, padx=6)

        self.transient(master)
        self.after(10, self._center)
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.after(60, self._msg_box.focus_set)

    def _center(self):
        try:
            self.update_idletasks()
            m = self.master
            x = m.winfo_rootx() + (m.winfo_width() - self.winfo_width()) // 2
            y = m.winfo_rooty() + (m.winfo_height() - self.winfo_height()) // 2
            self.geometry("+%d+%d" % (max(0, x), max(0, y)))
            self.grab_set()
        except Exception:
            pass

    def _send(self):
        if self._sending:
            return
        message = self._msg_box.get("1.0", "end").strip()
        if not message:
            self._status.configure(
                text="Please describe the problem first.", text_color=T.WARN)
            return
        self._sending = True
        self._send_btn.configure(state="disabled")
        self._status.configure(text="Sending…", text_color=T.ACCENT)
        include = bool(self._include_var.get())
        email = self._email_var.get().strip()

        def work():
            try:
                status, detail = vf_bugreport.report(
                    message, include, email, self._cfg)
            except Exception as exc:
                status, detail = "failed", str(exc)
            self.after(0, lambda: self._done(status, detail))
        threading.Thread(target=work, daemon=True).start()

    def _done(self, status, detail):
        self._result = status
        # Close on success/mailto; on failure leave the dialog open so the user
        # can retry or copy their message.
        if status in ("sent", "mailto"):
            self._close()
        else:
            self._sending = False
            try:
                self._send_btn.configure(state="normal")
            except Exception:
                pass
            self._status.configure(
                text="Couldn't send. Please try again or email us directly.",
                text_color=T.DANGER)

    def _cancel(self):
        if self._sending:
            return
        self._result = None
        self._close()

    def _close(self):
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()

    def show(self):
        self.wait_window()
        return self._result


# ---------------------------------------------------------------------------
# Add/edit a voice snippet (trigger -> expansion).
# ---------------------------------------------------------------------------
class _SnippetDialog(ctk.CTkToplevel):
    def __init__(self, master, snippet, edit=False):
        super().__init__(master)
        self.title("Edit snippet" if edit else "Add snippet")
        self.configure(fg_color=T.BG)
        self.geometry("440x280")
        self.resizable(False, False)
        self._result = None
        self.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(self, text="Trigger (what you say)",
                     font=T.font("body_bold"), text_color=T.TEXT).grid(
            row=0, column=0, padx=24, pady=(22, 2), sticky="w")
        self._trigger_var = ctk.StringVar(
            value=str((snippet or {}).get("trigger", "")))
        trig_entry = ctk.CTkEntry(self, textvariable=self._trigger_var,
                                  placeholder_text="e.g. brb")
        trig_entry.grid(row=1, column=0, padx=24, pady=(0, 12), sticky="ew")

        ctk.CTkLabel(self, text="Expansion (what you get)",
                     font=T.font("body_bold"), text_color=T.TEXT).grid(
            row=2, column=0, padx=24, pady=(0, 2), sticky="w")
        self._expansion_var = ctk.StringVar(
            value=str((snippet or {}).get("expansion", "")))
        exp_entry = ctk.CTkEntry(self, textvariable=self._expansion_var,
                                 placeholder_text="e.g. be right back")
        exp_entry.grid(row=3, column=0, padx=24, pady=(0, 8), sticky="ew")

        self._warn = W.hint_label(self, "", color=T.WARN, wraplength=380)
        self._warn.grid(row=4, column=0, padx=24, sticky="w")

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.grid(row=5, column=0, sticky="e", padx=24, pady=(12, 18))
        W.secondary_button(btns, "Cancel", width=100,
                           command=self._cancel).grid(row=0, column=0, padx=6)
        W.accent_button(btns, "Save", width=110,
                        command=self._save).grid(row=0, column=1, padx=6)

        self.transient(master)
        self.after(10, self._center)
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.after(60, trig_entry.focus_set)

    def _center(self):
        try:
            self.update_idletasks()
            m = self.master
            x = m.winfo_rootx() + (m.winfo_width() - self.winfo_width()) // 2
            y = m.winfo_rooty() + (m.winfo_height() - self.winfo_height()) // 2
            self.geometry("+%d+%d" % (max(0, x), max(0, y)))
            self.grab_set()
        except Exception:
            pass

    def _save(self):
        trigger = self._trigger_var.get().strip()
        expansion = self._expansion_var.get().strip()
        if not trigger or not expansion:
            self._warn.configure(
                text="Trigger and expansion can't be empty.")
            return
        self._result = (trigger, expansion)
        self._close()

    def _cancel(self):
        self._result = None
        self._close()

    def _close(self):
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()

    def show(self):
        self.wait_window()
        return self._result


# ---------------------------------------------------------------------------
# Per-app modes manager (list / add / edit / delete) + a per-mode edit dialog.
# ---------------------------------------------------------------------------
class _ModesDialog(ctk.CTkToplevel):
    """Modal dialog to list, add, edit, and delete per-app context modes.

    show() returns True if the modes were changed (so the caller can reload the
    engine), else False. All persistence goes through voiceflow.modes (atomic
    + defensive); the "Default" mode is protected from deletion."""

    def __init__(self, master):
        super().__init__(master)
        self.title("Manage modes")
        self.configure(fg_color=T.BG)
        self.geometry("680x520")
        self.minsize(560, 420)
        self._changed = False
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        # Load current modes (seeds builtins on first use; never raises).
        self._path = vf_modes.modes_path()
        try:
            self._modes = vf_modes.load_modes(self._path)
        except Exception:
            self._modes = []

        ctk.CTkLabel(self, text="Per-app modes", font=T.font("h2"),
                     text_color=T.TEXT).grid(row=0, column=0, sticky="w",
                                             padx=22, pady=(20, 2))
        W.hint_label(
            self,
            "Each mode maps a list of app executables (e.g. slack.exe) to a "
            "biasing prompt. The first enabled mode that matches the active app "
            "wins; \"Default\" (empty app list) is the catch-all.",
            color=T.TEXT_MUTED, wraplength=600).grid(
                row=1, column=0, sticky="w", padx=22)

        self._scroll = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self._scroll.grid(row=2, column=0, sticky="nsew", padx=16, pady=12)
        self._scroll.grid_columnconfigure(0, weight=1)

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.grid(row=3, column=0, sticky="ew", padx=22, pady=(0, 16))
        W.secondary_button(btns, "Add mode", width=130, height=34,
                           command=self._add).grid(row=0, column=0, padx=(0, 8))
        W.accent_button(btns, "Done", width=110,
                        command=self._close).grid(row=0, column=1)

        self._refresh()
        self.transient(master)
        self.after(10, self._center)
        self.protocol("WM_DELETE_WINDOW", self._close)

    def _refresh(self):
        for w in self._scroll.winfo_children():
            try:
                w.destroy()
            except Exception:
                pass
        if not self._modes:
            ctk.CTkLabel(self._scroll, text="No modes.", font=T.font("small"),
                         text_color=T.TEXT_FAINT, anchor="w").grid(
                             row=0, column=0, sticky="w", pady=4)
            return
        for i, m in enumerate(self._modes):
            row = ctk.CTkFrame(self._scroll, fg_color=T.SURFACE_2,
                               corner_radius=T.RADIUS_SM)
            row.grid(row=i, column=0, sticky="ew", pady=4, padx=2)
            row.grid_columnconfigure(0, weight=1)
            left = ctk.CTkFrame(row, fg_color="transparent")
            left.grid(row=0, column=0, sticky="ew", padx=12, pady=8)
            left.grid_columnconfigure(0, weight=1)
            name = str(m.get("name", "?"))
            on = bool(m.get("enabled", True))
            title = name + ("" if on else "  (off)")
            ctk.CTkLabel(left, text=title, font=T.font("body_bold"),
                         text_color=T.TEXT if on else T.TEXT_MUTED,
                         anchor="w").grid(row=0, column=0, sticky="w")
            apps = m.get("apps") or []
            apps_s = ", ".join(apps) if apps else "any app (catch-all)"
            W.hint_label(left, apps_s, color=T.TEXT_MUTED,
                         wraplength=420).grid(row=1, column=0, sticky="w",
                                              pady=(2, 0))
            actions = ctk.CTkFrame(row, fg_color="transparent")
            actions.grid(row=0, column=1, padx=(0, 10))
            W.ghost_button(actions, "Edit", width=56, height=28,
                           command=lambda idx=i: self._edit(idx)).grid(
                               row=0, column=0, padx=(0, 4))
            is_default = (name == "Default")
            del_btn = W.danger_button(
                actions, "Delete", width=72, height=28,
                command=lambda idx=i: self._delete(idx))
            del_btn.grid(row=0, column=1)
            if is_default:
                # The Default catch-all can't be deleted.
                try:
                    del_btn.configure(state="disabled")
                except Exception:
                    pass

    def _add(self):
        result = _ModeEditDialog(self, None).show()
        if result is not None:
            self._modes.append(result)
            self._save()

    def _edit(self, index):
        if not (0 <= index < len(self._modes)):
            return
        result = _ModeEditDialog(self, self._modes[index]).show()
        if result is not None:
            self._modes[index] = result
            self._save()

    def _delete(self, index):
        if not (0 <= index < len(self._modes)):
            return
        if self._modes[index].get("name") == "Default":
            return  # protected catch-all
        if _ConfirmDialog(self, "Delete this mode?",
                          "Remove this mode? You can add it back later.").show():
            del self._modes[index]
            self._save()

    def _save(self):
        try:
            vf_modes.save_modes(self._path, self._modes)
            self._changed = True
        except Exception:
            pass
        self._refresh()

    def _center(self):
        try:
            self.update_idletasks()
            m = self.master
            x = m.winfo_rootx() + (m.winfo_width() - self.winfo_width()) // 2
            y = m.winfo_rooty() + (m.winfo_height() - self.winfo_height()) // 2
            self.geometry("+%d+%d" % (max(0, x), max(0, y)))
            self.grab_set()
        except Exception:
            pass

    def _close(self):
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()

    def show(self):
        self.wait_window()
        return self._changed


class _ModeEditDialog(ctk.CTkToplevel):
    """Add/edit a single mode. show() returns the new mode dict, or None on
    cancel. ``mode`` is the existing dict (edit) or None (add)."""

    _TONES = ["neutral", "formal", "casual", "code-aware"]

    def __init__(self, master, mode):
        super().__init__(master)
        editing = mode is not None
        self.title("Edit mode" if editing else "Add mode")
        self.configure(fg_color=T.BG)
        self.geometry("520x520")
        self.resizable(False, True)
        self._result = None
        self._editing = editing
        self._orig_name = str((mode or {}).get("name", "")) if editing else ""
        self.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(self, text="Name", font=T.font("body_bold"),
                     text_color=T.TEXT).grid(row=0, column=0, padx=24,
                                             pady=(20, 2), sticky="w")
        self._name_var = ctk.StringVar(value=str((mode or {}).get("name", "")))
        name_entry = ctk.CTkEntry(self, textvariable=self._name_var,
                                  placeholder_text="e.g. Email")
        name_entry.grid(row=1, column=0, padx=24, pady=(0, 10), sticky="ew")
        # The Default catch-all keeps its name (it's the protected fallback).
        if editing and self._orig_name == "Default":
            try:
                name_entry.configure(state="disabled")
            except Exception:
                pass

        ctk.CTkLabel(self, text="Apps (comma-separated exe names; blank = any)",
                     font=T.font("body_bold"), text_color=T.TEXT).grid(
                         row=2, column=0, padx=24, pady=(0, 2), sticky="w")
        self._apps_var = ctk.StringVar(
            value=", ".join((mode or {}).get("apps", []) or []))
        ctk.CTkEntry(self, textvariable=self._apps_var,
                     placeholder_text="e.g. slack.exe, discord.exe").grid(
                         row=3, column=0, padx=24, pady=(0, 10), sticky="ew")

        ctk.CTkLabel(self, text="Prompt (how to format dictation in this app)",
                     font=T.font("body_bold"), text_color=T.TEXT).grid(
                         row=4, column=0, padx=24, pady=(0, 2), sticky="w")
        self._prompt_box = ctk.CTkTextbox(self, height=120,
                                          fg_color=T.SURFACE_2,
                                          text_color=T.TEXT, border_width=1,
                                          border_color=T.BORDER)
        self._prompt_box.grid(row=5, column=0, padx=24, pady=(0, 10),
                              sticky="ew")
        self._prompt_box.insert("1.0", str((mode or {}).get("prompt", "")))

        row6 = ctk.CTkFrame(self, fg_color="transparent")
        row6.grid(row=6, column=0, padx=24, pady=(0, 8), sticky="ew")
        ctk.CTkLabel(row6, text="Tone", font=T.font("body_bold"),
                     text_color=T.TEXT).grid(row=0, column=0, sticky="w")
        self._tone_var = ctk.StringVar(
            value=str((mode or {}).get("tone", "neutral")))
        ctk.CTkOptionMenu(row6, values=self._TONES, variable=self._tone_var,
                          font=T.font("body")).grid(row=0, column=1,
                                                    sticky="w", padx=(12, 0))
        self._enabled_var = ctk.BooleanVar(
            value=bool((mode or {}).get("enabled", True)))
        ctk.CTkSwitch(row6, text="Enabled", variable=self._enabled_var,
                      onvalue=True, offvalue=False, progress_color=T.ACCENT,
                      button_color=T.TEXT, button_hover_color=T.TEXT).grid(
                          row=0, column=2, sticky="e", padx=(24, 0))

        self._warn = W.hint_label(self, "", color=T.WARN, wraplength=440)
        self._warn.grid(row=7, column=0, padx=24, sticky="w")

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.grid(row=8, column=0, sticky="e", padx=24, pady=(12, 18))
        W.secondary_button(btns, "Cancel", width=100,
                           command=self._cancel).grid(row=0, column=0, padx=6)
        W.accent_button(btns, "Save", width=110,
                        command=self._save).grid(row=0, column=1, padx=6)

        self.transient(master)
        self.after(10, self._center)
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.after(60, name_entry.focus_set)

    def _center(self):
        try:
            self.update_idletasks()
            m = self.master
            x = m.winfo_rootx() + (m.winfo_width() - self.winfo_width()) // 2
            y = m.winfo_rooty() + (m.winfo_height() - self.winfo_height()) // 2
            self.geometry("+%d+%d" % (max(0, x), max(0, y)))
            self.grab_set()
        except Exception:
            pass

    def _save(self):
        # Default keeps its name; otherwise read the (possibly edited) name.
        if self._editing and self._orig_name == "Default":
            name = "Default"
        else:
            name = self._name_var.get().strip()
        prompt = self._prompt_box.get("1.0", "end").strip()
        if not name:
            self._warn.configure(text="Name can't be empty.")
            return
        if not prompt:
            self._warn.configure(text="Prompt can't be empty.")
            return
        apps = [a.strip().lower() for a in self._apps_var.get().split(",")
                if a.strip()]
        self._result = {
            "name": name,
            "enabled": bool(self._enabled_var.get()),
            "apps": apps,
            "prompt": prompt,
            "tone": self._tone_var.get() or "neutral",
        }
        self._close()

    def _cancel(self):
        self._result = None
        self._close()

    def _close(self):
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()

    def show(self):
        self.wait_window()
        return self._result


# ---------------------------------------------------------------------------
# GPU runtime install dialog (Settings -> Enable GPU acceleration).
# ---------------------------------------------------------------------------
class _GpuInstallDialog(ctk.CTkToplevel):
    def __init__(self, master):
        super().__init__(master)
        self.title("Enable GPU acceleration")
        self.configure(fg_color=T.BG)
        self.geometry("560x420")
        self.resizable(False, True)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)
        self._done = False

        ctk.CTkLabel(self, text="Installing GPU runtime", font=T.font("h2"),
                     text_color=T.TEXT).grid(row=0, column=0, sticky="w",
                                             padx=22, pady=(20, 4))
        W.hint_label(self, "Downloading the NVIDIA CUDA libraries (cuBLAS + "
                           "cuDNN). Restart OpenVerba afterward to use the GPU.",
                     color=T.TEXT_MUTED, wraplength=500).grid(
            row=1, column=0, sticky="w", padx=22)
        self._log = ctk.CTkTextbox(self, font=T.font("mono"),
                                   fg_color=T.SURFACE_2, text_color=T.TEXT_MUTED,
                                   border_width=1, border_color=T.BORDER)
        self._log.grid(row=2, column=0, sticky="nsew", padx=22, pady=12)
        self._log.configure(state="disabled")
        self._bar = ctk.CTkProgressBar(self, mode="indeterminate",
                                       progress_color=T.ACCENT,
                                       fg_color=T.SURFACE_2)
        self._bar.grid(row=3, column=0, sticky="ew", padx=22, pady=(0, 8))
        self._close_btn = W.secondary_button(self, "Close", width=110,
                                             command=self._close)
        self._close_btn.grid(row=4, column=0, sticky="e", padx=22, pady=(0, 16))
        self._close_btn.configure(state="disabled")

        self.transient(master)
        self.after(10, self._center)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self._bar.start()
        threading.Thread(target=self._worker, daemon=True).start()

    def _center(self):
        try:
            self.update_idletasks()
            m = self.master
            x = m.winfo_rootx() + (m.winfo_width() - self.winfo_width()) // 2
            y = m.winfo_rooty() + (m.winfo_height() - self.winfo_height()) // 2
            self.geometry("+%d+%d" % (max(0, x), max(0, y)))
            self.grab_set()
        except Exception:
            pass

    def _worker(self):
        def emit(line):
            self.after(0, lambda: self._append(line))
        try:
            ok, msg = cuda.install_gpu_runtime(progress_cb=emit)
        except Exception as exc:
            ok, msg = False, "Install failed: %s" % exc
        self.after(0, lambda: self._finish(ok, msg))

    def _append(self, line):
        try:
            self._log.configure(state="normal")
            self._log.insert("end", line + "\n")
            self._log.see("end")
            self._log.configure(state="disabled")
        except Exception:
            pass

    def _finish(self, ok, msg):
        try:
            self._bar.stop()
            self._bar.configure(mode="determinate")
            self._bar.set(1.0 if ok else 0.0)
            self._bar.configure(progress_color=T.OK if ok else T.DANGER)
        except Exception:
            pass
        self._append(("\n✓ " if ok else "\n✗ ") + msg)
        self._close_btn.configure(state="normal")
        self._done = True

    def _close(self):
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()

    def show(self):
        self.wait_window()
        return self._done


# ---------------------------------------------------------------------------
# Smart-AI setup dialog (Settings -> Enable smart AI editing): installs Ollama
# if needed and pulls the recommended local model, with progress.
# ---------------------------------------------------------------------------
class _AiSetupDialog(ctk.CTkToplevel):
    def __init__(self, master, model):
        super().__init__(master)
        self.title("Enable smart AI editing")
        self.configure(fg_color=T.BG)
        self.geometry("580x440")
        self.resizable(False, True)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)
        self._model = model
        self._done = False

        ctk.CTkLabel(self, text="Setting up local AI", font=T.font("h2"),
                     text_color=T.TEXT).grid(row=0, column=0, sticky="w",
                                             padx=22, pady=(20, 4))
        W.hint_label(
            self, "Installing Ollama (if needed) and downloading the '%s' model. "
                  "This is a one-time download (~1-2 GB) and runs entirely on "
                  "your PC -- free and private." % model,
            color=T.TEXT_MUTED, wraplength=520).grid(row=1, column=0, sticky="w",
                                                     padx=22)
        self._log = ctk.CTkTextbox(self, font=T.font("mono"),
                                   fg_color=T.SURFACE_2, text_color=T.TEXT_MUTED,
                                   border_width=1, border_color=T.BORDER)
        self._log.grid(row=2, column=0, sticky="nsew", padx=22, pady=12)
        self._log.configure(state="disabled")
        self._bar = ctk.CTkProgressBar(self, mode="indeterminate",
                                       progress_color=T.ACCENT,
                                       fg_color=T.SURFACE_2)
        self._bar.grid(row=3, column=0, sticky="ew", padx=22, pady=(0, 8))
        self._close_btn = W.secondary_button(self, "Close", width=110,
                                             command=self._close)
        self._close_btn.grid(row=4, column=0, sticky="e", padx=22, pady=(0, 16))
        self._close_btn.configure(state="disabled")

        self.transient(master)
        self.after(10, self._center)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self._bar.start()
        threading.Thread(target=self._worker, daemon=True).start()

    def _center(self):
        try:
            self.update_idletasks()
            m = self.master
            x = m.winfo_rootx() + (m.winfo_width() - self.winfo_width()) // 2
            y = m.winfo_rooty() + (m.winfo_height() - self.winfo_height()) // 2
            self.geometry("+%d+%d" % (max(0, x), max(0, y)))
            self.grab_set()
        except Exception:
            pass

    def _worker(self):
        def emit(line):
            self.after(0, lambda: self._append(line))
        try:
            ok, msg = vf_ai_setup.setup(self._model, progress_cb=emit)
        except Exception as exc:
            ok, msg = False, "Setup failed: %s" % exc
        self.after(0, lambda: self._finish(ok, msg))

    def _append(self, line):
        try:
            self._log.configure(state="normal")
            self._log.insert("end", line + "\n")
            self._log.see("end")
            self._log.configure(state="disabled")
        except Exception:
            pass

    def _finish(self, ok, msg):
        try:
            self._bar.stop()
            self._bar.configure(mode="determinate")
            self._bar.set(1.0 if ok else 0.0)
            self._bar.configure(progress_color=T.OK if ok else T.DANGER)
        except Exception:
            pass
        self._append(("\n✓ " if ok else "\n✗ ") + msg)
        self._close_btn.configure(state="normal")
        self._done = ok

    def _close(self):
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()

    def show(self):
        self.wait_window()
        return self._done
