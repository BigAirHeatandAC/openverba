@echo off
REM ===========================================================================
REM VoiceFlow - one-command WINDOWS build (src/ layout).
REM
REM This builds the Windows artifact only. macOS (.app/.dmg) and Linux
REM (.AppImage) are built on their own native runners -- PyInstaller cannot
REM cross-compile -- via the GitHub Actions matrix (docs/PRODUCTION_PLAN.md
REM sec 5) using packaging/voiceflow.spec, packaging/linux/build_appimage.sh
REM and the macOS codesign/notarize steps.
REM
REM   1) activates the project venv (C:\Users\shaha\voiceflow\venv)
REM   2) installs the CPU-ONLY runtime deps (requirements.txt) + PyInstaller.
REM      requirements.txt intentionally has NO torch / NO nvidia-*-gpu wheels,
REM      so PyInstaller can never pack the multi-GB CUDA runtime (the spec also
REM      excludes torch/nvidia/*-gpu as belt-and-braces). The CUDA libs are
REM      fetched on demand only when the user opts in to GPU acceleration.
REM   3) runs PyInstaller against packaging\voiceflow.spec (entry =
REM      src\voiceflow\__main__.py)  -> dist\VoiceFlow\VoiceFlow.exe (onedir)
REM   4) if ISCC (Inno Setup compiler) is found, builds the per-user installer
REM      -> dist\VoiceFlow-Setup-1.0.0.exe
REM
REM Run from anywhere; it cd's to the project root itself.
REM ===========================================================================
setlocal EnableExtensions

REM --- Resolve project root (this script lives in <root>\scripts) ------------
set "SCRIPT_DIR=%~dp0"
pushd "%SCRIPT_DIR%.." || (echo Could not enter project root.& exit /b 1)
set "ROOT=%CD%"

REM --- Locate the venv -------------------------------------------------------
set "VENV=C:\Users\shaha\voiceflow\venv"
set "PY=%VENV%\Scripts\python.exe"
if not exist "%PY%" (
    echo [ERROR] venv python not found at "%PY%".
    echo         Create/point the venv, then re-run.
    popd & exit /b 1
)

echo.
echo ============================================================
echo  VoiceFlow build
echo  root : %ROOT%
echo  py   : %PY%
echo ============================================================
echo.

REM --- Activate the venv (so PyInstaller picks up the right interpreter) -----
call "%VENV%\Scripts\activate.bat"

REM --- Single source of truth for the version (src\voiceflow\__init__.py) -----
for /f "delims=" %%v in ('"%PY%" -c "import voiceflow,sys;sys.stdout.write(voiceflow.__version__)"') do set "VF_VER=%%v"
if not defined VF_VER set "VF_VER=1.0.0"
echo     version: %VF_VER%

REM --- Build dependencies (CPU-only runtime + PyInstaller) -------------------
echo [1/4] Installing CPU-only runtime deps (requirements.txt) + PyInstaller...
"%PY%" -m pip install --upgrade pip >nul
if exist "%ROOT%\requirements.txt" (
    "%PY%" -m pip install -r "%ROOT%\requirements.txt"
    if errorlevel 1 (
        echo [ERROR] Failed to install runtime dependencies from requirements.txt.
        popd & exit /b 1
    )
)
"%PY%" -m pip install --upgrade pyinstaller
if errorlevel 1 (
    echo [ERROR] Failed to install PyInstaller.
    popd & exit /b 1
)
REM Editable install so 'voiceflow' (src/ layout) is importable by name; if the
REM project isn't a package yet this is a no-op (the spec also puts src\ on pathex).
"%PY%" -m pip install -e "%ROOT%" >nul 2>nul

REM --- Clean previous build/dist for a deterministic result ------------------
echo [2/4] Cleaning previous build\ and dist\VoiceFlow ...
if exist "%ROOT%\build" rmdir /S /Q "%ROOT%\build"
if exist "%ROOT%\dist\VoiceFlow" rmdir /S /Q "%ROOT%\dist\VoiceFlow"

REM --- Freeze the app with PyInstaller --------------------------------------
echo [3/4] Running PyInstaller (onedir, windowed)...
"%PY%" -m PyInstaller --noconfirm --clean "%ROOT%\packaging\voiceflow.spec"
if errorlevel 1 (
    echo [ERROR] PyInstaller build failed. See the output above.
    popd & exit /b 1
)
if not exist "%ROOT%\dist\VoiceFlow\VoiceFlow.exe" (
    echo [ERROR] Expected dist\VoiceFlow\VoiceFlow.exe was not produced.
    popd & exit /b 1
)
echo     [ok] dist\VoiceFlow\VoiceFlow.exe

REM --- Compile the Inno Setup installer if the compiler is available --------
echo [4/4] Building the Windows installer (Inno Setup)...
set "ISCC="
where ISCC >nul 2>nul && set "ISCC=ISCC"
REM Single-line ifs (NO parenthesised blocks) so the ")" inside %ProgramFiles(x86)%
REM can't prematurely close a block. Also check %LOCALAPPDATA%\Programs (where a
REM per-user Inno install lives).
if not defined ISCC if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"

REM Flat goto flow (NO nested parenthesised blocks) so a literal ")" in an echo
REM or in %ProgramFiles(x86)% can't break CMD's block parsing.
if not defined ISCC goto :no_iscc
"%ISCC%" /DMyAppVersion=%VF_VER% "%ROOT%\packaging\installer.iss"
if errorlevel 1 goto :iscc_failed
echo     [ok] dist\OpenVerba-Setup-%VF_VER%.exe
REM Emit the auto-update manifest (computes sha256 from the exe) and stage the
REM installer into the site's download folder for the next deploy.
"%PY%" "%ROOT%\scripts\make_manifest.py" --version %VF_VER% --exe "%ROOT%\dist\OpenVerba-Setup-%VF_VER%.exe" --out "%ROOT%\website\latest.json"
if not exist "%ROOT%\website\download" mkdir "%ROOT%\website\download"
copy /Y "%ROOT%\dist\OpenVerba-Setup-%VF_VER%.exe" "%ROOT%\website\download\OpenVerba-Setup-%VF_VER%.exe" >nul
echo     [ok] website\latest.json + website\download\OpenVerba-Setup-%VF_VER%.exe
goto :iscc_done

:iscc_failed
echo [WARN] Inno Setup compile failed; the onedir app in dist\VoiceFlow is still usable.
goto :iscc_done

:no_iscc
echo [SKIP] ISCC (Inno Setup 6 compiler) not found on PATH or in Program Files.
echo        Install Inno Setup 6 from jrsoftware.org/isdl.php, then re-run this script.

:iscc_done

echo.
echo ============================================================
echo  BUILD COMPLETE
echo ------------------------------------------------------------
echo  Portable app : dist\VoiceFlow\VoiceFlow.exe
if defined ISCC echo  Installer    : dist\OpenVerba-Setup-%VF_VER%.exe
echo.
echo  Next steps:
echo    * Test the app:   dist\VoiceFlow\VoiceFlow.exe
echo    * First launch downloads the speech model (needs internet once).
echo    * GPU users: enable GPU acceleration from the app (fetches CUDA libs).
echo    * Ship the installer (or zip dist\VoiceFlow for a portable build).
echo ============================================================
echo.

popd
endlocal
exit /b 0
