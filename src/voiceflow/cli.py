"""
voiceflow.cli - headless/background dictation runtime (the `voiceflow-cli`
console script).

Modes:
  voiceflow-cli                  run the dictation runtime headless (tray only),
  voiceflow-cli --background     reading the saved config. This is what
  voiceflow-cli --headless       autostart-at-login uses.
  voiceflow-cli --version        print the version and exit.

The single-instance guard applies to this BACKGROUND runtime (two runtimes race
on the global clipboard and both grab the trigger). The GUI does not take the
mutex, so you can open settings while the background runtime is running -- the
GUI talks to its own in-process engine only when you start dictation from it.

NOTE: ``voiceflow._cuda_shim`` must be imported before faster_whisper. The
package ``__main__`` does this; when ``voiceflow-cli`` is invoked as a console
script we import the shim here too (cheap + idempotent) for safety.
"""

import os
import sys
import time
import logging
import threading
import ctypes
from ctypes import wintypes

from . import _cuda_shim  # noqa: F401  (register CUDA DLLs before faster_whisper)
from .constants import (
    APP_NAME, APP_DISPLAY_NAME, LOG_PATH, SINGLETON_MUTEX, ensure_data_dir,
    STATE_LABELS, IDLE, RECORDING, TRANSCRIBING,
)
from . import config as vf_config
from . import updater
from . import __version__
from .app import setup_logging, _notify


log = None  # set in run_background()


# ---------------------------------------------------------------------------
# Single-instance guard (machine-wide named mutex).
# ---------------------------------------------------------------------------
_SINGLETON_HANDLE = None


def acquire_single_instance():
    """Return True if we are the only instance. Two runtimes each register the
    same trigger and race on the global clipboard, silently breaking pasting."""
    global _SINGLETON_HANDLE
    if os.name != "nt":
        return True
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = wintypes.HANDLE
        k32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL,
                                     wintypes.LPCWSTR)
        _SINGLETON_HANDLE = k32.CreateMutexW(None, True, SINGLETON_MUTEX)
        ERROR_ALREADY_EXISTS = 183
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            return False
        return True
    except Exception:
        return True  # never block startup over the guard itself


def _model_appears_cached(cfg):
    """Heuristic: is the model already downloaded (so we won't fetch on start)?"""
    try:
        name = cfg.get("model", "")
        # Model given as a direct path to an existing model directory -> local,
        # no download needed (don't show the "first run downloading" popup).
        if name and os.path.isdir(name):
            return True
        root = vf_config.resolve_download_root(cfg)
        if root and os.path.isdir(root):
            for d in os.listdir(root):
                if name and (name in d):
                    return True
        # default HF cache (in case a model was pulled there)
        hub = os.path.join(os.path.expanduser("~"), ".cache", "huggingface", "hub")
        if os.path.isdir(hub):
            for d in os.listdir(hub):
                if name and name in d and "whisper" in d.lower():
                    return True
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# Background (headless) runtime: engine + tray, no GUI window.
# ---------------------------------------------------------------------------
def run_background():
    global log
    log = setup_logging()

    if not acquire_single_instance():
        msg = ("Another OpenVerba instance is already running. Quit it from the "
               "tray first (this one would clash on the trigger/clipboard).")
        log.warning(msg)
        _notify("OpenVerba already running", msg)
        print(msg)
        return 0

    cfg = vf_config.load_config()
    log.info("=" * 60)
    log.info("OpenVerba %s starting (background runtime).", __version__)
    if cfg.get("__parse_warn__"):
        log.warning(cfg["__parse_warn__"])
    for c in cfg.get("__corrections__", []):
        log.warning("config correction: %s", c)

    # Import the engine LAST (it registers CUDA DLLs + imports faster_whisper).
    from voiceflow.engine import DictationEngine

    engine = DictationEngine(cfg)

    if not _model_appears_cached(cfg):
        _notify("OpenVerba",
                "First run: downloading the speech model. This can take a couple "
                "of minutes. OpenVerba is ready when the start beep / tray icon "
                "appears.")

    try:
        engine.load_model()
    except Exception:
        import traceback
        log.error("FATAL: could not load any model:\n%s", traceback.format_exc())
        _notify("OpenVerba - FATAL",
                "The speech model failed to load. See voiceflow.log. If this is "
                "the first run and you are offline, connect once to download.")
        return 1

    # Optional tray icon (idle/recording/working dot + Open/Pause/Quit).
    tray = _make_tray(engine, cfg)

    if not engine.start():
        _notify("OpenVerba - FATAL",
                "Could not register the trigger '%s'. Change it in Settings or "
                "config.json. See voiceflow.log." % cfg.get("trigger"))
        return 1

    msg = ("OpenVerba ready (%s, model=%s). Press %s to dictate."
           % (engine.device, cfg.get("model"), cfg.get("trigger")))
    log.info(msg)
    print("\n" + msg + "\n")

    # Update the tray dot from engine state.
    if tray is not None:
        engine.on_state = lambda s: tray.set_state(s)

    # Once-a-day background update check: NOTIFY only (never auto-installs and
    # kills a live dictation session). Fire-and-forget; never blocks startup, and
    # never re-nags for a version the user was already told about.
    def _auto_update_poll():
        try:
            if not updater.due_for_check(cfg):
                return
            res = updater.check_for_updates(cfg, interactive=False)
            if res.available and res.version != cfg.get("last_notified_version"):
                _notify("OpenVerba update available",
                        "Version %s is ready. Open OpenVerba and choose "
                        "\"Check for updates\" to install (you're on %s)."
                        % (res.version, res.current))
                cfg["last_notified_version"] = res.version
                try:
                    vf_config.save_config(cfg)
                except Exception:
                    pass
        except Exception:
            pass
    threading.Thread(target=_auto_update_poll, daemon=True,
                     name="update-check").start()

    # Block forever (the tray runs on its own thread; keyboard hooks on theirs).
    try:
        import keyboard
        keyboard.wait()
    except KeyboardInterrupt:
        pass
    except Exception:
        # No keyboard backend (rare) -> idle-wait so the process stays alive.
        while True:
            time.sleep(3600)
    finally:
        try:
            engine.stop()
        except Exception:
            pass
    return 0


