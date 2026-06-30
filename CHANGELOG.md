# Changelog

All notable changes to VoiceFlow are documented here. This project adheres to
[Semantic Versioning](https://semver.org/) and the
[Keep a Changelog](https://keepachangelog.com/) format.

## [1.0.0] - 2026-06-14

Initial release. Free, local, offline speech-to-text dictation for Windows.

### Added

- **Local dictation engine** (`voiceflow.engine.DictationEngine`): press a
  trigger, speak, and the transcript is pasted into the focused app. Everything
  runs locally with [faster-whisper](https://github.com/SYSTRAN/faster-whisper);
  fully offline after the model downloads once.
- **GPU acceleration with CPU fallback.** Uses an NVIDIA GPU (`int8_float16`)
  when available, otherwise the CPU (`int8`). CUDA DLLs are registered on `PATH`
  before faster-whisper is imported so inference finds `cublas`/`cudnn`. A
  one-click "Enable GPU acceleration" flow installs the cuBLAS/cuDNN runtime
  wheels on demand.
- **Clipboard-preserving paste.** Saves and restores the existing clipboard in
  all formats (Unicode text, `CF_DIB` images, `CF_HDROP` files, HTML/RTF) around
  a 40-byte-correct `SendInput` Ctrl+V.
- **Flexible triggers.** Keyboard combos (suppressed), single mouse buttons with
  suppression via a low-level `WH_MOUSE_LL` hook (middle, `x1`, `x2`), and the
  left+right chord. A live trigger picker reports what was detected and warns
  about driver-only (undetectable) and conflict-prone choices.
- **Hardware-aware model recommender.** `detect_hardware()` probes GPU/VRAM, CPU,
  RAM, and OS; `recommend_models()` suggests the best model for the machine.
- **Model catalog & manager** (`tiny.en`, `base.en`, `small.en`,
  `distil-small.en`, `medium.en`, `distil-large-v3`, `large-v3`) with download
  (retry up to 10x), delete, switch, and on-disk size reporting.
- **customtkinter GUI**: onboarding (hardware scan -> recommendation -> download
  -> optional GPU enable), a dashboard (status, model/device, active trigger,
  last-transcript preview, start/pause, mic meter), a trigger picker, and
  settings (model manager, trigger, toggles, autostart, open log folder, about).
- **System tray** with Open / Pause / Quit and a state-colored icon.
- **Background runtime** (`app.py --background`): headless tray-only dictation
  reading the saved config; used by autostart-at-login. Guarded by a
  machine-wide single-instance mutex (`Global\VoiceFlowSingleton`).
- **Per-user data dir.** Config, log, and models live under
  `%LOCALAPPDATA%\VoiceFlow` (no admin rights; survives upgrades). Config is
  type/range-coerced on load so a bad edit can't brick startup; legacy `hotkey`
  is migrated to `trigger`.
- **Hallucination filter, VAD, transcript cleanup,** and a rotating log.
- **Packaging:** PyInstaller spec (onedir, windowed) and an Inno Setup installer
  (per-user, optional Desktop shortcut and start-at-login). Models and CUDA
  libraries are fetched on demand, not bundled, to keep the installer small.
- **Static marketing website** under `website/`.

[1.0.0]: #
