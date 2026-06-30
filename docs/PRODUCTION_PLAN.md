# VoiceFlow — Production Plan

**Status:** Authoritative implementation blueprint (v1)
**Last updated:** 2026-06-14
**Audience:** The engineering team implementing the cross-platform, open-source release.

VoiceFlow is a **free, open-source, fully local/offline speech-to-text dictation desktop app**. Flow:
press a trigger → record mic → transcribe locally with **faster-whisper** (GPU if available, else CPU) →
paste into the focused app (set clipboard + synthesize paste keystroke) → **restore the prior clipboard**.
Python app, GUI via **customtkinter**. Currently Windows-only; this plan makes it production-grade and
cross-platform (Windows, macOS, Linux X11 + Wayland).

This document is **opinionated**: one primary approach per decision, with rationale, gotchas, and the
code/config you should write. Deviate only with a recorded reason.

---

## 0. Top-level decisions (the TL;DR)

| Area | Decision | Why |
|---|---|---|
| Layout | `src/` layout, single `pyproject.toml`, **Hatchling** backend | PyPA-maintained, zero-config, VCS-aware versioning |
| Env / lockfile | **uv** + committed `uv.lock`; floored ranges in `pyproject.toml` (it's an *app*) | Reproducible installs across OSes; exact pins live in the lock, not the metadata |
| GUI | **customtkinter**, launched via `[project.gui-scripts]` (no console window on Windows) | MIT; existing codebase already uses it |
| Engine | **faster-whisper (CTranslate2) everywhere**, with a CPU-int8 fallback ladder | One engine, max code reuse, never hard-fails for lack of GPU |
| GPU | Win/Linux NVIDIA via **pip wheels** `nvidia-cublas-cu12` + `nvidia-cudnn-cu12==9.*`, downloaded **on opt-in, not bundled**; macOS = CPU int8 (optional `pywhispercpp` Metal backend) | CTranslate2 has no Metal backend; bundling CUDA is the #1 packaging trap |
| Models | **Downloaded on first run** from Hugging Face into a per-user cache; never bundled or committed | Installer stays small (hundreds of MB, not multi-GB) |
| Packaging | **PyInstaller `--onedir`** core, per-OS native installer, driven by a **GitHub Actions OS matrix** | Only tool with a mature story for all 3 native installer targets; matrix neutralizes its no-cross-compile weakness |
| Per-OS artifact | Win: Inno Setup `.exe` (per-user, no admin) · macOS: `.app`→`.dmg`, signed + **notarized** · Linux: **AppImage** (primary) + Flatpak (secondary) | Native, smooth installs; AppImage avoids Flatpak sandbox breaking hotkeys/paste |
| Signing | Win: **Azure Trusted Signing** (~$9.99/mo, OIDC) · macOS: Developer ID + notarytool + staple | EV certs no longer give instant SmartScreen; notarization is mandatory for a clean macOS install |
| Input layer | Thin **platform-abstraction layer** chosen at runtime by OS + (Linux) session type | Wayland vs X11 and macOS TCC permissions make a single library impossible |
| Hotkeys | **pynput** default everywhere; **evdev** on Linux (works under Wayland + hold-mode) | pynput silently fails under native Wayland; evdev distinguishes real release from autorepeat |
| Clipboard | **Format-aware native** code per OS (NOT pyperclip) for full save/restore | pyperclip is text-only; full restore is a stated requirement (best-effort for lazy formats) |
| Updates | **v1:** simple "check GitHub Releases API → notify" updater. **Later:** tufup (TUF-based) | No key-management overhead for v1; tufup when you want silent delta updates |
| License | **MIT** app. Note: **pynput is LGPL-3.0** — keep it an unmodified, separately-installed dep | All other core deps are MIT/BSD; LGPL dep is fine if not vendored/modified |

---

## 1. Project file tree, `pyproject.toml`, entry points

### 1.1 File tree (`src/` layout, with the platform-abstraction layer)

```
VoiceFlowApp/
├── pyproject.toml                # PEP 621 metadata, Hatchling backend
├── uv.lock                       # committed — reproducible installs
├── README.md                     # badges: CI, license, py-versions, downloads
├── LICENSE                       # MIT
├── THIRD-PARTY-LICENSES.md       # pynput=LGPL-3.0; whisper/CT2/ctk=MIT; pyperclip note
├── CHANGELOG.md                  # Keep a Changelog + SemVer
├── CONTRIBUTING.md
├── CODE_OF_CONDUCT.md            # Contributor Covenant 2.1
├── SECURITY.md
├── .pre-commit-config.yaml
├── .gitignore
├── docs/
│   └── PRODUCTION_PLAN.md        # this file
├── packaging/
│   ├── voiceflow.spec            # committed PyInstaller spec (onedir, CPU-only)
│   ├── windows/installer.iss     # Inno Setup, per-user install
│   ├── macos/entitlements.plist  # mic + cpython/ct2 entitlements
│   ├── macos/Info.plist.patch    # NSMicrophoneUsageDescription, LSUIElement
│   └── linux/voiceflow.desktop   # .desktop + AppImage recipe / AppDir layout
├── assets/
│   ├── icon.ico  icon.icns  icon.png
├── src/
│   └── voiceflow/
│       ├── __init__.py           # __version__ = "0.1.0"
│       ├── __main__.py           # `python -m voiceflow` → app.main()
│       ├── app.py                # GUI entry: main()
│       ├── cli.py                # headless/debug entry: main()
│       ├── config.py             # settings model + per-OS config/cache dirs (platformdirs)
│       ├── _cuda_shim.py         # MUST import before faster_whisper (Win DLL path / Linux re-exec)
│       ├── audio/
│       │   └── recorder.py       # sounddevice mic capture → wav/np array
│       ├── transcribe/
│       │   ├── engine.py         # backend selection, fallback ladder
│       │   ├── models.py         # catalog + hardware→model recommendation
│       │   ├── backend_faster_whisper.py
│       │   └── backend_whispercpp.py   # optional macOS Metal path
│       ├── download/
│       │   └── hf.py             # first-run model download + CTkProgressBar hook
│       ├── platform/             # <<< the platform-abstraction layer
│       │   ├── __init__.py       # make_backends() factory + detect_platform()
│       │   ├── base.py           # ABCs: HotkeyBackend, MouseBackend, ClipboardBackend, Paster, Permissions
│       │   ├── windows.py        # SendInput, win32clipboard, pynput/boppreh side-button
│       │   ├── macos.py          # pyobjc NSPasteboard, CGEvent, TCC permission helpers
│       │   ├── linux_x11.py      # xdotool/pynput, xclip, evdev
│       │   └── linux_wayland.py  # ydotool/wtype, wl-clipboard, evdev
│       ├── core/
│       │   └── dictation.py      # orchestrates: snapshot→set→paste→restore
│       ├── ui/
│       │   ├── main_window.py    # customtkinter
│       │   ├── first_run.py      # model download + permission guidance
│       │   └── settings_view.py  # trigger, model, GPU opt-in, paste-chord
│       └── resources/
└── tests/
    ├── conftest.py
    ├── test_platform_factory.py  # detect_platform / make_backends selection
    ├── test_engine_fallback.py   # CPU fallback ladder (mock CT2)
    ├── test_models.py            # recommendation matrix
    └── test_dictation.py         # snapshot/restore flow (mock clipboard+paster)
```

### 1.2 `pyproject.toml` (essentials)

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "voiceflow"
dynamic = ["version"]
description = "Free, offline, local speech-to-text dictation for your desktop."
readme = "README.md"
requires-python = ">=3.10"
license = "MIT"
license-files = ["LICENSE"]
keywords = ["speech-to-text", "dictation", "whisper", "offline", "local"]
classifiers = [
  "Development Status :: 4 - Beta",
  "Intended Audience :: End Users/Desktop",
  "License :: OSI Approved :: MIT License",
  "Operating System :: OS Independent",
  "Programming Language :: Python :: 3.10",
  "Programming Language :: Python :: 3.11",
  "Programming Language :: Python :: 3.12",
  "Programming Language :: Python :: 3.13",
  "Topic :: Multimedia :: Sound/Audio :: Speech",
]
# APPLICATION → floored ranges here; exact versions live in uv.lock
dependencies = [
  "faster-whisper>=1.1",     # MIT (pulls CTranslate2 MIT, av, huggingface-hub, tokenizers, onnxruntime)
  "customtkinter>=5.2",      # MIT
  "darkdetect>=0.8",         # MIT — ctk runtime dep, must be explicit for PyInstaller
  "pynput>=1.8",             # LGPL-3.0 → see THIRD-PARTY-LICENSES.md
  "sounddevice>=0.5",        # MIT (mic capture)
  "numpy>=1.26",
  "platformdirs>=4.2",       # per-OS cache/config dirs
  "huggingface-hub>=0.24",   # explicit: first-run download UX
  "packaging>=24.0",         # version compare for the updater
  "requests>=2.32",          # updater (GitHub Releases API)
]

[project.optional-dependencies]
# User opt-in; NEVER bundled. Linux-only wheels for GPU.
gpu = ["nvidia-cublas-cu12", "nvidia-cudnn-cu12==9.*"]
# macOS accelerated backend (optional power-user path)
macos-metal = ["pywhispercpp>=1.3"]

[dependency-groups]              # PEP 735 dev deps (uv-native)
dev = ["pytest>=8", "pytest-cov>=5", "ruff>=0.9", "mypy>=1.13",
       "pre-commit>=4", "pyinstaller>=6.11"]

[project.urls]
Homepage = "https://github.com/your-org/voiceflow"
Repository = "https://github.com/your-org/voiceflow"
Issues = "https://github.com/your-org/voiceflow/issues"
Changelog = "https://github.com/your-org/voiceflow/blob/main/CHANGELOG.md"

# GUI launcher → NO console window on Windows
[project.gui-scripts]
voiceflow = "voiceflow.app:main"
# Headless/debug CLI (console attached)
[project.scripts]
voiceflow-cli = "voiceflow.cli:main"

[tool.hatch.version]
path = "src/voiceflow/__init__.py"

[tool.hatch.build.targets.wheel]
packages = ["src/voiceflow"]

[tool.ruff]
line-length = 100
target-version = "py310"
src = ["src", "tests"]
[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "SIM", "C4", "PTH", "RUF"]

[tool.mypy]
python_version = "3.10"
strict = true
ignore_missing_imports = true     # customtkinter, pynput, pyobjc lack full stubs

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-ra --cov=voiceflow --cov-report=term-missing"
```

**Entry points rationale:** `gui-scripts` is identical to `scripts` *except* it does not attach a Windows
console — use it for the user-facing launcher. Keep a `scripts` `voiceflow-cli` for headless/debug
transcription (good for CI smoke tests and bug reports).

---

## 2. The platform-abstraction layer

A thin runtime-selected abstraction is the architectural backbone. **Wayland vs X11** (different
hotkey/paste mechanisms) and **macOS TCC permissions** make a single cross-platform library impossible —
pynput silently fails under native Wayland, can't see mouse side-buttons, and on macOS needs *two* distinct
permissions.

### 2.1 Detection + factory

```python
# src/voiceflow/platform/__init__.py
import os, sys

def detect_platform() -> str:
    if sys.platform == "win32":  return "windows"
    if sys.platform == "darwin": return "macos"
    if os.environ.get("WAYLAND_DISPLAY") or os.environ.get("XDG_SESSION_TYPE") == "wayland":
        return "linux-wayland"
    return "linux-x11"

def make_backends():
    p = detect_platform()
    if   p == "windows":       from . import windows as b
    elif p == "macos":         from . import macos as b
    elif p == "linux-wayland": from . import linux_wayland as b
    else:                      from . import linux_x11 as b
    return b.Hotkeys(), b.Mouse(), b.Clipboard(), b.Paster(), b.Permissions()
```

### 2.2 Interfaces (`platform/base.py`) — exact signatures

```python
from abc import ABC, abstractmethod
from typing import Callable

class HotkeyBackend(ABC):
    @abstractmethod
    def register(self, combo: str, on_press: Callable[[], None],
                 on_release: Callable[[], None] | None = None) -> None: ...
    @abstractmethod
    def start(self) -> None: ...
    @abstractmethod
    def stop(self) -> None: ...
    @property
    @abstractmethod
    def supports_hold_mode(self) -> bool: ...   # push-to-talk reliable?

class MouseBackend(ABC):
    supports_side_buttons: bool = False
    @abstractmethod
    def register(self, button: str, on_press: Callable[[], None],
                 on_release: Callable[[], None] | None = None) -> None: ...
    @abstractmethod
    def start(self) -> None: ...
    @abstractmethod
    def stop(self) -> None: ...

class ClipboardBackend(ABC):
    @abstractmethod
    def snapshot(self) -> dict[str, bytes | str]: ...   # {fmt/MIME: data} best-effort, eager formats
    @abstractmethod
    def restore(self, snap: dict[str, bytes | str]) -> None: ...
    @abstractmethod
    def set_text(self, text: str) -> None: ...

class Paster(ABC):
    @abstractmethod
    def paste(self) -> None: ...                 # synthesize Ctrl/Cmd+V
    @abstractmethod
    def set_chord(self, chord: str) -> None: ...  # e.g. "ctrl+v", "cmd+v", "shift+insert"

class Permissions(ABC):
    @abstractmethod
    def check(self) -> dict[str, bool]: ...      # {"accessibility": bool, "input_monitoring": bool, "mic": bool}
    @abstractmethod
    def request(self, name: str) -> None: ...    # open the right OS settings pane / trigger prompt
    @abstractmethod
    def all_ok(self) -> bool: ...
```

### 2.3 Capability matrix — which library implements what, per OS

| Capability | Windows | macOS | Linux X11 | Linux Wayland |
|---|---|---|---|---|
| **Global hotkey** | pynput `GlobalHotKeys` | pynput (needs **Input Monitoring** TCC) | pynput **or** evdev | **evdev** (pynput fails under native Wayland) |
| **Hold/push-to-talk** | pynput | pynput | evdev preferred | evdev (real release vs autorepeat) |
| **Mouse side-buttons (X1/X2)** | boppreh `mouse` lib | Quartz `CGEventTap` (pyobjc) | evdev | evdev |
| **Paste keystroke** | `SendInput` via ctypes (configurable `Shift+Insert` for terminals) | pynput/CGEvent **Cmd+V** (needs **Accessibility** TCC) | xdotool or pynput | **ydotool** (primary) → **wtype** (wlroots fallback) |
| **Clipboard read/write (format-aware)** | `pywin32` `win32clipboard` (CF_UNICODETEXT, CF_HTML, CF_DIB/PNG, CF_HDROP) | `pyobjc` `NSPasteboard` (iterate `pasteboardItems` → `types()` → `dataForType_`) | `xclip -t <target>` per TARGET | `wl-copy`/`wl-paste --list-types` per MIME |
| **Permission prompts** | n/a (UIPI note below) | TCC: Accessibility + Input Monitoring + Mic | n/a | n/a |

**pynput is the default hotkey lib everywhere** because it's the only single actively-maintained library
that does Win/macOS/X11 (v1.8.x, 2025, with injected-event detection so your own synthesized paste doesn't
re-trigger the hotkey). On Linux, prefer **evdev** when `/dev/input` is readable. Do **not** base the
foundation on the boppreh `keyboard`/`mouse` libs (unmaintained, root-on-Linux, weak macOS) — use boppreh
`mouse` only as the Windows side-button helper.

### 2.4 macOS permissions (a real product hazard)

macOS needs **two distinct TCC permissions**, and a frozen `.app` often does **not** trigger the prompt and
may silently no-op:

- **Input Monitoring** (`kTCCServiceListenEvent`) — to *listen* to the global hotkey.
- **Accessibility** (`kTCCServicePostEvent` / `AXIsProcessTrusted`) — to *post* Cmd+V.
- **Microphone** (`NSMicrophoneUsageDescription` in Info.plist) — to record.

Requirements:
1. Ship a **properly signed, notarized `.app`** — TCC grants bind to the code signature/identity; an
   unsigned/ad-hoc rebuild loses the grant.
2. Build a **first-run UI** (`ui/first_run.py`) that detects missing permissions, opens the exact Settings
   pane, and re-checks `AXIsProcessTrusted` after the user grants:

```python
# platform/macos.py — guide user to the right pane
import subprocess
def open_accessibility_pane():
    subprocess.run(["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"])
def open_input_monitoring_pane():
    subprocess.run(["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent"])
```

### 2.5 Linux X11 vs Wayland handling

- **Detect** via `XDG_SESSION_TYPE` / `WAYLAND_DISPLAY` (see `detect_platform`).
- **Hotkeys:** evdev (works on both; needs the user in group `input` or a udev rule). Surface an in-app
  diagnostic if `/dev/input` is unreadable.
- **Paste:** auto-pick `ydotool` (universal via kernel uinput; needs `ydotoold` + `/dev/uinput`) else
  `wtype` (sway/Hyprland). GNOME needs special setup — surface a diagnostic, don't promise "just works".
- **Clipboard:** `wl-clipboard` on Wayland, `xclip` on X11.
- **Never simulate *typing* the transcript** on Wayland — non-ASCII and non-US layouts garble. Always use
  **clipboard + paste-keystroke** (layout-independent).
- **Treat Wayland as a "may require setup" tier** with clear in-app diagnostics. AppImage (unsandboxed) is
  the Linux primary precisely because the Flatpak sandbox can break global hotkeys/synthetic input.

### 2.6 Core dictation flow (`core/dictation.py`)

```python
import time
def emit_transcript(text, clip, paster, settle=0.12):
    saved = clip.snapshot()         # best-effort: text/html/rtf/png/file-lists (eager formats only)
    try:
        clip.set_text(text)
        paster.paste()              # Ctrl/Cmd+V (configurable chord per OS)
        time.sleep(settle)          # let the target app read clipboard BEFORE restore (avoids race)
    finally:
        clip.restore(saved)         # lazily-promised/delayed-rendered formats are lost — documented
```

### 2.7 Windows input gotchas (bake into UI/docs)

- `Ctrl+V` fails in Windows Terminal/cmd/PowerShell → make the paste chord configurable, offer
  `Shift+Insert`.
- **UIPI/UAC:** a non-elevated VoiceFlow can't inject paste into an elevated window. To dictate into admin
  apps, VoiceFlow itself must run elevated. Document this.
- Use `SendInput` (ctypes), not deprecated `keybd_event`.

---

## 3. Engine plan (faster-whisper everywhere + GPU story + model matrix)

**One engine on all three OSes: faster-whisper (CTranslate2).** Heavy dep is CTranslate2, **not PyTorch**, so
the CPU base is a few hundred MB (av/PyAV bundles FFmpeg, plus ctranslate2 + tokenizers + onnxruntime + Tcl/Tk).
Build the CI venv **CPU-only** and **exclude torch / *-gpu** so PyInstaller can never pack multi-GB CUDA.

### 3.1 Fallback ladder (never hard-fail)

`CUDA float16` → `CUDA int8_float16` (low VRAM) → **`CPU int8`** (universal safety net, genuinely usable for
dictation with small/base/turbo).

```python
# transcribe/engine.py
import sys, ctranslate2
from faster_whisper import WhisperModel

def pick_backend():
    if sys.platform == "darwin":                 # CTranslate2 has NO Metal backend → CPU only
        return ("cpu", "int8")
    try:
        if ctranslate2.get_cuda_device_count() > 0:
            return ("cuda", "float16")
    except Exception:
        pass
    return ("cpu", "int8")

def load_model(size, low_vram=False, download_root=None):
    device, compute = pick_backend()
    if device == "cuda" and low_vram:
        compute = "int8_float16"                  # turbo int8 ~1.5 GB → fits a 2 GB GPU
    try:
        return WhisperModel(size, device=device, compute_type=compute, download_root=download_root)
    except Exception:                             # any CUDA/DLL failure → universal fallback
        return WhisperModel(size, device="cpu", compute_type="int8", download_root=download_root)
```

### 3.2 Per-OS GPU story

**The CUDA runtime ships via pip wheels, NOT a system CUDA toolkit, and is downloaded on opt-in — never
bundled.** `pip install nvidia-cublas-cu12 nvidia-cudnn-cu12==9.*` (CTranslate2 ≥4.5 needs cuDNN 9 + CUDA ≥12.3).

- **Windows + NVIDIA:** ctranslate2 loads cuBLAS/cuDNN **by name** and only searches the Windows DLL path,
  **NOT** site-packages. You **must** call `os.add_dll_directory()` on the nvidia wheel lib dirs **before**
  `import ctranslate2/faster_whisper`. This is `_cuda_shim.py`, imported first in `__main__.py`. (There are
  no nvidia-cublas/cudnn pip wheels for Windows historically — the wheels resolve, but if a user is on an
  older stack, the fallback is Purfview's cuBLAS+cuDNN archive dropped next to the exe.) Symptoms when wrong:
  `Could not locate cudnn_ops64_9.dll` / `cublas64_12.dll not found`.
- **Linux + NVIDIA:** ctranslate2 reads `LD_LIBRARY_PATH`, which is read **at process launch** — you cannot
  fix it from inside a running interpreter (`os.add_dll_directory` is Windows-only). Solution: a launcher
  that exports it, or a one-time **self-re-exec** computing the path from `nvidia.cublas.lib`/`nvidia.cudnn.lib`.
- **macOS:** **No CUDA, no Metal in CTranslate2** → CPU int8 only. **Do not advertise GPU with
  faster-whisper on Mac.** Optional accelerated path: `pywhispercpp` (whisper.cpp; Metal on by default in a
  Metal build, Core ML/ANE via `WHISPER_COREML=1` source build) behind the same `transcribe(wav)->str`
  interface. Prefer pywhispercpp over mlx-whisper (same GGUF model story, prebuilt CPU wheels, works on Intel
  Macs); reserve mlx-whisper for power users (separate MLX model catalog).

```python
# _cuda_shim.py — MUST run before importing ctranslate2/faster_whisper
import os, sys
def enable_cuda_dlls():
    if sys.platform != "win32":
        return                          # Linux: LD_LIBRARY_PATH set before launch; macOS: n/a
    try:
        import nvidia.cublas.lib, nvidia.cudnn.lib
    except ImportError:
        return                          # CPU-only install → faster-whisper falls back to CPU int8
    for mod in (nvidia.cublas.lib, nvidia.cudnn.lib):
        d = os.path.dirname(mod.__file__)
        if os.path.isdir(d):
            os.add_dll_directory(d)
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
```

### 3.3 Model catalog

| Model | Params | Notes for dictation |
|---|---|---|
| tiny | 39M (~75 MB) | weakest CPU fallback |
| base | 74M | good CPU low-latency |
| small | 244M | CPU sweet spot |
| medium | 769M | |
| large-v3 | 1550M (~1.5 GB) | best accuracy, GPU |
| **turbo** (large-v3-turbo) | 809M | distilled, ~5–8× faster, near-large; int8 ~1.5 GB VRAM |
| **distil-large-v3.5** | ~50% of large-v3 | ~6× faster, WER within ~1%; ~1.5× faster than turbo |

faster-whisper repos are named `Systran/faster-whisper-<size>`.

### 3.4 Hardware → model recommendation matrix (per OS)

```python
# transcribe/models.py
import sys, platform, psutil, ctranslate2

def recommend_model():
    # NVIDIA tier (Win/Linux)
    try:
        if sys.platform != "darwin" and ctranslate2.get_cuda_device_count() > 0:
            vram = query_vram_gb()                 # pynvml / nvidia-smi
            if vram >= 10: return ("large-v3", False)
            if vram >= 6:  return ("distil-large-v3.5", False)
            if vram >= 4:  return ("turbo", True)  # int8_float16 ~1.5 GB
            if vram >= 2:  return ("small", True)
            return ("base", True)
    except Exception:
        pass
    # Apple Silicon (CPU int8 here; optional pywhispercpp Metal)
    if sys.platform == "darwin" and platform.machine() == "arm64":
        um = psutil.virtual_memory().total / 1e9
        if um >= 32: return ("distil-large-v3.5", False)
        if um >= 16: return ("small", False)
        return ("base", False)
    # CPU-only RAM tiers (Intel Mac, AMD/Intel desktops)
    ram = psutil.virtual_memory().total / 1e9
    if ram >= 16: return ("small", False)
    if ram >= 8:  return ("base", False)
    return ("tiny", False)
```

### 3.5 First-run model download (GUI progress)

`WhisperModel(size)` auto-downloads from the HF Hub into the HF cache. **Route the cache to a writable
per-user dir** (`HF_HOME`/`download_root`) because the install dir may be read-only. Show progress by
injecting a custom `tqdm_class` into `snapshot_download` to drive a `CTkProgressBar`; let the user choose
model size and cache dir; handle offline/partial-download failures; offline reuse via
`WhisperModel(dir, local_files_only=True)`.

```python
# download/hf.py
from huggingface_hub import snapshot_download
from tqdm.auto import tqdm as _tqdm

class GuiTqdm(_tqdm):
    gui_cb = None
    def update(self, n=1):
        super().update(n)
        if GuiTqdm.gui_cb and self.total:
            GuiTqdm.gui_cb(self.n / self.total)

def download_model(size, models_dir, on_progress):
    GuiTqdm.gui_cb = on_progress     # e.g. progress_bar.set
    return snapshot_download(f"Systran/faster-whisper-{size}", local_dir=models_dir, tqdm_class=GuiTqdm)
```

```python
# config.py — writable per-user cache
import os
from pathlib import Path
from platformdirs import user_cache_dir
APPDIR = Path(user_cache_dir("VoiceFlow", "VoiceFlow"))
os.environ.setdefault("HF_HOME", str(APPDIR / "hf"))
MODELS_DIR = APPDIR / "models"
```

---

## 4. Packaging plan

**Core tool: PyInstaller `--onedir` (NOT `--onefile`)**, wrapped per-OS in the native installer, built by a
GitHub Actions matrix. `--onefile` is a trap with customtkinter (extracts to temp each launch; its
`.json`/`.otf` data files break) and with ctranslate2 native libs (mangled / AV false-positives).

**None of PyInstaller/Nuitka/Briefcase cross-compile** → a Windows artifact must build on Windows, macOS on
macOS (and Apple Silicon for arm64), Linux on Linux. The matrix is mandatory. PyInstaller wins over Briefcase
(tuned for Toga; weak at orchestrating large optional ML binaries) and Nuitka (fragile macOS
notarization; obfuscation is moot for OSS).

### 4.1 Committed `packaging/voiceflow.spec`

```python
from PyInstaller.utils.hooks import collect_all, copy_metadata
datas, binaries, hidden = [], [], ["darkdetect"]
for pkg in ("faster_whisper", "ctranslate2", "onnxruntime", "customtkinter"):
    d, b, h = collect_all(pkg); datas += d; binaries += b; hidden += h
datas += copy_metadata("huggingface_hub") + copy_metadata("faster_whisper")

a = Analysis(["src/voiceflow/__main__.py"], binaries=binaries, datas=datas,
             hiddenimports=hidden,
             excludes=["torch", "torchaudio", "onnxruntime_gpu", "nvidia", "triton"])  # keep CUDA OUT
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, exclude_binaries=True, name="VoiceFlow", console=False,
          icon="assets/icon.ico")
