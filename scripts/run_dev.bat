@echo off
REM ===========================================================================
REM VoiceFlow - run from source (development), src/ layout.
REM
REM Launches the package GUI entry with the project venv interpreter. Any args
REM you pass are forwarded, e.g.:
REM
REM   scripts\run_dev.bat                 -> GUI (default; onboarding on 1st run)
REM   scripts\run_dev.bat --background    -> headless dictation runtime (tray)
REM   scripts\run_dev.bat --version       -> print version and exit
REM
REM Uses the venv at C:\Users\shaha\voiceflow\venv. We run with python.exe (not
REM pythonw.exe) on purpose during dev so you see logs/tracebacks in the console.
REM
REM If you have done an editable install (`pip install -e .`) the package is
REM already importable and PYTHONPATH is not needed; we set it anyway so this
REM works straight from a fresh checkout without installing.
REM ===========================================================================
setlocal EnableExtensions

set "SCRIPT_DIR=%~dp0"
pushd "%SCRIPT_DIR%.." || (echo Could not enter project root.& exit /b 1)
set "ROOT=%CD%"

set "VENV=C:\Users\shaha\voiceflow\venv"
set "PY=%VENV%\Scripts\python.exe"
if not exist "%PY%" (
    echo [ERROR] venv python not found at "%PY%".
    popd & exit /b 1
)

call "%VENV%\Scripts\activate.bat"

REM Run from src/ so 'voiceflow' resolves to the new src layout even without an
REM editable install. (-m voiceflow imports _cuda_shim before faster_whisper.)
set "PYTHONPATH=%ROOT%\src;%PYTHONPATH%"

echo Running VoiceFlow from source: -m voiceflow %*
"%PY%" -m voiceflow %*
set "RC=%ERRORLEVEL%"

popd
endlocal & exit /b %RC%
