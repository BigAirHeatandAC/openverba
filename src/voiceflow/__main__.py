"""
voiceflow.__main__ - `python -m voiceflow` entry point.

Dispatch (preserves the original app.py behaviour):
  python -m voiceflow                 launch the GUI (default; onboarding 1st run)
  python -m voiceflow --background    run the dictation runtime headless (tray)
  python -m voiceflow --headless      (alias of --background)
  python -m voiceflow --version       print the version and exit

CRITICAL ORDERING: import ``voiceflow._cuda_shim`` FIRST so the Windows CUDA DLL
search path is set up before anything imports faster_whisper / ctranslate2.
Only after that do we import the app/cli dispatchers.
"""

import sys

# When PyInstaller freezes the app it runs THIS file as a top-level script
# (module name "__main__", no parent package), so `from . import ...` raises
# "attempted relative import with no known parent package". To work BOTH as
# `python -m voiceflow` (package context) and as the frozen entry script, import
# the package absolutely; if that fails (running the file directly, unfrozen),
# put src/ on sys.path and retry. CRITICAL ORDERING is preserved: importing the
# `voiceflow` package runs voiceflow/__init__, and `_cuda_shim` is imported
# before anything pulls in faster_whisper / ctranslate2.
try:
    import voiceflow._cuda_shim  # noqa: F401  (MUST be first: registers CUDA DLLs)
    from voiceflow import __version__
except ImportError:
    import os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import voiceflow._cuda_shim  # noqa: F401
    from voiceflow import __version__


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--version" in argv or "-V" in argv:
        print("OpenVerba %s" % __version__)
        return 0
    if "--background" in argv or "--headless" in argv:
        from voiceflow import cli
        return cli.run_background()
    from voiceflow import app
    return app.run_gui()


if __name__ == "__main__":
    sys.exit(main())
