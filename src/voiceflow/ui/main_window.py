"""
gui.main_window - the VoiceFlow application window + launch() entry point.

Responsibilities:
  * Own the customtkinter root window, the config dict, and the DictationEngine.
  * Swap between the Onboarding flow (first run), the Dashboard, and Settings.
  * Run all engine work (model load, switch+reload) on background threads, and
    marshal every engine callback (state/level/transcript/log) back onto the UI
    thread via .after (the engine's callbacks fire on background threads).
  * System tray (Open/Pause/Quit); minimize-and-close-to-tray with a first-time
    hint.
  * The Trigger picker dialog, applied live via engine.set_trigger.

app.py calls launch() for GUI mode. The GUI runs its OWN in-process engine; it
does not take the background single-instance mutex (so you can open the window
while a background runtime runs — but you wouldn't normally run both).
"""

from __future__ import annotations

import logging
import threading
import traceback

import customtkinter as ctk

from . import theme as T
from . import widgets as W
from .onboarding import OnboardingView
from .dashboard import DashboardView
from .settings import SettingsView
from .trigger_picker import TriggerPicker
from .tray import GuiTray

from voiceflow import config as vf_config
from voiceflow.constants import APP_DISPLAY_NAME

log = logging.getLogger("voiceflow.gui")


def _set_app_user_model_id():
    """Give the process its own Windows taskbar identity so the taskbar shows the
    OpenVerba icon (and groups under OpenVerba) instead of pythonw/Python. MUST be
    called before the first window is created. No-op off Windows / on error."""
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "OpenVerba.OpenVerba.App.1")
    except Exception:
        pass


