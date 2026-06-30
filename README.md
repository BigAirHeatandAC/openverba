# VoiceFlow

**Free, private, offline voice typing for your desktop â€” a local Wispr Flow clone.**

> Brand note: the project ships publicly as **OpenVerba**; the code, package, and
> internal names still say **VoiceFlow** (a rename is in progress). They are the
> same app. See [CONTRIBUTING.md](CONTRIBUTING.md#the-voiceflow--openverba-name).

VoiceFlow is a local speech-to-text dictation app. Press a trigger, speak, and
your words are typed into whatever app is focused. Everything runs on your own
machine: the audio never leaves your PC, and after the speech model downloads
once, VoiceFlow works fully offline. It is free and MIT-licensed.

It uses [faster-whisper](https://github.com/SYSTRAN/faster-whisper) for
transcription, automatically using your NVIDIA GPU when one is available and
falling back to the CPU otherwise, picks a model that fits your hardware, and
restores whatever was on your clipboard after it pastes â€” so dictation never
clobbers your copy buffer.

<!-- TODO(screenshot): add docs/screenshot-dashboard.png and embed it here. -->
<!-- ![VoiceFlow dashboard](docs/screenshot-dashboard.png) -->
*(Screenshot placeholder â€” a dashboard screenshot will go here.)*

---

## Table of contents

- [Key features](#key-features)
- [How it works (in three sentences)](#how-it-works-in-three-sentences)
- [Install (Windows)](#install-windows)
- [Run from source](#run-from-source)
- [Choosing a model](#choosing-a-model)
- [Triggers, voice editing & voice commands](#triggers-voice-editing--voice-commands)
- [Privacy](#privacy)
- [Where your files live](#where-your-files-live)
- [Documentation](#documentation)
- [License](#license)

---

## Key features

- **100% local & offline.** Audio is transcribed on your PC with faster-whisper.
  Nothing is sent to any server; after the model downloads once, no internet is
  required (set `local_files_only` to enforce it).
- **Works in any app.** VoiceFlow types into the focused window (editor, browser,
  chat, terminal) by pasting, so it works everywhere a normal paste works.
- **Live streaming dictation.** An append-only streaming mode types confirmed
  words as you speak (flicker-free, via a LocalAgreement-2 strategy) instead of
  waiting for you to finish.
- **Tap-to-talk and hold-to-edit.** A single trigger can do double duty: a quick
  **tap** toggles dictation, while a **hold** enters voice-editing mode for the
  text you currently have selected.
- **Voice commands.** Hands-free editing â€” say "select all", "backspace five",
  "press enter", and more. Non-mechanical instructions ("make this shorter",
  "fix the grammar") are sent to an optional local LLM to rewrite your selection.
- **GPU-accelerated, with a CPU fallback.** Uses an NVIDIA GPU if present for
  near-instant transcription, otherwise the CPU.
- **Clipboard-safe paste.** Your previous clipboard contents (text, images,
  files, HTML/RTF) are saved before pasting and restored right after.
- **Smart model picker.** On first run it scans your hardware (GPU, VRAM, CPU,
  RAM) and recommends the best speech model for your PC.
- **Auto-update.** The app checks for newer builds, verifies the download by
  SHA-256, and hands off to the installer for an in-place upgrade.
- **Free and open source.** MIT licensed.

## How it works (in three sentences)

You press your trigger; VoiceFlow records 16 kHz mono audio from your mic and
runs a tiny `IDLE â†’ RECORDING â†’ TRANSCRIBING` state machine. When you stop, it
transcribes locally with faster-whisper (GPU if available, else CPU), filters out
Whisper's silence "hallucinations", and cleans up the text. It then saves your
current clipboard, pastes the transcript with a SendInput Ctrl+V, and restores
your original clipboard â€” so your copy buffer is never clobbered.

## Install (Windows)

**Recommended: the installer.**

1. Download `OpenVerba-Setup-<version>.exe` from the project's
   [Releases page](https://github.com/BigAirHeatandAC/openverba/releases) *(replace
   with the real repo URL before publishing)*.
2. Run it. VoiceFlow installs **per-user** to
   `%LOCALAPPDATA%\Programs\VoiceFlow` â€” **no administrator rights are needed.**
3. Choose whether to create a Desktop shortcut and whether to start VoiceFlow at
   login.
4. Launch VoiceFlow from the Start Menu. The first run walks you through a
   hardware scan, model download, and (NVIDIA only) optional GPU acceleration.

The installer bundles everything the app needs to start â€” there is no separate
Python install to manage. The speech model is downloaded on first run, and the
optional GPU libraries are fetched only if you enable GPU acceleration.

## Run from source

Prereqs: Windows 10/11, Python 3.10+ (3.11 is the validated version), and a
virtual environment with the runtime dependencies installed.

```bash
# 1. Create and activate a venv
python -m venv .venv
.venv\Scripts\activate

# 2. Install the package (editable) â€” pulls in the runtime deps from pyproject.toml
pip install -e .

# 3. Run it
python -m voiceflow                 # GUI (default; onboarding on first run, then dashboard)
python -m voiceflow --background    # headless dictation runtime (tray only)
python -m voiceflow --version       # print the version and exit
```

After `pip install -e .` the console entry points are also available:

```bash
voiceflow            # GUI (no console window) â€” the gui-script
voiceflow-cli        # headless/debug CLI (console attached; good for bug reports)
```

> Always launch through one of these entry points. `voiceflow._cuda_shim` must
> import **before** `faster_whisper`, and the package's `__main__` wires that up.

On Windows you can also use the convenience scripts (they use the project venv):
`scripts\run_dev.bat` to run from source, and `scripts\build.bat` to produce the
PyInstaller bundle and Inno Setup installer. See
[CONTRIBUTING.md](CONTRIBUTING.md) for the full dev workflow and
[ARCHITECTURE.md](ARCHITECTURE.md) for the module map.

## Choosing a model

VoiceFlow recommends a model automatically, but you can switch any time in
**Settings â†’ Model manager** (download, delete, or switch the active model). The
English models are the `.en`-tuned variants, which are faster and more accurate
for English dictation than the multilingual builds.

| Model | Languages | Disk | VRAM (int8) | Accuracy | Speed | Good for |
|---|---|---|---|---|---|---|
| `tiny.en` | English | ~75 MB | ~350 MB | Low | Fastest | Old / low-power CPUs |
| `base.en` | English | ~145 MB | ~500 MB | Good | Fast | **CPU-only default** |
| `small.en` | English | ~480 MB | ~1.1 GB | High | Fast | **4 GB+ GPU default** |
| `distil-small.en` | English | ~340 MB | ~0.9 GB | High | Fastest | Low-latency English |
| `medium.en` | English | ~1.5 GB | ~2.6 GB | Very high | Medium | 6 GB+ GPU |
| `distil-large-v3` | English | ~1.5 GB | ~2.8 GB | Very high | Fast | Best high-end English |
| `large-v3` | Multilingual | ~3.1 GB | ~4.7 GB | Best | Slow | 6â€“10 GB+ GPU, multilingual |

> **A note on CPU speed:** on the CPU, transcription happens *after* you stop
> speaking, and bigger models mean a longer wait. With no GPU, prefer `base.en`
> (or `tiny.en` on weak machines). A GPU makes the larger, more accurate models
> practical for real-time dictation.

## Triggers, voice editing & voice commands

A **trigger** is what you press to start/stop recording (set it in
**Settings â†’ Trigger**). The picker watches your input live and tells you whether
your choice is *clean* or *conflict-prone*.

- **Keyboard combos** â€” e.g. `f9`, `ctrl+shift+space`, `ctrl+alt+d`. Most
  reliable, and suppressed so they don't leak into the focused app.
- **Single mouse buttons** â€” `mouse:middle`, `mouse:x1`, `mouse:x2` (side/thumb
  buttons). A low-level mouse hook suppresses that button's press so no stray
  action (like browser Back/Forward) leaks through.
- **Left+right chord** â€” `mouse:left+right` (press both buttons together).

**Tap-to-talk / hold-to-edit:** with a tap/hold trigger, a quick tap toggles
dictation while holding the trigger enters **voice-editing** mode for the text
you have selected.

**Voice commands** (hands-free editing): say things like "select all",
"backspace five", "press enter". A spoken instruction that isn't a mechanical
keystroke ("make this shorter", "fix the grammar") is sent to an optional local
LLM (Ollama by default â€” free, private, offline) to rewrite your selection.

> **The mouse caveat.** Some mouse buttons are handled entirely inside the
> mouse's own driver/firmware (dedicated DPI, profile, or RGB buttons). Windows
> never sees those, so VoiceFlow cannot use them â€” the picker shows nothing was
> detected. Pick a standard button (middle or a thumb button) or a function key.

## Privacy

- Audio is captured from your microphone, transcribed **locally** on your CPU or
  GPU, and discarded. It is not uploaded.
- The **only** network access VoiceFlow makes is to download a speech model
  (and, optionally, the GPU runtime and update manifest). After that you can be
  fully offline â€” set `local_files_only` to keep it that way.
- The log file (`voiceflow.log`) records app events for troubleshooting, not your
  transcripts.

## Where your files live

VoiceFlow keeps your data in your per-user profile, **not** in the install folder
(so it survives upgrades and needs no admin rights):

- **Config:** `%LOCALAPPDATA%\VoiceFlow\config.json`
- **Log:** `%LOCALAPPDATA%\VoiceFlow\voiceflow.log`
- **Models:** `%LOCALAPPDATA%\VoiceFlow\models` (override with `download_root`)
- **Program files:** `%LOCALAPPDATA%\Programs\VoiceFlow` (the app itself)

## Documentation

- **[ARCHITECTURE.md](ARCHITECTURE.md)** â€” the module map: platform abstraction,
  the engine (batch vs streaming), triggers/tap-hold, commands + AI editing, the
  updater, config/constants, the GUI, and packaging.
- **[CONTRIBUTING.md](CONTRIBUTING.md)** â€” dev setup, tests, conventions, how to
  add a platform backend, and how releases + the updater manifest work.
- **[docs/PRODUCTION_PLAN.md](docs/PRODUCTION_PLAN.md)** â€” the full design /
  production blueprint.
- **[docs/STREAMING_DESIGN.md](docs/STREAMING_DESIGN.md)** â€” the streaming
  (LocalAgreement-2) design.

## License

[MIT](LICENSE) Â© 2026 VoiceFlow / OpenVerba contributors.
