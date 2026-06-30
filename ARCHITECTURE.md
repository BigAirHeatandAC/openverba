# Architecture

This is the map a newcomer needs to find their way around VoiceFlow. The code
lives under `src/voiceflow/` (a `src/` layout package named `voiceflow`; the
public brand is OpenVerba). Every file is heavily docstring'd — this document
links the pieces together so you know *where* to look.

> Design rule of the whole project: **the engine and GUI talk to OS-specific code
> only through the platform-abstraction layer** (the ABCs in
> `platform/base.py`). Nothing outside `platform/` imports a Windows/macOS/Linux
> module directly. Adding a new OS = dropping in a sibling module that implements
> the same surface.

---

## Top-level data flow

```
                 key press / mouse button (trigger)
                                |
        platform trigger hook thread  (must stay microscopic: flip state,
                                |       enqueue a command, return < ~300ms)
                                v
   +--------------------------------------------------------------+
   |  DictationEngine state machine   (engine.py)                 |
   |                                                              |
   |   IDLE  --trigger-->  RECORDING  --trigger/silence-->  TRANSCRIBING |
   |     ^                     |                                |  |
   |     |                sounddevice mic                       |  |
   |     |                (16 kHz mono)                         |  |
   |     |                     |                                v  |
   |     |               np.concatenate            faster-whisper decode |
   |     |                                          (GPU int8_float16    |
   |     |                                           else CPU int8)      |
   |     |                                                |             |
   |     |                              hallucination filter + cleanup  |
   |     |                                                |             |
   |     +-------------------- restore clipboard <--- paste (SendInput  |
   |                            (original data)         Ctrl+V)         |
   +--------------------------------------------------------------+
```

Batch path: **key press → record → transcribe → paste → restore clipboard.**
Streaming path (optional): confirmed words are *typed live* (no clipboard) as
you speak; see [Engine](#engine-batch-vs-streaming).

All heavy work (stream teardown, `np.concatenate`, transcription, paste) runs on
the engine's control/worker threads. Engine callbacks (`on_state`,
`on_transcript`, `on_log`, `on_level`) fire on background threads; the GUI
marshals them back onto the UI thread.

---

## Platform-abstraction layer

The architectural backbone. Wayland vs X11, macOS TCC permissions, and Windows
hooks make a single cross-platform library impossible, so each OS supplies a
module implementing fixed interfaces.

- **`platform/base.py`** — the ABCs (`HotkeyBackend`, `MouseBackend`,
  `ClipboardBackend`, `Paster`, `Typer`, `Permissions`, `TriggerBackend`, plus
  the `TriggerHandle` return type). Signatures are fixed; the engine talks only
  to these.
- **`platform/__init__.py`** — the **factory**. `detect_platform()` returns
  `"windows" | "macos" | "linux-wayland" | "linux-x11"`; `make_backends()`,
  `make_trigger_backend()`, `make_clipboard()`, `make_typer()`,
  `make_tap_hold_chord()`, `make_tap_hold_keyboard()`, `make_multi_chord()`,
  `make_permissions()`, and `diagnostics()` return the concrete backend for the
  detected OS.
- **`platform/windows.py`** — the **verified** backend, and the largest module.
  Holds the hard-won Windows specifics: the 40-byte `SendInput` `INPUT` struct
  with explicit `argtypes`, the Ctrl+V SendInput modifier-release dance,
  all-format clipboard save/restore (`CF_DIB`/`CF_DIBV5`/`CF_HDROP`/HTML/RTF), the
  `WH_MOUSE_LL` low-level mouse hooks (single-button suppression + left+right
  chord hold-and-forward), tap/hold (mouse and keyboard), the unicode `Typer` for
  streaming, and the trigger classifier/recorder.
- **`platform/macos.py`**, **`platform/linux_x11.py`**,
  **`platform/linux_wayland.py`**, **`platform/linux_common.py`** — the
  cross-platform backends (per `docs/PRODUCTION_PLAN.md`): pyobjc NSPasteboard +
  pynput on macOS; evdev + xclip/xdotool on X11; wl-clipboard + ydotool/wtype on
  Wayland.
- **`triggers.py`** — platform-neutral trigger-UX helpers re-exported from the
  active backend (`classify_trigger`, `PRESETS`, `TriggerRecorder`,
  `register_trigger`) so the GUI never imports an OS module directly.