class VoiceFlowApp(ctk.CTk):
    def __init__(self):
        _set_app_user_model_id()   # taskbar identity BEFORE the window exists
        super().__init__()
        self.title(APP_DISPLAY_NAME)
        self.configure(fg_color=T.BG)
        self.geometry("960x640")
        self.minsize(840, 600)

        # Window/app icon.
        self._icon_img = None
        self._apply_window_icon()

        # State.
        self.cfg = vf_config.load_config()
        self.engine = None
        self._engine_lock = threading.Lock()
        self._model_loaded = False
        self._closing_hint_shown = False
        self._armed = True

        self.dashboard = None
        self.settings_view = None
        self.history_view = None
        self.file_transcriber_view = None
        self._current_view = None

        # Live-preview floating bar (preview mode). Created lazily on first show,
        # reused for the app lifetime, destroyed on quit.
        self._overlay = None

        # Single content area; views are frames swapped into it.
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._content = ctk.CTkFrame(self, fg_color=T.BG)
        self._content.grid(row=0, column=0, sticky="nsew")
        self._content.grid_columnconfigure(0, weight=1)
        self._content.grid_rowconfigure(0, weight=1)

        # Tray.
        self.tray = GuiTray(self, on_open=self._tray_open,
                            on_toggle_pause=self._tray_toggle_pause,
                            on_quit=self._tray_quit)
        self.tray.run()

        # Close button hides to tray (if tray available); else really quit.
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Unmap>", self._on_minimize)

        # Route entry: onboarding on first run, else dashboard.
        if not self.cfg.get("first_run_done"):
            self._show_onboarding()
        else:
            self._show_dashboard_and_start()

    # ----------------------------------------------------- window icon
    def _apply_window_icon(self):
        try:
            import os
            import tempfile
            img = W.make_icon_image(256)
            ico_path = os.path.join(tempfile.gettempdir(),
                                    "voiceflow_icon.ico")
            img.save(ico_path, format="ICO",
                     sizes=[(16, 16), (32, 32), (48, 48), (64, 64),
                            (128, 128), (256, 256)])
            self.iconbitmap(ico_path)
            self._icon_img = ico_path
        except Exception:
            pass

    # ----------------------------------------------------- view swapping
    def _clear_content(self):
        if self._current_view is not None:
            try:
                self._current_view.destroy()
            except Exception:
                pass
            self._current_view = None

    def _show_onboarding(self):
        self._clear_content()
        view = OnboardingView(self._content, self.cfg,
                              on_done=self._onboarding_done)
        view.grid(row=0, column=0, sticky="nsew", padx=16, pady=12)
        self._current_view = view

    def _onboarding_done(self, model_id, gpu_enabled):
        # cfg already saved by the onboarding flow (model + first_run_done).
        self.cfg = vf_config.load_config()
        self._show_dashboard_and_start()

    def show_dashboard(self):
        self._show_dashboard_and_start()

    def _show_dashboard_and_start(self):
        self._clear_content()
        self.dashboard = DashboardView(self._content, self)
        self.dashboard.grid(row=0, column=0, sticky="nsew")
        self._current_view = self.dashboard
        # Reflect current known state immediately.
        self.dashboard.set_trigger(self.cfg.get("trigger"))
        self.dashboard.set_armed(self._armed)
        if self._model_loaded and self.engine is not None:
            self.dashboard.set_model_info(self.engine.current_model_info())
            self.dashboard.set_state("idle")
        else:
            self.dashboard.set_state("idle")
            self.dashboard.set_log("Loading the speech model…")
            self._ensure_engine_started()

    def show_settings(self):
        self._clear_content()
        self.settings_view = SettingsView(self._content, self)
        self.settings_view.grid(row=0, column=0, sticky="nsew")
        self._current_view = self.settings_view

    def show_history(self):
        self._clear_content()
        from .history_view import HistoryView
        self.history_view = HistoryView(self._content, self)
        self.history_view.grid(row=0, column=0, sticky="nsew")
        self._current_view = self.history_view

    def show_file_transcriber(self):
        """Swap in the audio/video file transcription view. The view reuses the
        already-loaded engine model read-only; if it isn't loaded yet it kicks
        off the same background load the dashboard uses."""
        self._clear_content()
        from .file_transcriber_view import FileTranscriberView
        self.file_transcriber_view = FileTranscriberView(self._content, self)
        self.file_transcriber_view.grid(row=0, column=0, sticky="nsew")
        self._current_view = self.file_transcriber_view
        # Best-effort: make sure the model is loading so the user isn't blocked.
        if not self._model_loaded:
            self._ensure_engine_started()

    def on_learned(self):
        """A learn/clear event happened in the History or Settings view; reload
        the engine's learned vocab + correction rules so the next utterance uses
        them immediately (no restart)."""
        if self.engine is not None:
            try:
                self.engine._reload_learned()
            except Exception:
                pass

    def on_snippets_changed(self):
        """A snippet was added/edited/removed or toggled in Settings; reload the
        engine's snippets so the next utterance uses them immediately (no
        restart). Best-effort: never raises into the UI."""
        if self.engine is not None:
            try:
                self.engine.reload_snippets()
            except Exception:
                pass

    def reload_modes(self):
        """A per-app mode was added/edited/removed in Settings; reload the
        engine's modes so the next utterance uses them immediately (no restart).
        Best-effort: never raises into the UI."""
        if self.engine is not None:
            try:
                self.engine.reload_modes()
            except Exception:
                pass

    # ----------------------------------------------------- engine lifecycle
    def _build_engine(self):
        """Create the DictationEngine with UI-marshalling callbacks."""
        from voiceflow.engine import DictationEngine
        eng = DictationEngine(
            self.cfg,
            on_state=lambda s: self.schedule(self._on_state, s),
            on_transcript=lambda t: self.schedule(self._on_transcript, t),
            on_log=lambda m: self.schedule(self._on_log, m),
            on_level=lambda lv: self.schedule(self._on_level, lv),
            on_preview_show=lambda: self.schedule(self._on_preview_show),
            on_preview_text=lambda t: self.schedule(self._on_preview_text, t),
            on_preview_hide=lambda: self.schedule(self._on_preview_hide),
        )
        return eng

    def _ensure_engine_started(self):
        """Load the model (bg thread) then start() the engine. Idempotent."""
        if self._model_loaded:
            return
        with self._engine_lock:
            if self.engine is None:
                try:
                    self.engine = self._build_engine()
                except Exception:
                    log.error("Engine construction failed:\n%s",
                              traceback.format_exc())
                    self.schedule(self._on_log,
                                  "Engine failed to initialize. See the log.")
                    return
        threading.Thread(target=self._load_and_start_worker, daemon=True,
                         name="engine-load").start()

    def _load_and_start_worker(self):
        try:
            def prog(msg):
                self.schedule(self._on_log, msg)
            info = self.engine.load_model(progress_cb=prog)
            self._model_loaded = True
            self.schedule(self._on_model_loaded, info)
            armed = self.engine.start()
            self._armed = armed
            self.schedule(self._on_started, armed)
        except Exception as exc:
            tb = traceback.format_exc()
            log.error("Model load/start failed:\n%s", tb)
            self.schedule(self._on_load_failed, exc)

    def _on_model_loaded(self, info):
        if self.dashboard is not None:
            self.dashboard.set_model_info(info)
            self.dashboard.set_state("idle")

    def _on_started(self, armed):
        if self.dashboard is not None:
            self.dashboard.set_armed(armed)
            if armed:
                self.dashboard.set_log(
                    "Ready. Press your trigger to dictate.")
            else:
                self.dashboard.set_log(
                    "Could not register the trigger. Try changing it.")
        self.tray.set_paused(not armed)

    def _on_load_failed(self, exc):
        if self.dashboard is not None:
            self.dashboard.set_log("Model failed to load: %s" % exc)
            self.dashboard.set_state("idle")

    # ----------------------------------------------------- engine callbacks
    def _on_state(self, state_label):
        if self.dashboard is not None:
            self.dashboard.set_state(state_label)
        self.tray.set_state(state_label)

    def _on_transcript(self, text):
        if self.dashboard is not None:
            self.dashboard.set_transcript(text)

    def _on_log(self, message):
        if self.dashboard is not None:
            self.dashboard.set_log(message)

    def _on_level(self, level):
        if self.dashboard is not None:
            self.dashboard.set_level(level)

    # ------------------------------------------------- live-preview overlay
    def _ensure_overlay(self):
        """Lazily create the preview bar on the UI thread (Tk widget). Reused for
        the app lifetime."""
        if self._overlay is None:
            from .overlay import PreviewOverlay
            self._overlay = PreviewOverlay(
                self, max_chars=self.cfg.get("preview_max_chars", 120))
        return self._overlay

    def _on_preview_show(self):
        try:
            self._ensure_overlay().show()
        except Exception:
            pass        # fail-open: overlay failure must never break dictation

    def _on_preview_text(self, t):
        if self._overlay is not None:
            try:
                self._overlay.set_text(t)
            except Exception:
                pass

    def _on_preview_hide(self):
        if self._overlay is not None:
            try:
                self._overlay.hide()
            except Exception:
                pass

    # ----------------------------------------------------- dashboard actions
    def toggle_dictation(self):
        """Pause/resume the trigger (engine stays warm)."""
        if self.engine is None or not self._model_loaded:
            return
        if self._armed:
            self.engine.pause()
            self._armed = False
        else:
            self._armed = self.engine.resume()
        if self.dashboard is not None:
            self.dashboard.set_armed(self._armed)
        self.tray.set_paused(not self._armed)

    def open_trigger_picker(self):
        picker = TriggerPicker(self, current=self.cfg.get("trigger"),
                               on_apply=self._apply_trigger)
        result = picker.show()
        if result:
            self.cfg["trigger"] = result
            if self.dashboard is not None:
                self.dashboard.set_trigger(result)
            if self.settings_view is not None:
                try:
                    self.settings_view.refresh_trigger()
                except Exception:
                    pass

    def _apply_trigger(self, trigger):
        """Called on the UI thread from the picker's Save. Apply live if the
        engine is running; otherwise just persist to config."""
        if self.engine is not None and self._model_loaded:
            ok = self.engine.set_trigger(trigger)
            if ok:
                self.cfg["trigger"] = trigger
                self._armed = self.engine.is_armed
            return ok
        # Engine not up yet -> persist directly.
        self.cfg["trigger"] = trigger
        vf_config.save_config(self.cfg)
        return True

    def apply_behavior_change(self, key, value):
        """A behavior toggle changed in Settings. The engine reads self.cfg live
        for most flags (beep is built into Beeper at construct time, so rebuild
        the beeper). cfg is the same dict the engine holds."""
        if self.engine is not None:
            try:
                if key == "beep":
                    from voiceflow.engine import Beeper
                    self.engine.beeper = Beeper(value)
            except Exception:
                pass

    def apply_mode_change(self, mode):
        """Batch/streaming mode changed in Settings. Applied live (no reload)."""
        self.cfg["mode"] = mode
        if self.engine is not None:
            try:
                self.engine.set_mode(mode)
            except Exception:
                pass
        else:
            from voiceflow.config import save_config
            save_config(self.cfg)

    def switch_active_model(self, model_id, on_done):
        """Switch the active model and reload the engine on a bg thread.
        on_done(ok: bool, info: dict|None, exc) is called from the bg thread (the
        caller marshals to UI)."""
        def work():
            try:
                with self._engine_lock:
                    if self.engine is not None:
                        try:
                            self.engine.stop()
                        except Exception:
                            pass
                    self.cfg["model"] = model_id
                    vf_config.save_config(self.cfg)
                    self._model_loaded = False
                    self.engine = self._build_engine()
                info = self.engine.load_model(
                    progress_cb=lambda m: self.schedule(self._on_log, m))
                self._model_loaded = True
                armed = self.engine.start()
                self._armed = armed
                self.schedule(self._post_switch, info, armed)
                on_done(True, info, None)
            except Exception as exc:
                log.error("Model switch failed:\n%s", traceback.format_exc())
                on_done(False, None, exc)
        threading.Thread(target=work, daemon=True, name="engine-switch").start()

    def _post_switch(self, info, armed):
        if self.dashboard is not None:
            self.dashboard.set_model_info(info)
            self.dashboard.set_armed(armed)
        self.tray.set_paused(not armed)

    # ----------------------------------------------------- thread marshaling
    def schedule(self, fn, *args):
        """Run fn(*args) on the Tk UI thread. Safe to call from any thread."""
        try:
            self.after(0, lambda: self._safe_call(fn, *args))
        except Exception:
            pass

    def _safe_call(self, fn, *args):
        try:
            fn(*args)
        except Exception:
            log.debug("UI callback error:\n%s", traceback.format_exc())

    # ----------------------------------------------------- tray + close
    def _tray_open(self):
        self.schedule(self._restore_window)

    def _restore_window(self):
        try:
            self.deiconify()
            self.lift()
            self.focus_force()
            self.state("normal")
        except Exception:
            pass

    def _tray_toggle_pause(self, paused):
        self.schedule(self._set_paused_from_tray, paused)

    def _set_paused_from_tray(self, paused):
        if self.engine is None or not self._model_loaded:
            return
        if paused:
            self.engine.pause()
            self._armed = False
        else:
            self._armed = self.engine.resume()
        if self.dashboard is not None:
            self.dashboard.set_armed(self._armed)

    def _tray_quit(self):
        self.schedule(self._really_quit)

    def _on_minimize(self, event=None):
        # Only treat actual iconify of the main window (not child toplevels).
        if event is not None and event.widget is not self:
            return

    def _on_close(self):
        """Closing the window hides to tray (with a one-time hint) when a tray is
        available; otherwise it really quits."""
        if self.tray.available:
            if not self._closing_hint_shown:
                self._closing_hint_shown = True
                self._show_tray_hint()
            self.withdraw()
        else:
            self._really_quit()

    def _show_tray_hint(self):
        try:
            hint = ctk.CTkToplevel(self)
            hint.title(APP_DISPLAY_NAME)
            hint.configure(fg_color=T.BG)
            hint.geometry("400x180")
            hint.resizable(False, False)
            hint.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(hint, text="Still running in the tray",
                         font=T.font("h2"), text_color=T.TEXT).grid(
                row=0, column=0, padx=24, pady=(22, 6), sticky="w")
            W.hint_label(
                hint, "OpenVerba keeps listening in the system tray so you can "
                      "dictate anytime. Right-click the tray icon to pause or "
                      "quit.", color=T.TEXT_MUTED, wraplength=350).grid(
                row=1, column=0, padx=24, sticky="w")
            W.accent_button(hint, "Got it", width=110,
                            command=hint.destroy).grid(
                row=2, column=0, sticky="e", padx=24, pady=18)
            hint.transient(self)
            hint.after(10, lambda: self._center_child(hint))
        except Exception:
            pass

    def _center_child(self, child):
        try:
            child.update_idletasks()
            x = self.winfo_rootx() + (self.winfo_width()
                                      - child.winfo_width()) // 2
            y = self.winfo_rooty() + (self.winfo_height()
                                      - child.winfo_height()) // 2
            child.geometry("+%d+%d" % (max(0, x), max(0, y)))
        except Exception:
            pass

    def _really_quit(self):
        try:
            if self.engine is not None:
                self.engine.stop()
        except Exception:
            pass
        try:
            if self._overlay is not None:
                self._overlay.destroy()
                self._overlay = None
        except Exception:
            pass
        try:
            self.tray.stop()
        except Exception:
            pass
        try:
            self.quit()
            self.destroy()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Public entry point used by app.py: from voiceflow.ui.main_window import launch
# ---------------------------------------------------------------------------
def launch():
    """Create and run the VoiceFlow GUI. Blocks until the window is closed."""
    app = VoiceFlowApp()
    app.mainloop()