coll = COLLECT(exe, a.binaries, a.datas, name="VoiceFlow")            # --onedir
app  = BUNDLE(coll, name="VoiceFlow.app", icon="assets/icon.icns",    # macOS: BUNDLE not COLLECT
              bundle_identifier="com.voiceflow.app",
              info_plist={"NSMicrophoneUsageDescription":
                          "VoiceFlow records your microphone to transcribe speech locally.",
                          "LSUIElement": True})
```

### 4.2 Per-OS artifacts

**Windows → Inno Setup `.exe`, per-user, no admin.**
```ini
[Setup]
AppName=VoiceFlow
AppVersion=1.0.0
DefaultDirName={localappdata}\VoiceFlow
PrivilegesRequired=lowest
OutputBaseFilename=VoiceFlow-Setup
SetupIconFile=assets\icon.ico
[Files]
Source: "dist\VoiceFlow\*"; DestDir: "{app}"; Flags: recursesubdirs
[Icons]
Name: "{userprograms}\VoiceFlow"; Filename: "{app}\VoiceFlow.exe"
[Run]
Filename: "{app}\VoiceFlow.exe"; Flags: nowait postinstall skipifsilent
```

**macOS → `.app` → `.dmg`, signed + notarized + stapled.** Build as **BUNDLE** (not COLLECT) so every inner
`.dylib`/`.so` and `Python.framework` is signable — unsigned embedded binaries are the #1 notarization
rejection. Hardened runtime (`--options=runtime`) + `--timestamp` are mandatory. You **cannot staple a
`.zip`** — staple the `.app`/`.dmg`.
```bash
codesign --force --deep --options runtime --timestamp \
  --entitlements packaging/macos/entitlements.plist \
  -s "Developer ID Application: Your Name (TEAMID)" dist/VoiceFlow.app