---

## Engine (batch vs streaming)

- **`engine.py`** — `DictationEngine`: the `IDLE → RECORDING → TRANSCRIBING`
  state machine, mic capture, worker threads, VAD, the hallucination filter,
  model loading with a GPU→CPU fallback ladder, and the paste cycle (via the
  platform clipboard). It registers the CUDA DLL dirs **before** importing
  `faster_whisper` (critical ordering — see [CUDA](#cuda)). Also contains the
  `Beeper` (start/stop tones) and trigger registration that wires taps/holds to
  the right handler. Construct it with the config + optional GUI callbacks.
- **`streaming.py`** — `StreamingSession`: append-only, flicker-free live
  transcription using a **LocalAgreement-2** strategy (only commit a word once
  two consecutive decode passes agree on it), plus the end-of-utterance VAD
  thresholds. Confirmed words are typed live through the platform `Typer`.
  Detailed design in `docs/STREAMING_DESIGN.md`.

## Triggers / tap-hold

Trigger registration lives in the engine (`engine.py`, the trigger-registration
section) and delegates to the platform factory:

- A **keyboard combo** or **single mouse button** or **left+right chord** is
  registered through `make_trigger_backend()`.
- A **tap/hold** trigger is registered through `make_tap_hold_chord()` /
  `make_tap_hold_keyboard()`: a quick tap toggles dictation; a hold starts/ends
  voice-editing of the current selection.
- The trigger callback runs on a low-level hook thread and must return almost
  instantly — it only flips state and enqueues a command.

## Commands + AI editing

- **`commands.py`** — a small, predictable spoken-command parser. In batch mode
  each finalized utterance is checked here first: if the **whole** utterance is a
  recognized command (`select all`, `backspace five`, `press enter`, …) it is
  executed as keystrokes via the platform `Typer`; otherwise it's typed as normal
  dictation. Spoken or digit numbers, optional wake word, synonyms.
- **`ai.py`** — optional LLM-powered text editing. A command-mode instruction
  that is *not* a mechanical key action ("make this shorter", "fix the grammar")
  rewrites the selected text. Default backend is a **local Ollama** model (free,
  private, offline); pluggable to anthropic/openai with an API key. Stdlib-only
  (urllib/json).
- **`ai_setup.py`** — first-run helper to detect/install/pull a local Ollama
  model with progress callbacks.

## Updater

- **`updater.py`** — checks `openverba.com/latest.json` for a newer build,
  downloads the per-user installer to `%TEMP%`, **verifies its SHA-256 against the
  manifest (mandatory)**, then spawns the installer detached and exits so file
  locks + the single-instance mutex release and Inno can upgrade the onedir in
  place. Stdlib-only so it imports from both the headless runtime and the GUI;
  never blocks startup or raises into callers.

## Config / constants / data dir / hardware

- **`constants.py`** — `APP_NAME`, the per-user data dir (`%LOCALAPPDATA%\VoiceFlow`
  on Windows) and the derived `CONFIG_PATH` / `LOG_PATH` / `MODELS_DIR` /
  `RECORDINGS_DIR`, the state names (`IDLE`/`RECORDING`/`TRANSCRIBING` +
  `STATE_LABELS`), the single-instance mutex name, and `DEFAULT_CONFIG`. Data
  lives in the per-user profile, never the install dir.
- **`config.py`** — `load_config()` / `save_config()` / `resolve_download_root()`.
  Every value is type/range-coerced on load (`_coerce_config`), so a bad manual
  edit can never brick startup; the legacy `hotkey` key is migrated to `trigger`.
- **`hardware.py`** — `detect_hardware()` (GPU/VRAM/CPU/RAM/OS, with graceful
  fallbacks: NVML → nvidia-smi → stdlib/ctypes) and `recommend_models()` (the
  VRAM-bracketed model recommendations the onboarding shows).
- **`models.py`** — `MODEL_CATALOG` plus download/install/delete/verify state
  (snapshot completeness checks, HF cache resolution).
- **`debuglog.py`** — optional debug capture of audio + paired transcripts
  (JSONL) when `save_recordings` is on.

## CUDA

- **`cuda.py`** — `register_cuda_dlls()` prepends the pip `nvidia-*` wheel `bin`
  dirs to `PATH` in a fixed order (cuBLAS first, then cuDNN, then the rest) so
  transitive `LoadLibraryA` resolution finds them — otherwise inference dies with
  *"cublas64_12.dll not found"*. Also `gpu_runtime_present()` and
  `install_gpu_runtime()` for the opt-in GPU flow.
- **`_cuda_shim.py`** — a tiny shim imported **first** (before anything pulls in
  faster-whisper) to run the DLL registration; falls back to importing the nvidia
  wheels directly if `voiceflow.cuda` fails.

## Entry points

- **`__main__.py`** — `python -m voiceflow`. Imports `_cuda_shim` first, then
  dispatches: `--version`, `--background`/`--headless` → `cli.run_background()`,
  else → `app.run_gui()`. Works both as a module and as the frozen PyInstaller
  entry script.
- **`app.py`** — the GUI entry (`run_gui()` / the `voiceflow` gui-script). Sets up
  rotating-file logging into the per-user data dir, then launches
  `voiceflow.ui.main_window.launch()`. Does not import the engine at import time.
- **`cli.py`** — the headless/background runtime (the `voiceflow-cli` console
  script). Single-instance named mutex (two runtimes would race on the clipboard
  and both grab the trigger), engine + system tray (Open / Pause / Check for
  updates / Quit), and the once-a-day notify-only update poll.

## GUI

customtkinter, under `ui/`. A controller owns the engine; views are passive.

- **`ui/main_window.py`** — `VoiceFlowApp` + `launch()`. Owns the root window, the
  config dict, and the `DictationEngine`. Runs engine work on background threads
  and marshals every callback back to the UI thread via `.after()`. Swaps between
  Onboarding (first run) → Dashboard ↔ Settings; tray + close-to-tray.
- **`ui/dashboard.py`** — `DashboardView`: status indicator, active model/device,
  current trigger, last-transcript preview, Start/Pause toggle, mic level meter.
- **`ui/settings.py`** — `SettingsView`: model manager (download/delete/switch),
  trigger picker entry, GPU/AI setup dialogs, update check. (Largest UI file.)
- **`ui/onboarding.py`** + **`ui/first_run.py`** — the first-run wizard (welcome →
  hardware scan → model recommendation → download → optional GPU → done) and
  permission checks (mostly a no-op on Windows).
- **`ui/trigger_picker.py`** — the live "press your trigger" dialog (uses
  `triggers.TriggerRecorder` + `classify_trigger` + `PRESETS`).
- **`ui/tray.py`**, **`ui/autostart.py`**, **`ui/widgets.py`**, **`ui/theme.py`**
  — tray icon, start-at-login wiring, shared widgets (Card/Badge/StatRow/
  LevelMeter/buttons), and the single-source theme (palette + fonts).

## Packaging

The Windows artifact only (PyInstaller can't cross-compile; macOS/Linux build on
their own native runners).

- **`packaging/voiceflow.spec`** — the PyInstaller spec (entry =
  `src/voiceflow/__main__.py`) producing a **onedir, windowed** bundle. Excludes
  torch/nvidia/`*-gpu` as belt-and-braces so the multi-GB CUDA runtime can never
  be packed.
- **`packaging/installer.iss`** — the Inno Setup script. Per-user install to
  `%LOCALAPPDATA%\Programs\VoiceFlow` (no admin), AppId-keyed for in-place
  upgrades, output `OpenVerba-Setup-<ver>.exe`, optional start-at-login Run entry.
- **`packaging/version_info.txt`** — Windows `.exe` version resource.
- **`packaging/macos/`**, **`packaging/linux/`** — Info.plist/entitlements and the
  AppImage build script + `.desktop` file for the other OSes.
- **`scripts/build.bat`** — one command: activate venv → install CPU-only runtime
  deps + PyInstaller → `pip install -e .` → run PyInstaller →
  `dist/VoiceFlow/VoiceFlow.exe` → (if `ISCC` present) compile the installer →
  `dist/OpenVerba-Setup-<ver>.exe` → run `scripts/make_manifest.py` to emit
  `website/latest.json` and stage the exe into `website/download/`.
- **`scripts/make_manifest.py`** — emits `website/latest.json` (version, url,
  sha256, size, notes, pub_date) that installed apps poll. The version is the
  single source of truth in `src/voiceflow/__init__.py`.
- **`scripts/run_dev.bat`** — run from source via the venv (`python -m voiceflow`).
