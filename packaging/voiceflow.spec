# -*- mode: python ; coding: utf-8 -*-
"""
Cross-platform PyInstaller spec for VoiceFlow (onedir, windowed/no-console).

One spec drives all three OSes (PyInstaller cannot cross-compile, so it is run
on a native runner per OS -- see the GitHub Actions matrix in
docs/PRODUCTION_PLAN.md section 5):

    Windows  -> dist/VoiceFlow/VoiceFlow.exe      (onedir)   -> Inno Setup .exe
    macOS    -> dist/VoiceFlow.app                (BUNDLE)   -> codesign -> .dmg
    Linux    -> dist/VoiceFlow/VoiceFlow          (onedir)   -> AppImage

Build (from the project root, with the build venv active):
    pyinstaller --noconfirm --clean packaging/voiceflow.spec

WHAT WE BUNDLE
--------------
- The GUI + engine source (the src/voiceflow package: voiceflow.ui = GUI,
  voiceflow.platform = OS backend layer, voiceflow.engine, etc.). Entry is
  src/voiceflow/__main__.py, which imports voiceflow._cuda_shim FIRST (Windows
  CUDA DLL path registration) before anything pulls in faster_whisper.
- faster_whisper + ctranslate2 + onnxruntime + customtkinter via collect_all
  (their data files, binaries AND hidden submodules in one shot). customtkinter
  ships theme JSON/.otf assets loaded by path -- without them CTk raises
  FileNotFoundError at runtime. ctranslate2/onnxruntime are compiled native libs.
- copy_metadata for huggingface_hub + faster_whisper: faster_whisper reads its
  own dist metadata (version), and huggingface_hub's import machinery checks
  installed-package metadata; missing *.dist-info -> importlib.metadata errors.
- sounddevice + its bundled PortAudio DLL (_sounddevice_data/portaudio-binaries)
  -- without it mic capture fails with "PortAudio library not found".
- The per-OS input/clipboard/paste stack (see PER-OS HIDDEN IMPORTS below).

WHAT WE DELIBERATELY DO NOT BUNDLE
----------------------------------
- The multi-gigabyte NVIDIA CUDA runtime wheels (nvidia-cublas-cu12 /
  nvidia-cudnn-cu12) and torch. They are excluded so a GPU-enabled dev venv can
  never balloon the installer to multiple GB. The CUDA runtime is fetched on
  demand only when the user opts in to GPU acceleration
  (voiceflow.cuda.install_gpu_runtime). The CI build venv is CPU-only.
- The speech models. They are always downloaded on first run into the per-user
  data dir via huggingface_hub, so the installer stays small.
"""

import os
import sys

from PyInstaller.utils.hooks import collect_all, collect_data_files, copy_metadata

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

# ---------------------------------------------------------------------------
# Locate the spec dir (packaging/) and the project root (its parent). PyInstaller
# injects SPECPATH = the directory containing this .spec file; we then anchor on
# src/voiceflow/__main__.py, walking up until we find it, so the spec is runnable
# regardless of the current working directory.
# ---------------------------------------------------------------------------
try:
    _SPEC_DIR = os.path.abspath(SPECPATH)  # noqa: F821 (injected by PyInstaller)
except NameError:
    _SPEC_DIR = os.path.dirname(os.path.abspath(__file__))


def _find_project_root(start):
    """Return the nearest ancestor (incl. parent of packaging/) that holds the
    src/voiceflow package. Falls back to the parent of the spec dir."""
    candidate = os.path.dirname(start)  # parent of packaging/
    here = candidate
    for _ in range(6):
        if os.path.isfile(os.path.join(here, "src", "voiceflow", "__main__.py")):
            return here
        parent = os.path.dirname(here)
        if parent == here:
            break
        here = parent
    return candidate


PROJECT_ROOT = _find_project_root(_SPEC_DIR)
PACKAGING_DIR = _SPEC_DIR if os.path.basename(_SPEC_DIR).lower() == "packaging" \
    else os.path.join(PROJECT_ROOT, "packaging")

APP_NAME = "VoiceFlow"
SRC_DIR = os.path.join(PROJECT_ROOT, "src")
ENTRY = os.path.join(SRC_DIR, "voiceflow", "__main__.py")
ASSETS_DIR = os.path.join(PROJECT_ROOT, "assets")
VERSION_FILE = os.path.join(PACKAGING_DIR, "version_info.txt")  # Windows-only metadata

# Per-OS icon. The repo ships assets/voiceflow.ico (Windows) and
# assets/voiceflow.png (Linux/source). A macOS .icns is generated in CI from the
# png (iconutil) into assets/voiceflow.icns; fall back to .ico/.png/none so the
# build never hard-fails on a missing icon during local/dev runs.
def _first_existing(*names):
    for n in names:
        p = os.path.join(ASSETS_DIR, n)
        if os.path.exists(p):
            return p
    return None