hdiutil create -volname VoiceFlow -srcfolder dist/VoiceFlow.app -ov -format UDZO dist/VoiceFlow.dmg
xcrun notarytool submit dist/VoiceFlow.dmg --keychain-profile VF_PROFILE --wait   # notarytool, NOT altool
xcrun stapler staple dist/VoiceFlow.dmg
xcrun spctl --assess --type open --context context:primary-signature -v dist/VoiceFlow.dmg
```
`entitlements.plist`:
```xml
<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict>
  <key>com.apple.security.cs.allow-unsigned-executable-memory</key><true/>
  <key>com.apple.security.cs.disable-library-validation</key><true/>
  <key>com.apple.security.device.audio-input</key><true/>
</dict></plist>
```
**Apple Silicon vs Intel:** `macos-latest`/`macos-14`/`macos-15` are **arm64**. To cover Intel users add a
separate **`macos-15-intel`** leg (the `macos-13` image is being retired Dec 2025; Intel support ends
Fall 2027). Universal2 is impractical with arch-specific wheels (ctranslate2/onnxruntime).

**Linux → AppImage (primary) + Flatpak on Flathub (secondary).** AppImage = one portable file, no install,
unsandboxed → global hotkeys/synthetic paste work. Flatpak gives discoverability/auto-update but the sandbox
can break the hotkey + paste behaviors (portals/permissions on Wayland) — **validate the paste flow inside
the sandbox before promoting Flatpak**. Skip `.deb` unless specifically targeting Ubuntu. **Build the
AppImage on the oldest glibc you support** (e.g. `ubuntu-22.04`, not `ubuntu-latest`) — PyInstaller Linux
binaries are glibc-sensitive.

### 4.3 What is fetched at runtime, never bundled

- **Whisper models** → first-run HF download into the per-user cache (Section 3.5).
- **CUDA cuBLAS/cuDNN** → only on GPU opt-in: Linux via `nvidia-*` wheels into the app dir; Windows via
  Purfview archive into `APPDIR/cuda` + `os.add_dll_directory` at runtime.
- Build the CI venv **CPU-only**; the spec **excludes** torch/nvidia/*-gpu so CUDA can never be packed.

---

## 5. CI/CD (`.github/workflows/`)

Two workflows. Pin all third-party actions to a tag/SHA (signing pipeline = supply-chain sensitive).

### 5.1 `ci.yml` — lint + type + test matrix (every push/PR)

3 OS × 4 Python versions, coverage on one combo.

```yaml
name: CI
on: [push, pull_request]
jobs:
  test:
    runs-on: ${{ matrix.os }}
    strategy:
      fail-fast: false
      matrix:
        os: [ubuntu-latest, macos-latest, windows-latest]
        python-version: ["3.10", "3.11", "3.12", "3.13"]
    steps:
      - uses: actions/checkout@v4
      - name: Linux audio/clipboard deps
        if: runner.os == 'Linux'
        run: sudo apt-get update && sudo apt-get install -y xclip wl-clipboard libportaudio2
      - uses: astral-sh/setup-uv@v5
        with: { python-version: "${{ matrix.python-version }}" }
      - run: uv sync --frozen --group dev
      - run: uv run ruff check .
      - run: uv run ruff format --check .
      - run: uv run mypy src
      - run: uv run pytest
      - if: matrix.os == 'ubuntu-latest' && matrix.python-version == '3.12'
        uses: codecov/codecov-action@v5
