# Third-Party Licenses

VoiceFlow itself is released under the [MIT License](LICENSE). It is built on a
number of open-source projects, listed here with their licenses and any
compliance obligations they carry.

VoiceFlow is distributed as an application: dependencies are installed alongside
it (PyInstaller `--onedir` keeps each dependency as a separate, importable file —
**not** statically linked or vendored), and the multi-gigabyte speech-model
weights and the optional CUDA runtime are downloaded on demand rather than
bundled.

---

## ⚠️ Copyleft (LGPL) dependencies — read this

Two dependencies in the VoiceFlow ecosystem are licensed under the
**GNU Lesser General Public License v3.0 (LGPL-3.0)**. The LGPL permits use
inside a permissively-licensed (MIT) application, **provided** the project:

1. does **not** vendor (copy into the source tree) or modify the library's
   source code;
2. documents the library as LGPL-3.0 (this file); and
3. ships it in a form the end user can **replace or relink** with their own
   build of the library (LGPL §4). PyInstaller's `--onedir` mode satisfies this:
   each dependency remains a separate file on disk that the user can swap. (If
   VoiceFlow ever moves to `--onefile`, the relink path must be documented — one
   more reason `--onedir` is the correct packaging choice.)

VoiceFlow meets all three conditions: these libraries are installed unmodified
from PyPI and are never copied into or altered within this repository.

| Dependency | License | Role | Status in VoiceFlow |
|---|---|---|---|
| **pystray** | **LGPL-3.0** | System-tray icon (background runtime) | Used **unmodified** on Windows; separately installed; replaceable |
| **pynput** | **LGPL-3.0** | Global hotkey/input listener (planned macOS + Linux backends) | Used **unmodified** when present; separately installed; replaceable |

> **Note on the current Windows build:** VoiceFlow's verified Windows backend
> implements its own input hooks (a low-level `WH_MOUSE_LL`/keyboard hook via
> `ctypes` + the MIT-licensed `keyboard`/`mouse` helper libraries) and does
> **not** ship `pynput`. `pynput` is the planned hotkey library for the
> cross-platform (macOS / Linux X11) backends described in
> [`docs/PRODUCTION_PLAN.md`](docs/PRODUCTION_PLAN.md); the same LGPL compliance
> note above applies to it the moment those backends ship. The tray library,
> **pystray** (by the same author as pynput), is the LGPL dependency present in
> the build today and is handled identically.
>
> If LGPL friction ever becomes unacceptable, both libraries can be swapped for
> MIT/BSD equivalents (a native tray implementation; `evdev` + native
> `SendInput`/`CGEvent` for input) without affecting VoiceFlow's MIT license.

---

## Permissively-licensed dependencies (MIT / BSD / Apache-2.0 / PSF)

These impose no copyleft obligation. Their license and copyright notices are
retained in their distributed packages; the obligation is attribution, which
this file provides.

### Speech / ML core

| Dependency | License | Notes |
|---|---|---|
| faster-whisper | MIT | CTranslate2 Whisper inference |
| CTranslate2 | MIT | Inference engine (CPU baseline; GPU via on-demand CUDA wheels) |
| OpenAI Whisper (model code + weights) | MIT | Model architecture and weights, downloaded on first run; attributed in the About box |
| tokenizers | Apache-2.0 | Hugging Face tokenizers |
| onnxruntime | MIT | Silero VAD backend |
| huggingface-hub | Apache-2.0 | First-run model download |
| av (PyAV) | BSD-3-Clause | Audio decode (bundles FFmpeg, LGPL/GPL components — see below) |
| numpy | BSD-3-Clause | Array math |

### Audio / input / clipboard / GUI / tray

| Dependency | License | Notes |
|---|---|---|
| sounddevice | MIT | Microphone capture (bundles the MIT-licensed PortAudio) |
| keyboard | MIT | Keyboard-combo triggers (Windows) |
| mouse | MIT | Mouse helper (Windows) |
| pyperclip | BSD-3-Clause | Clipboard text fallback |
| pywin32 | PSF (Python Software Foundation License) | `win32clipboard` all-format save/restore, `SendInput`, Win32 APIs |
| customtkinter | MIT | GUI toolkit |
| darkdetect | BSD-3-Clause | System theme detection (customtkinter runtime dep) |
| Pillow | MIT-CMU (HPND) | Tray-icon and asset images |

### Utilities / updater

| Dependency | License | Notes |
|---|---|---|
| platformdirs | MIT | Per-OS config/cache directories |
| packaging | Apache-2.0 OR BSD-2-Clause | Version comparison for the update checker |
| requests | Apache-2.0 | GitHub Releases API (update checker) |
| psutil *(optional)* | BSD-3-Clause | Better CPU/RAM detection (graceful fallback if absent) |
| nvidia-ml-py *(optional)* | BSD-3-Clause | NVML GPU detection (falls back to `nvidia-smi`) |

### Planned cross-platform backends (macOS / Linux)

| Dependency | License | Notes |
|---|---|---|
| pyobjc | MIT | macOS `NSPasteboard` / `CGEvent` |
| python-evdev | BSD-3-Clause (revised) | Linux `/dev/input` hotkeys (X11 + Wayland) |
| pywhispercpp *(optional)* | MIT | macOS Metal acceleration backend |

---

## On-demand / runtime-fetched components (NOT bundled)

These are downloaded by the user at runtime, not shipped in the installer:

| Component | License | When fetched |
|---|---|---|
| Whisper model weights (`Systran/faster-whisper-*`) | MIT | First run |
| NVIDIA cuBLAS (`nvidia-cublas-cu12`) | NVIDIA Software License (proprietary) | Only on GPU opt-in |
| NVIDIA cuDNN (`nvidia-cudnn-cu12`) | NVIDIA Software License (proprietary) | Only on GPU opt-in |

> The NVIDIA CUDA runtime libraries are **proprietary** and are governed by the
> NVIDIA Software License Agreement. They are **never** redistributed by
> VoiceFlow; they are fetched from NVIDIA's official PyPI wheels only after the
> user explicitly enables GPU acceleration.

## A note on bundled FFmpeg (via PyAV)

PyAV (`av`) bundles FFmpeg, which contains components under LGPL-2.1+ (and, in
some builds, GPL). PyAV's prebuilt wheels ship an LGPL FFmpeg build. As with the
other LGPL components, FFmpeg is used unmodified and as a separate shared
library; its source is available from the FFmpeg project.

---

*Full license texts for each dependency are available in the corresponding
package distribution (e.g. `site-packages/<package>-<version>.dist-info/`) and
on each project's homepage. If you spot a licensing error or omission, please
[open an issue](https://github.com/your-org/voiceflow/issues).*