if IS_WIN:
    ICON = _first_existing("voiceflow.ico", "icon.ico", "voiceflow.png")
elif IS_MAC:
    ICON = _first_existing("voiceflow.icns", "icon.icns", "voiceflow.png")
else:
    ICON = _first_existing("voiceflow.png", "icon.png")

# ---------------------------------------------------------------------------
# collect_all the heavy/native packages: data files, binaries AND submodules in
# one call. This is the cross-platform-correct way (the old per-OS hand-listing
# of ctranslate2/onnxruntime libs is error-prone on macOS/Linux .so/.dylib).
# ---------------------------------------------------------------------------
datas = []
binaries = []
hiddenimports = ["darkdetect"]  # ctk runtime dep loaded by string -> must be explicit

for _pkg in ("faster_whisper", "ctranslate2", "onnxruntime", "customtkinter"):
    try:
        _d, _b, _h = collect_all(_pkg)
        datas += _d
        binaries += _b
        hiddenimports += _h
    except Exception:
        # onnxruntime may be absent in a slim analysis; faster_whisper imports it
        # lazily for VAD, so a missing collect_all here is non-fatal.
        pass

# dist metadata so importlib.metadata lookups succeed in the frozen app.
datas += copy_metadata("huggingface_hub")
datas += copy_metadata("faster_whisper")

# huggingface_hub data/templates (collect_all already covers faster_whisper's deps,
# but pull hub data explicitly -- it is the first-run download surface).
try:
    datas += collect_data_files("huggingface_hub")
except Exception:
    pass

# Ship our own assets (icon used by the tray / window at runtime).
if os.path.isdir(ASSETS_DIR):
    datas.append((ASSETS_DIR, "assets"))

# ---------------------------------------------------------------------------
# sounddevice + its PortAudio native lib (all OSes). collect_data_files keeps the
# _sounddevice_data/portaudio-binaries/ layout intact; sounddevice loads it by
# relative path at import time.
# ---------------------------------------------------------------------------
try:
    datas += collect_data_files("_sounddevice_data", include_py_files=False)
except Exception:
    pass

# ---------------------------------------------------------------------------
# Hidden imports common to all OSes (lazy / string / C-extension imports that
# static analysis misses). The collect_all above already adds the bulk for the
# four heavy packages; these cover the remaining runtime stack.
# ---------------------------------------------------------------------------
hiddenimports += [
    # audio capture + its CFFI backend
    "sounddevice", "_sounddevice", "cffi", "_cffi_backend",
    # clipboard text helper
    "pyperclip",
    # inference + model download (collect_all covers most; pin the roots)
    "ctranslate2", "faster_whisper", "huggingface_hub",
    # GUI
    "customtkinter", "darkdetect",
    "numpy",
]

# ---------------------------------------------------------------------------
# PER-OS HIDDEN IMPORTS -- the platform-abstraction layer picks the backend at
# runtime; bundle only the relevant OS's input/clipboard/paste/tray deps so the
# Windows pywin32 stack does not become a (failing) hidden import on macOS/Linux.
# ---------------------------------------------------------------------------
if IS_WIN:
    hiddenimports += [
        # win32clipboard (all-format save/restore) + SendInput + win32 APIs
        "win32clipboard", "win32con", "win32api", "win32gui",
        "win32process", "pywintypes", "pythoncom",
        # keyboard-combo + mouse triggers
        "keyboard", "mouse",
        # tray icon (pystray Windows backend) + imaging
        "pystray", "pystray._win32",
        "PIL", "PIL.Image", "PIL.ImageDraw", "PIL._tkinter_finder",
    ]
elif IS_MAC:
    hiddenimports += [
        # pyobjc: NSPasteboard clipboard, CGEvent Cmd+V, TCC permission probes
        "pynput",
        "objc", "Foundation", "AppKit", "Quartz", "CoreFoundation",
        "ApplicationServices",
        "PyObjCTools",
        # tray icon (pystray macOS / darwin backend) + imaging
        "pystray", "pystray._darwin",
        "PIL", "PIL.Image", "PIL.ImageDraw", "PIL._tkinter_finder",
    ]
else:  # Linux (X11 + Wayland share linux_common)
    hiddenimports += [
        # evdev hotkeys/mouse; pynput X11 fallback; subprocess tools (xclip /
        # xdotool / wl-clipboard / ydotool / wtype) are external binaries, not
        # python imports, so nothing to bundle for those.
        "evdev", "pynput",
        # tray icon (pystray GTK/Xorg backend) + imaging
        "pystray", "pystray._xorg",
        "PIL", "PIL.Image", "PIL.ImageDraw", "PIL._tkinter_finder",
    ]