```

### 5.2 `release.yml` — build + sign + package, release on `v*` tag

```yaml
name: build-release
on:
  push: { tags: ['v*'] }
  pull_request:
permissions:
  contents: write      # action-gh-release
  id-token: write      # Azure OIDC (Windows signing)
jobs:
  build:
    strategy:
      fail-fast: false
      matrix:
        include:
          - { os: windows-latest, artifact: VoiceFlow-windows-x64 }
          - { os: macos-14,       artifact: VoiceFlow-macos-arm64 }   # pin: arm64
          - { os: macos-15-intel, artifact: VoiceFlow-macos-x64 }     # optional Intel leg
          - { os: ubuntu-22.04,   artifact: VoiceFlow-linux-x64 }     # pin: oldest glibc
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.12', cache: pip }
      - run: pip install -r requirements.txt pyinstaller   # CPU-only; NO torch / NO *-gpu
      - run: pyinstaller packaging/voiceflow.spec --noconfirm

      # macOS: import cert → sign → dmg → notarize → staple (skip on fork PRs)
      - if: runner.os == 'macOS' && github.event_name != 'pull_request'
        uses: apple-actions/import-codesign-certs@v3
        with: { p12-file-base64: ${{ secrets.MAC_CERT_P12_BASE64 }}, p12-password: ${{ secrets.MAC_CERT_PASSWORD }} }
      - if: runner.os == 'macOS' && github.event_name != 'pull_request'
        env: { DEV_ID: ${{ secrets.MAC_DEVELOPER_ID }}, APPLE_ID: ${{ secrets.MAC_APPLE_ID }},
               APPLE_PW: ${{ secrets.MAC_APP_SPECIFIC_PASSWORD }}, TEAM_ID: ${{ secrets.MAC_TEAM_ID }} }
        run: |
          codesign --force --deep --options=runtime --timestamp \
            --entitlements packaging/macos/entitlements.plist -s "$DEV_ID" dist/VoiceFlow.app
          hdiutil create -volname VoiceFlow -srcfolder dist/VoiceFlow.app -ov -format UDZO dist/${{ matrix.artifact }}.dmg
          xcrun notarytool submit dist/${{ matrix.artifact }}.dmg \
            --apple-id "$APPLE_ID" --password "$APPLE_PW" --team-id "$TEAM_ID" --wait
          xcrun stapler staple dist/${{ matrix.artifact }}.dmg

      # Windows: Inno Setup → Azure Trusted Signing (OIDC)
      - if: runner.os == 'Windows'
        run: iscc packaging\windows\installer.iss
      - if: runner.os == 'Windows' && github.event_name != 'pull_request'
        uses: azure/login@v2
        with: { client-id: ${{ secrets.AZURE_CLIENT_ID }}, tenant-id: ${{ secrets.AZURE_TENANT_ID }},
                subscription-id: ${{ secrets.AZURE_SUBSCRIPTION_ID }} }
      - if: runner.os == 'Windows' && github.event_name != 'pull_request'
        uses: azure/trusted-signing-action@v0
        with:
          endpoint: https://eus.codesigning.azure.net/
          trusted-signing-account-name: ${{ secrets.SIGN_ACCOUNT }}
          certificate-profile-name: ${{ secrets.SIGN_PROFILE }}
          files-folder: ${{ github.workspace }}\dist
          files-folder-filter: exe
          file-digest: SHA256
          timestamp-rfc3161: http://timestamp.acs.microsoft.com

      # Linux: AppImage (build on ubuntu-22.04 for glibc portability)
      - if: runner.os == 'Linux'
        run: bash packaging/linux/build_appimage.sh   # appimagetool over the onedir AppDir

      - uses: actions/upload-artifact@v4
        with: { name: ${{ matrix.artifact }}, path: "dist/*.dmg\ndist/*Setup.exe\ndist/*.AppImage" }

  release:
    needs: build
    if: startsWith(github.ref, 'refs/tags/')
    runs-on: ubuntu-latest
    steps:
      - uses: actions/download-artifact@v4
        with: { path: artifacts, merge-multiple: true }
      - uses: softprops/action-gh-release@v2
        with: { files: artifacts/**/*, generate_release_notes: true }
