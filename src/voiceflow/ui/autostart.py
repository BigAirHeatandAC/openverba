"""
gui.autostart - enable/disable "start VoiceFlow at login" via the per-user
HKCU\\...\\Run registry key (no admin needed). The Run entry launches the
background runtime (app.py --background), which is what autostart should use.

Helpers degrade gracefully off-Windows / on error and never raise to the GUI.
"""

from __future__ import annotations

import os
import sys

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_VALUE_NAME = "VoiceFlow"


def _launch_command():
    """The command Windows should run at login: the background runtime.

    Frozen (PyInstaller) build -> the exe with --background.
    Source build              -> pythonw.exe -m voiceflow --background.

    Note: in the src/ layout there is no top-level app.py to point at; the
    package is launched as a module (``-m voiceflow``), which dispatches
    ``--background`` to the headless runtime (voiceflow.__main__ / cli).
    """
    if getattr(sys, "frozen", False):
        return '"%s" --background' % sys.executable
    # Source: prefer pythonw so no console flashes; fall back to python.
    exe = sys.executable
    pyw = exe
    if exe.lower().endswith("python.exe"):
        cand = exe[:-len("python.exe")] + "pythonw.exe"
        if os.path.isfile(cand):
            pyw = cand
    return '"%s" -m voiceflow --background' % pyw


def is_enabled():
    if os.name != "nt":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            val, _ = winreg.QueryValueEx(key, _VALUE_NAME)
            return bool(val)
    except FileNotFoundError:
        return False
    except OSError:
        return False
    except Exception:
        return False


def set_enabled(enabled: bool):
    """Add/remove the HKCU Run entry. Returns True on success."""
    if os.name != "nt":
        return False
    try:
        import winreg
        if enabled:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
                winreg.SetValueEx(key, _VALUE_NAME, 0, winreg.REG_SZ,
                                  _launch_command())
            return True
        else:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0,
                                    winreg.KEY_SET_VALUE) as key:
                    winreg.DeleteValue(key, _VALUE_NAME)
            except FileNotFoundError:
                pass
            return True
    except Exception:
        return False