# Our own package (so a frozen run that imports by string still resolves). With
# the src/ layout everything lives under the 'voiceflow' package.
hiddenimports += [
    "voiceflow", "voiceflow.__main__", "voiceflow._cuda_shim",
    "voiceflow.app", "voiceflow.cli", "voiceflow.engine",
    "voiceflow.ui", "voiceflow.ui.main_window",
    "voiceflow.platform", "voiceflow.triggers",
    "voiceflow.platform.windows", "voiceflow.platform.macos",
    "voiceflow.platform.linux_x11", "voiceflow.platform.linux_wayland",
    "voiceflow.platform.linux_common",
]

# ---------------------------------------------------------------------------
# Excludes -- keep the bundle small and CPU-only.
#   * torch / nvidia CUDA wheels / *-gpu runtimes are fetched on demand (GPU
#     opt-in) or never used -> NEVER bundled.
#   * dev/test tooling that may live in the venv.
# ---------------------------------------------------------------------------
excludes = [
    # GPU / heavy ML runtimes -- keep CUDA OUT (fetched on opt-in)
    "torch", "torchaudio", "torchvision",
    "onnxruntime_gpu", "onnxruntime-gpu",
    "nvidia", "nvidia_cublas_cu12", "nvidia_cudnn_cu12",
    "triton",
    # dev/test tooling
    "pytest", "tests",
    "tkinter.test", "lib2to3", "pydoc_data",
]


block_cipher = None

a = Analysis(
    [ENTRY],
    pathex=[SRC_DIR, PROJECT_ROOT],   # SRC_DIR first so 'voiceflow' resolves to src/
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)


# Belt-and-braces: physically drop any NVIDIA CUDA libs or torch shared objects
# that a hook or a dependency dragged in from a GPU-enabled dev venv. These are
# large and must be fetched on demand, never shipped. Matches Win .dll, Linux
# .so* and macOS .dylib by name fragment.
def _strip_gpu(entries):
    kept = []
    for name, path, kind in entries:
        low = (name or "").lower()
        plow = (path or "").lower()
        if any(tok in low or tok in plow for tok in (
                "nvidia", "cublas", "cudnn", "cudart", "cufft",
                "libtorch", "/torch/", "\\torch\\")):
            continue
        kept.append((name, path, kind))
    return kept


a.binaries = _strip_gpu(a.binaries)
a.datas = _strip_gpu(a.datas)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,          # onedir (binaries live alongside, not in exe)
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                       # UPX trips antivirus heuristics; keep off
    console=False,                   # windowed (no console)
    disable_windowed_traceback=False,
    argv_emulation=IS_MAC,           # macOS: receive Apple "open" events / file args
    target_arch=None,                # native arch of the runner (CI matrix per arch)
    codesign_identity=None,          # signing handled post-build in CI (notarytool)
    entitlements_file=None,
    icon=ICON,
    version=VERSION_FILE if (IS_WIN and os.path.exists(VERSION_FILE)) else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,                   # -> dist/VoiceFlow/
)

# ---------------------------------------------------------------------------
# macOS .app bundle. BUNDLE (not COLLECT) is required so every inner .dylib/.so
# and Python.framework is signable -- unsigned embedded binaries are the #1
# notarization rejection (PRODUCTION_PLAN.md sec 4.2). LSUIElement keeps it a
# menu-bar/agent app (no Dock icon); NSMicrophoneUsageDescription drives the mic
# TCC prompt. Accessibility / Input Monitoring are runtime TCC grants (no plist
# key) handled by voiceflow.platform.macos + ui.first_run.
# ---------------------------------------------------------------------------
if IS_MAC:
    app = BUNDLE(
        coll,
        name=APP_NAME + ".app",
        icon=ICON,
        bundle_identifier="com.voiceflow.app",
        version="1.0.0",
        info_plist={
            "CFBundleName": APP_NAME,
            "CFBundleDisplayName": APP_NAME,
            "CFBundleShortVersionString": "1.0.0",
            "CFBundleVersion": "1.0.0",
            "NSMicrophoneUsageDescription":
                "VoiceFlow records your microphone to transcribe speech locally on your device.",
            "LSUIElement": True,           # menu-bar agent, no Dock icon
            "LSMinimumSystemVersion": "11.0",
            "NSHighResolutionCapable": True,
            "NSHumanReadableCopyright": "MIT-licensed. See THIRD-PARTY-LICENSES.md.",
        },
    )