```

**CI notes:**
- `macos-latest`/`macos-14`/`macos-15` are now **arm64** — pin the macОS leg explicitly so a future label
  move can't silently change target arch. The `macos-13` Intel image is retiring (Dec 2025); use
  `macos-15-intel` if you still ship Intel.
- **Fork PRs cannot read secrets** → guard signing/notarization with
  `github.event_name != 'pull_request'`.
- **Secrets hygiene:** macOS `.p12` and Windows cert material as base64 secrets; prefer **OIDC** for Azure
  over long-lived client secrets.
- **Azure Trusted Signing** is ~$9.99/mo (Basic, OIDC-integrated). Caveat: it's generally available to
  **organizations with a 3-year verifiable history** (individual sign-up is a public-preview path). Brand-new
  accounts still **build SmartScreen reputation over time** like a fresh OV cert. Shipping unsigned is viable
  for OSS but triggers SmartScreen "unknown publisher" until reputation accrues. EV certs are **not** worth
  it (no longer grant instant SmartScreen).

### 5.3 Auto-update

**v1: simple GitHub-Releases checker** (no key management) — compare installed version to the latest tag,
notify, open the download page:
```python
import requests
from packaging.version import Version
API = "https://api.github.com/repos/your-org/voiceflow/releases/latest"
def check_for_update(current: str):
    rel = requests.get(API, timeout=5, headers={"Accept": "application/vnd.github+json"}).json()
    latest = rel["tag_name"].lstrip("v")
    if Version(latest) > Version(current):
        return {"version": latest, "url": rel["html_url"], "notes": rel.get("body", "")}
    return None
