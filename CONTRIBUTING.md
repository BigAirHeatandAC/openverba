# Contributing to VoiceFlow

Thanks for your interest in improving VoiceFlow — a free, fully local/offline
speech-to-text dictation app (published publicly as **OpenVerba**; see
[the name note](#the-voiceflow--openverba-name)). Contributions of all kinds are
welcome: bug reports, fixes, docs, tests, and platform support (Windows, macOS,
Linux X11 + Wayland).

By participating you agree to abide by our
[Code of Conduct](CODE_OF_CONDUCT.md).

New here? Read [ARCHITECTURE.md](ARCHITECTURE.md) first for the module map.

---

## Table of contents

- [The VoiceFlow / OpenVerba name](#the-voiceflow--openverba-name)
- [Development setup](#development-setup)
- [Running the app](#running-the-app)
- [Tests, lint, and type checks](#tests-lint-and-type-checks)
- [Pre-commit hooks](#pre-commit-hooks)
- [Coding guidelines](#coding-guidelines)
- [Adding a platform backend](#adding-a-platform-backend)
- [Releases and the updater manifest](#releases-and-the-updater-manifest)
- [Branch & PR conventions](#branch--pr-conventions)
- [Licensing of contributions](#licensing-of-contributions)

---

## The VoiceFlow / OpenVerba name

The project is published publicly as **OpenVerba** (the original "VoiceFlow"
name collides with a funded company, Voiceflow Inc.). The **code internals are
still named VoiceFlow** — the Python package is `voiceflow`, the data dir is
`%LOCALAPPDATA%\VoiceFlow`, the portable exe is `VoiceFlow.exe`, and most
docstrings say "VoiceFlow". The user-facing brand (website, installer filename
`OpenVerba-Setup-<ver>.exe`, update notifications) says **OpenVerba**. A full
rename is pending; until then, treat the two names as the same app and don't be
surprised by the mix.

## Development setup

Prereqs: Python 3.10+ (3.11 is the validated version) on Windows 10/11.

```bash
# 1. Clone
git clone https://github.com/your-org/voiceflow      # replace with the real repo URL
cd voiceflow

# 2. Create and activate a virtual environment
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux

# 3. Editable install (pulls in runtime deps from pyproject.toml) + dev tools
pip install -e .
pip install pytest pytest-cov ruff mypy pre-commit pyinstaller
```

`requirements.txt` lists the exact runtime versions validated in the project's
Windows venv; `scripts/build.bat` installs from it for the release build. The
runtime dependencies are also declared in `pyproject.toml`, so `pip install -e .`
is enough to run and test.

> **GPU (NVIDIA, optional):** `pip install -e ".[gpu]"` adds the cuBLAS/cuDNN
> wheels. These are **never** committed or bundled — the CPU build of ctranslate2
> runs without them, and the app fetches the GPU runtime on demand only when the
> user opts in. See `docs/PRODUCTION_PLAN.md` and `src/voiceflow/cuda.py`.
>
> **Linux** backends shell out to native tools — install `xclip`, `wl-clipboard`,
> `libportaudio2`, and (for Wayland paste) `ydotool` + `ydotoold` or `wtype`.

## Running the app

```bash
python -m voiceflow                 # GUI (default) — onboarding on first run, then dashboard
python -m voiceflow --background    # headless dictation runtime (tray only)
python -m voiceflow --version
```

After `pip install -e .` the console scripts also work: `voiceflow` (GUI) and
`voiceflow-cli` (headless/debug, console attached — best for bug reports).

`voiceflow._cuda_shim` must import **before** `faster_whisper`; this is wired
into `__main__.py`, `app.py`, and `cli.py`, so always launch through one of the
entry points above. On Windows, `scripts\run_dev.bat` runs `python -m voiceflow`
through the project venv and forwards any args.

## Tests, lint, and type checks

The suite has **159 passing tests** in `tests/`. Run the full quality gate
locally before opening a PR — it mirrors CI:

```bash
ruff check .            # lint (config in pyproject.toml: line-length 100, rule set E/F/I/UP/B/SIM/C4/PTH/RUF)
ruff format --check .   # formatting (drop --check to auto-format)
mypy src                # static types (strict mode)
pytest                  # the 159 tests
```

If you use the project venv directly, prefix with its interpreter, e.g.
`C:\Users\shaha\voiceflow\venv\Scripts\python.exe -m pytest`.

- Tests are **OS-agnostic** where possible — they mock `sys.platform`,
  environment variables, the clipboard, and the CTranslate2 backend rather than
  requiring real hardware. See `tests/conftest.py` and existing suites
  (`test_platform_factory`, `test_engine_fallback`, `test_streaming`,
  `test_commands`, `test_taphold`, `test_multichord`, `test_models`,
  `test_dictation`, `test_ai`, `test_updater`).
- **New code should come with tests.** Bug fixes should add a regression test.
- **Type hints are required** on new/changed code; mypy runs in `strict` mode.

## Pre-commit hooks

Install the hooks once; they then run on every commit:

```bash
pre-commit install
pre-commit run --all-files   # run against the whole tree on demand
```

The hook order is fixed in `.pre-commit-config.yaml`: `ruff-check --fix` →
`ruff-format` → `mypy` → hygiene hooks (trailing whitespace, end-of-file,
merge-conflict / TOML / YAML checks).

## Coding guidelines

- Target **Python 3.10+**; code lives under `src/voiceflow/`.
- **Follow the platform-abstraction layer.** OS-specific code belongs in
  `src/voiceflow/platform/<os>.py` behind the ABCs in `platform/base.py`, reached
  through the factory in `platform/__init__.py`. The engine and `ui/` stay
  OS-agnostic — they never import an OS module directly.
- **Never hard-fail for lack of a GPU.** Preserve the engine fallback ladder
  (CUDA float16 → CUDA int8_float16 → CPU int8).
- **Do not commit or bundle** model weights or the CUDA runtime — they are
  fetched on demand. Don't commit `build/`, `dist/`, or large `.exe` binaries.
- Respect ruff (`line-length = 100`) and mypy strict. Comments explain *why*,
  not *what*.
- **Preserve the hard-won Windows behaviors** (see the engine/`platform/windows.py`
  docstrings and `docs/PRODUCTION_PLAN.md`): the 40-byte `SendInput` `INPUT`
  struct with explicit `argtypes`, CUDA DLL `PATH` registration before importing
  faster-whisper, all-format clipboard save/restore, the `WH_MOUSE_LL` hooks
  (left+right chord hold-and-forward; suppressed side buttons; only suppress an UP
  if its DOWN was suppressed), the single-instance mutex, the hallucination
  filter, and the threaded `IDLE/RECORDING/TRANSCRIBING` state machine. If you
  touch these, add/extend a test and explain the change in the PR.

## Adding a platform backend

The whole point of `platform/` is that a new OS is a drop-in module. To add (or
improve) one:

1. Create `src/voiceflow/platform/<os>.py` (e.g. extend `linux_wayland.py`).
2. Implement the ABCs from `platform/base.py`: `HotkeyBackend`, `MouseBackend`,
   `ClipboardBackend`, `Paster`, `Typer`, `Permissions`, and the combined
   `Triggers` (`TriggerBackend`). Provide the module-level functions the factory
   looks up: `Hotkeys`, `Mouse`, `Clipboard`, `PasterImpl`, `PermissionsImpl`,
   `Triggers`, `make_clipboard`, and (optionally) `make_typer`,
   `register_chords`, `register_tap_hold`, `register_tap_hold_keyboard`,
   `register_trigger`, `classify_trigger`, `PRESETS`, `TriggerRecorder`,
   `diagnostics`.
3. Wire it into `platform/__init__.py` `detect_platform()` / `_backend_module()`
   if it's a new OS/session type.
4. Add diagnostics, not silent failures (e.g. "`/dev/input` not readable").
5. **State which OS/session you tested on in the PR.** Much cross-platform
   behavior can't be exercised on a single OS; lean on the CI matrix for
   build-time validation and on `tests/test_platform_factory.py` for the factory
   contract.

## Releases and the updater manifest

Releases are SemVer; the version is the **single source of truth** in
`src/voiceflow/__init__.py` (`__version__`), read by Hatchling, `build.bat`, and
`installer.iss`.

The Windows build + manifest flow (one command, `scripts\build.bat`):

1. Activate the venv, install CPU-only runtime deps (`requirements.txt`) +
   PyInstaller, then `pip install -e .`.
2. Run PyInstaller against `packaging/voiceflow.spec` → `dist/VoiceFlow/VoiceFlow.exe`
   (onedir, windowed).
3. If Inno Setup's `ISCC` is on `PATH`, compile `packaging/installer.iss` →
   `dist/OpenVerba-Setup-<ver>.exe`.
4. Run `scripts/make_manifest.py` to compute the installer's SHA-256 and write
   **`website/latest.json`** (`version`, `url`, `sha256`, `size`, `notes`,
   `pub_date`, optional `mandatory`/`min_version`), then copy the installer into
   `website/download/`.

`latest.json` is what installed apps poll (`updater.py` fetches
`openverba.com/latest.json`, **verifies the SHA-256 before launching anything**,
then hands off to Inno for an in-place upgrade). Keep `website/latest.json`
tracked in git; do **not** commit the large `website/download/*.exe` binaries —
publish those via GitHub Releases / the CDN. macOS (`.app`/`.dmg`) and Linux
(`.AppImage`) artifacts are built on their own native runners (PyInstaller can't
cross-compile). See `RELEASING.md` for the full checklist.

## Branch & PR conventions

1. **Branch off `main`** with a prefixed name: `feat/…`, `fix/…`, `docs/…`,
   `refactor/…`, `test/…`, `chore/…`, `ci/…` (e.g. `fix/clipboard-restore-race`).
2. **Keep PRs focused** — one logical change each.
3. **Before pushing**, ensure lint/format/types/tests all pass locally.
4. **Open a PR against `main`** and fill in the
   [pull request template](.github/PULL_REQUEST_TEMPLATE.md): describe the change,
   link the issue (`Closes #123`), confirm the checklist, and note platform
   implications.
5. **Update `CHANGELOG.md`** under `[Unreleased]` for any user-visible change.
6. **CI must be green** (lint/type/test matrix across Windows/macOS/Linux ×
   Python 3.10–3.13).

Commit messages follow lightweight
[Conventional Commits](https://www.conventionalcommits.org/):
`<type>(<scope>): <summary>` — e.g.
`feat(platform): add Linux Wayland paste backend (ydotool → wtype)`.

## Licensing of contributions

VoiceFlow is **MIT-licensed**. By submitting a contribution you agree it is
licensed under the MIT License and that you have the right to contribute it.

Be mindful of dependency licenses: do **not** vendor or modify any LGPL
dependency (`pystray`, and the planned `pynput`), and add any new dependency to
[`THIRD-PARTY-LICENSES.md`](THIRD-PARTY-LICENSES.md). Prefer MIT/BSD/Apache-2.0;
flag anything copyleft in your PR.
