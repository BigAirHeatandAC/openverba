"""
voiceflow.ui - the VoiceFlow customtkinter GUI package.

Public entry point: launch() opens the main window (onboarding on first run,
otherwise the dashboard). app.py imports it as `from voiceflow.ui.main_window
import launch`; it is also re-exported here for convenience.

Submodules:
  theme          - dark palette, accent color, fonts, shared layout constants
  widgets        - reusable styled customtkinter building blocks + app icon
  onboarding     - first-run flow (welcome -> hardware scan -> model pick ->
                   download -> optional GPU enable -> done)
  dashboard      - main view (status, model/device, trigger, last transcript,
                   start/pause, mic meter)
  trigger_picker - live trigger-capture dialog (TriggerRecorder)
  settings       - model manager, behavior toggles, autostart, log folder, about
  autostart      - HKCU Run-key "start at login" helpers
  tray           - system tray icon for the GUI process
  main_window    - the App window + launch()
"""

from __future__ import annotations


def launch():
    """Lazily import and run the GUI (so importing `gui` is cheap and doesn't
    require customtkinter/engine until the window is actually opened)."""
    from .main_window import launch as _launch
    return _launch()


__all__ = ["launch"]