```
**Later: tufup** (maintained TUF-based PyUpdater successor; v0.10+, packaging-agnostic, supports binary-diff
patches; GitHub Releases works as a dumb file host). Defer because you must generate/securely store TUF
root/targets/snapshot/timestamp keys.

---

## 6. OSS scaffolding

### 6.1 Community / meta files

| File | Purpose |
|---|---|
| `README.md` | Overview, install per-OS, badges (CI, license, py-versions, downloads), permission notes |
| `LICENSE` | **MIT** |
| `THIRD-PARTY-LICENSES.md` | Lists deps + licenses; **flags pynput = LGPL-3.0** |
| `CHANGELOG.md` | Keep a Changelog format + SemVer; `[Unreleased]` + dated releases |
| `CONTRIBUTING.md` | Dev setup (uv), how to run tests/lint, branch/PR conventions |
| `CODE_OF_CONDUCT.md` | Contributor Covenant 2.1 |
| `SECURITY.md` | Vulnerability reporting, supported versions |
| `.github/ISSUE_TEMPLATE/bug_report.yml` | Structured bug reports (OS, session type, GPU, model) |
| `.github/ISSUE_TEMPLATE/feature_request.yml` | Feature requests |
| `.github/ISSUE_TEMPLATE/config.yml` | Disable blank issues / link discussions |
| `.github/PULL_REQUEST_TEMPLATE.md` | PR checklist (tests, changelog, lint) |
| `.pre-commit-config.yaml` | ruff-check `--fix` → ruff-format → mypy → hygiene hooks |
| `.gitignore` | Python + build artifacts + model cache |

`.pre-commit-config.yaml` ordering matters — **`ruff-check --fix` BEFORE `ruff-format`**:
```yaml
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.9.6
    hooks:
      - { id: ruff-check, args: [--fix] }
      - { id: ruff-format }
  - repo: https://github.com/pre-commit/mirrors-mypy
    rev: v1.13.0
    hooks: [{ id: mypy }]
  - repo: https://github.com/pre-commit/pre-commit-hooks
    rev: v5.0.0
    hooks: [trailing-whitespace, end-of-file-fixer, check-merge-conflict, check-toml, check-yaml]
