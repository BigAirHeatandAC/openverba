# Releasing & publishing VoiceFlow

This is the end-to-end checklist to build the installer and put it on the web so
people can download it.

## 1. Build the installer (Windows)

Prereqs (one-time): the dev venv with deps, **PyInstaller**, and **Inno Setup 6**
(`winget install JRSoftware.InnoSetup`).

```bat
scripts\build.bat
```

That runs PyInstaller (from `packaging\voiceflow.spec`) then Inno Setup
(`packaging\installer.iss`) and produces:

```
dist\VoiceFlow\VoiceFlow.exe            <- the app (onedir)
dist\VoiceFlow-Setup-1.0.0.exe          <- the installer people download (~73 MB)
```

To build by hand:
```bat
"<venv>\Scripts\python.exe" -m PyInstaller packaging\voiceflow.spec --noconfirm --clean
"%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" packaging\installer.iss
```

The installer is **per-user** (installs to `%LOCALAPPDATA%\Programs\VoiceFlow`, no
admin prompt), adds a Start Menu shortcut, optionally a desktop shortcut and a
"start at login" entry, and a clean uninstaller. User data (config, logs,
downloaded models) lives in `%LOCALAPPDATA%\VoiceFlow` and survives reinstalls.

## 2. What the user gets on first run

The installer is intentionally small — it does **not** bundle the speech model,
the CUDA GPU libraries, or the AI model. On first launch:

1. Onboarding scans hardware and recommends a Whisper model, then downloads it
   (one time, from Hugging Face).
2. Dictation works immediately (on CPU; GPU is optional — see below).
3. **GPU acceleration** (NVIDIA): Settings -> "Enable GPU acceleration" downloads
   the CUDA runtime. Optional.
4. **Smart AI editing**: Settings -> "Enable smart AI editing" installs Ollama +
   a local model sized to their PC. Optional, free, fully local.

Everything optional degrades gracefully: without GPU it runs on CPU; without the
AI model the mechanical voice commands still work.

## 3. Publish to the web

The `website/` folder is a complete static site. The Download button points at
`download/VoiceFlow-Setup-1.0.0.exe`, and the installer is already copied there,
so the folder is deploy-ready.

**Option A — deploy the whole folder (simplest).** Drop `website/` on any static
host (Vercel, Netlify, GitHub Pages, Cloudflare Pages). The 73 MB installer ships
with it and downloads work immediately.

```bash
# Vercel example
cd website && vercel deploy --prod
```

**Option B — host the binary on GitHub Releases (recommended for big files).**
Keep the installer out of the web repo and attach it to a GitHub Release, then
change the two Download links in `website/index.html` to:
`https://github.com/<your-org>/voiceflow/releases/latest/download/VoiceFlow-Setup-1.0.0.exe`
(and delete `website/download/`).

## 4. Before you publish — replace placeholders

- `website/index.html`: the "View source on GitHub" link (`your-org`).
- `pyproject.toml` / README badges: repo URL (`your-org`).
- `CODE_OF_CONDUCT.md`: the contact email.
- `.github/ISSUE_TEMPLATE/config.yml`: discussion/security URLs.

## 5. Code signing (optional but recommended)

Unsigned installers trigger a Windows SmartScreen "unknown publisher" warning the
first time. To remove it, sign `dist\VoiceFlow-Setup-1.0.0.exe` with a code-signing
certificate (e.g. Azure Trusted Signing, ~$10/mo) before publishing. See
`docs/PRODUCTION_PLAN.md` section 5 for the CI signing setup.

## 6. macOS / Linux

The code is cross-platform (see `src/voiceflow/platform/`), but the macOS and
Linux backends need to be built and tested on those OSes (the `.github/workflows`
matrix builds all three). Only Windows is build- and runtime-verified today.