def _make_tray(engine, cfg):
    """Build a minimal system tray (Open/Pause/Quit) for the background runtime.
    Returns a small wrapper with set_state(), or None if pystray is missing."""
    try:
        import pystray
        from PIL import Image
    except Exception:
        log.info("pystray/pillow not installed; running without a tray icon.")
        return None

    colors = {
        STATE_LABELS[IDLE]: (120, 120, 120),
        STATE_LABELS[RECORDING]: (220, 40, 40),
        STATE_LABELS[TRANSCRIBING]: (230, 180, 30),
    }

    def _dot(color):
        img = Image.new("RGB", (64, 64), (32, 32, 32))
        px = img.load()
        for x in range(64):
            for y in range(64):
                if (x - 32) ** 2 + (y - 32) ** 2 <= 20 ** 2:
                    px[x, y] = color
        return img

    icons = {k: _dot(v) for k, v in colors.items()}

    class _Tray:
        def __init__(self):
            self._paused = False

            def _toggle_pause(icon, item):
                if self._paused:
                    engine.resume()
                    self._paused = False
                else:
                    engine.pause()
                    self._paused = True

            def _open_gui(icon, item):
                # Launch the GUI as a separate process so settings are reachable.
                # Frozen (PyInstaller) build: the exe IS the launcher -> run it
                # with no args (default = GUI). Source build: run the package GUI
                # entry (python -m voiceflow).
                try:
                    import subprocess
                    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    if getattr(sys, "frozen", False):
                        cmd = [sys.executable]
                    else:
                        cmd = [sys.executable, "-m", "voiceflow"]
                    subprocess.Popen(cmd, creationflags=flags)
                except Exception:
                    pass

            def _quit(icon, item):
                try:
                    engine.stop()
                except Exception:
                    pass
                icon.stop()
                os._exit(0)

            def _check_updates(icon, item):
                # Runs on a worker so the tray stays responsive. On an available
                # update: download + verify, then hand off to the installer and
                # exit (cleanly stops the engine first so locks release).
                def work():
                    try:
                        res = updater.check_for_updates(cfg, interactive=True)
                        if res.status == "error":
                            _notify("OpenVerba",
                                    "Couldn't check for updates: %s" % res.error)
                        elif res.available:
                            _notify("OpenVerba update",
                                    "Version %s is available. Downloading and "
                                    "starting the installer…" % res.version)
                            path = updater.download(res.info.url, res.info.sha256)
                            if not path:
                                _notify("OpenVerba",
                                        "The update download failed or did not "
                                        "verify. Please try again later.")
                                return
                            try:
                                engine.stop()
                            except Exception:
                                pass
                            try:
                                icon.stop()
                            except Exception:
                                pass
                            updater.launch_installer_and_exit(path)
                        else:
                            _notify("OpenVerba",
                                    "You're on the latest version (%s)."
                                    % res.current)
                    except Exception:
                        pass
                threading.Thread(target=work, daemon=True,
                                 name="tray-update").start()

            menu = pystray.Menu(
                pystray.MenuItem("Open OpenVerba", _open_gui),
                pystray.MenuItem("Check for updates…", _check_updates),
                pystray.MenuItem(
                    lambda item: "Resume" if self._paused else "Pause",
                    _toggle_pause),
                pystray.MenuItem("Quit", _quit),
            )
            self.icon = pystray.Icon(
                "voiceflow", icons[STATE_LABELS[IDLE]],
                APP_DISPLAY_NAME + ": idle", menu)

        def set_state(self, state_label):
            try:
                self.icon.icon = icons.get(state_label, icons[STATE_LABELS[IDLE]])
                self.icon.title = APP_DISPLAY_NAME + ": " + state_label
            except Exception:
                pass

        def run(self):
            threading.Thread(target=self.icon.run, daemon=True,
                             name="tray").start()

    try:
        t = _Tray()
        t.run()
        log.info("Tray icon active.")
        return t
    except Exception as exc:
        log.warning("Tray icon unavailable (%s); running without it.", exc)
        return None


# ---------------------------------------------------------------------------
# Argument dispatch (the console-script entry).
# ---------------------------------------------------------------------------
def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--version" in argv or "-V" in argv:
        print("OpenVerba %s" % __version__)
        return 0
    return run_background()


if __name__ == "__main__":
    sys.exit(main())