```

### 6.2 License choice + dependency compatibility

**App license: MIT.** Compatibility of core deps:

| Dependency | License | Compatible with MIT app? |
|---|---|---|
| faster-whisper | MIT | Yes |
| CTranslate2 | MIT | Yes |
| OpenAI Whisper (code + weights) | MIT | Yes (attribute in About box) |
| customtkinter | MIT | Yes |
| sounddevice / numpy / huggingface-hub | MIT/BSD | Yes |
| **pynput** | **LGPL-3.0** | **Yes, with conditions (below)** |
| pywin32 (Win clipboard) | PSF | Yes |
| pyobjc (macOS) | MIT | Yes |

**The pynput LGPL-3.0 gotcha (the single biggest compliance item):** keeping VoiceFlow MIT is fine because
LGPL permits use in a permissive app **provided** you (a) do **not** vendor or modify pynput's source,
(b) document pynput as LGPL-3.0 in `THIRD-PARTY-LICENSES.md`, and (c) since PyInstaller `--onedir` keeps
pynput as a separate importable file, users can replace/relink it — satisfying LGPL §4. (If you ever go
`--onefile`, document the relink path; another reason `--onedir` is correct here.) If LGPL friction ever
becomes unacceptable, swap pynput for an MIT global-hotkey lib — but evdev (the Linux path) plus native
SendInput/CGEvent already cover most cases.

**Whisper weights** are MIT but **never commit multi-GB weights to git or bundle them** — download on first
run; surface attribution in the About box.

---

## 7. Phased implementation checklist

### Phase 0 — Repo & scaffolding (1–2 days)
- [ ] `src/` layout, `pyproject.toml` (Hatchling), `uv.lock`, entry points (`gui-scripts` + `scripts`).
- [ ] Ruff + mypy + pytest + pre-commit; `ci.yml` (3 OS × 4 py) green.
- [ ] Community-health files incl. `THIRD-PARTY-LICENSES.md` (pynput=LGPL).

### Phase 1 — Engine + config (core value, fully testable on Windows)
- [ ] `config.py` (platformdirs cache/config), `_cuda_shim.py`.
- [ ] `transcribe/engine.py` (fallback ladder), `models.py` (recommendation matrix), `backend_faster_whisper.py`.
- [ ] `download/hf.py` first-run download with progress hook.
- [ ] `audio/recorder.py` (sounddevice).
- [ ] Unit tests: fallback ladder (mock CT2), recommendation matrix, download path.

### Phase 2 — Platform abstraction (the hard cross-platform work)
- [ ] `platform/base.py` ABCs + `__init__.py` factory.
- [ ] `platform/windows.py` first (current target): SendInput paste (+Shift+Insert), win32clipboard
      snapshot/restore, pynput hotkey, boppreh side-button.
- [ ] `core/dictation.py` snapshot→set→paste→restore with settle delay.
- [ ] Wire GUI (`main_window.py`, `settings_view.py`): trigger, model, GPU opt-in, paste chord.
- [ ] **Ship a working Windows build end-to-end** (PyInstaller onedir + Inno Setup).

### Phase 3 — macOS port
- [ ] `platform/macos.py`: pyobjc NSPasteboard, CGEvent Cmd+V, TCC `Permissions` (Accessibility + Input
      Monitoring + mic), `first_run.py` permission guidance.
- [ ] PyInstaller BUNDLE + entitlements + sign + notarize + staple in CI (`macos-14`, optional
      `macos-15-intel`).
- [ ] Optional `backend_whispercpp.py` (Metal) behind the engine interface.

### Phase 4 — Linux port
- [ ] `platform/linux_x11.py` (xdotool/pynput, xclip, evdev) and `platform/linux_wayland.py`
      (ydotool→wtype, wl-clipboard, evdev) + session detection.
- [ ] In-app diagnostics: `/dev/input` readable? `ydotoold` running? `/dev/uinput` accessible?
- [ ] AppImage build on `ubuntu-22.04`; validate paste/hotkey inside Flatpak before promoting it.

### Phase 5 — Release hardening
- [ ] `release.yml` end-to-end: signed artifacts for all OSes on `v*` tag via `action-gh-release`.
- [ ] Windows Azure Trusted Signing (OIDC); macOS notarization green; Linux AppImage attached.
- [ ] v1 GitHub-Releases update checker; CHANGELOG `0.1.0`.
- [ ] (Later) tufup silent updates.

### 7.1 What CAN be tested on a Windows-only dev machine
- Full engine stack: faster-whisper CPU int8 (and CUDA if an NVIDIA GPU is present), fallback ladder, model
  recommendation, first-run HF download + progress.
- Windows clipboard snapshot/restore (all formats), SendInput paste incl. Shift+Insert, pynput hotkey,
  boppreh side-button.
- The entire `windows.py` backend + `core/dictation.py` flow against real apps (Notepad, browser, Windows
  Terminal).
- PyInstaller onedir + Inno Setup installer.
- Unit tests for OS-agnostic logic (engine, models, config, the platform factory's *selection* logic via
  mocked `sys.platform`/env vars).
- The `ci.yml` matrix runs macOS/Linux jobs on GitHub runners even though you develop on Windows.

### 7.2 What CANNOT be properly tested on Windows-only (needs real hardware/CI/VMs)
- **macOS TCC permission flow** (Accessibility/Input Monitoring/mic prompts), `.app` signing + notarization
  + stapling, pyobjc NSPasteboard/CGEvent paste — needs a real Mac (or a notarization-capable macOS CI
  runner + a Mac for the TCC UX).
- **macOS Metal/pywhispercpp** acceleration — Apple Silicon only.
- **Linux Wayland vs X11** behavior: evdev `/dev/input` access, ydotool/`ydotoold`/`/dev/uinput`, wtype on
  wlroots, wl-clipboard MIME round-trip, Flatpak sandbox effects — needs Linux VMs/machines for both a
  Wayland (GNOME/KDE/sway) and an X11 session.
- **CUDA on Linux** (`LD_LIBRARY_PATH`-before-launch re-exec) — needs a Linux+NVIDIA box.
- **AppImage glibc portability** across distros — test on older distros / containers.
- **Cross-arch macOS** (Intel vs arm64 wheel resolution) — needs CI legs / both machines.

> Practical mitigation while Windows-only: lean on the GitHub Actions matrix for build-time validation of all
> three OSes, and recruit a small set of macOS/Linux beta testers for the permission/paste UX that CI cannot
> exercise.

---

## 8. Sources (key references)

- faster-whisper / CTranslate2: https://github.com/SYSTRAN/faster-whisper · https://pypi.org/project/faster-whisper/ · https://github.com/SYSTRAN/faster-whisper/issues/1086 (CUDA/cuDNN compat)
- Windows CUDA DLL fix (Purfview libs): https://github.com/Purfview/whisper-standalone-win/releases
- HF cache / download UX: https://huggingface.co/docs/huggingface_hub/en/guides/manage-cache · https://huggingface.co/docs/huggingface_hub/en/package_reference/file_download
- macOS GPU path (pywhispercpp/whisper.cpp): https://github.com/absadiki/pywhispercpp · https://github.com/ggml-org/whisper.cpp
- customtkinter packaging (onedir): https://customtkinter.tomschimansky.com/documentation/packaging/ · https://github.com/TomSchimansky/CustomTkinter/discussions/939
- PyInstaller: https://pyinstaller.org/en/stable/usage.html
- pynput limits / Wayland / side-buttons: https://pynput.readthedocs.io/en/latest/limitations.html · https://github.com/moses-palmer/pynput/issues/628 · /issues/301
- Linux input (evdev/ydotool/wtype) ref impls: https://github.com/bhargavchippada/faster-whisper-dictation · https://github.com/peteonrails/voxtype · https://handy.computer/docs/paste-methods
- macOS notarization (notarytool): https://github.com/Apple-Actions/import-codesign-certs · https://federicoterzi.com/blog/automatic-code-signing-and-notarization-for-macos-apps-using-github-actions/
- GitHub Actions macOS runner changes: https://github.blog/changelog/2025-09-19-github-actions-macos-13-runner-image-is-closing-down/ · https://github.com/actions/runner-images/issues/13046
- Windows signing (Azure Trusted Signing): https://github.com/Azure/trusted-signing-action · https://azure.microsoft.com/en-us/pricing/details/trusted-signing/ · https://learn.microsoft.com/en-us/windows/apps/package-and-deploy/smartscreen-reputation
- Linux package formats: https://ostechnix.com/linux-package-managers-compared-appimage-vs-snap-vs-flatpak/
- Auto-update (tufup): https://github.com/dennisvang/tufup · https://github.com/dennisvang/tufup-example
- Project structure / build backend / Ruff: https://packaging.python.org/en/latest/specifications/pyproject-toml/ · https://build.pypa.io/en/latest/explanation/build-backends.html · https://github.com/astral-sh/ruff-pre-commit
- License verification: https://github.com/SYSTRAN/faster-whisper/blob/master/LICENSE · https://github.com/TomSchimansky/CustomTkinter/blob/master/LICENSE · https://raw.githubusercontent.com/moses-palmer/pynput/master/COPYING.LGPL
- Release action: https://github.com/softprops/action-gh-release
```
