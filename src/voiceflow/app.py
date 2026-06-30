"""
voiceflow.app - the GUI entry point (the `voiceflow` gui-script / default mode).

`main()` launches the customtkinter GUI (onboarding on first run, otherwise the
dashboard). The headless/background dictation runtime lives in
``voiceflow.cli`` (the `voiceflow-cli` script / ``--background``); ``__main__``
dispatches between them.

This module does NOT import the engine/faster_whisper at import time; the GUI
imports the engine lazily when the user starts dictation, and ``__main__`` runs
``voiceflow._cuda_shim`` before anything imports faster_whisper.
"""

import os
import sys
import logging
import threading
import ctypes
from logging.handlers import RotatingFileHandler

from .constants import APP_NAME, LOG_PATH, ensure_data_dir
from . import __version__


# ---------------------------------------------------------------------------
# Logging -> per-user data dir (rotating file + stdout).
# ---------------------------------------------------------------------------
def setup_logging():
    ensure_data_dir()
    logger = logging.getLogger()
    if logger.handlers:
        return logging.getLogger("voiceflow")
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s")
    try:
        fh = RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=3,
                                 encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except Exception as exc:
        print("Could not open log file %s: %s" % (LOG_PATH, exc))
    try:
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(fmt)
        logger.addHandler(ch)
    except Exception:
        pass
    return logging.getLogger("voiceflow")


log = None  # set in run_gui()


def _notify(title, message):
    """Best-effort MessageBox so a headless run isn't an invisible hang."""
    if os.name != "nt":
        print("%s: %s" % (title, message))
        return
    try:
        def _mb():
            try:
                ctypes.windll.user32.MessageBoxW(
                    0, str(message), str(title), 0x40 | 0x1000)  # INFO|TOPMOST
            except Exception:
                pass
        threading.Thread(target=_mb, daemon=True).start()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# GUI mode (default).
# ---------------------------------------------------------------------------
def run_gui():
    global log
    log = setup_logging()
    log.info("OpenVerba %s starting (GUI).", __version__)
    try:
        from voiceflow.ui.main_window import launch
    except Exception as exc:
        # Helpful message if the GUI module isn't present yet / customtkinter
        # is missing, instead of a bare traceback.
        import traceback
        log.error("Could not start the GUI:\n%s", traceback.format_exc())
        _notify("OpenVerba",
                "The OpenVerba window could not start (%s). You can still run "
                "the dictation runtime with:  voiceflow-cli --background" % exc)
        print("Could not start the GUI: %s\n"
              "Run the headless runtime with:  voiceflow-cli --background" % exc)
        return 1
    try:
        launch()
        return 0
    except Exception:
        import traceback
        log.error("GUI crashed:\n%s", traceback.format_exc())
        return 1


# ---------------------------------------------------------------------------
# Argument dispatch (the gui-script entry). Mirrors the original app.py so
# `voiceflow`, `voiceflow --background`, and `voiceflow --version` all work.
# ---------------------------------------------------------------------------
def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--version" in argv or "-V" in argv:
        print("OpenVerba %s" % __version__)
        return 0
    if "--background" in argv or "--headless" in argv:
        from . import cli
        return cli.run_background()
    return run_gui()


if __name__ == "__main__":
    sys.exit(main())
