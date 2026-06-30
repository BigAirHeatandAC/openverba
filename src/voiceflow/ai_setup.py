"""
voiceflow.ai_setup - one-click setup for the OPTIONAL local AI editor (Ollama).

Smart AI editing is opt-in (it needs a capable PC and a ~1-2 GB model download),
so Ollama is NOT bundled in the installer. This module lets the app set it up on
demand from the GUI: detect/start Ollama, install it if missing, and pull the
model recommended for the user's hardware -- all with progress callbacks.

stdlib only (urllib/json/subprocess) so it adds no runtime dependency.
"""

from __future__ import annotations

import os
import json
import time
import logging
import subprocess
import urllib.request

log = logging.getLogger("voiceflow.ai_setup")

OLLAMA_URL = "http://localhost:11434"
OLLAMA_INSTALLER_URL = "https://ollama.com/download/OllamaSetup.exe"

_NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW (Windows) so no console flashes


def ollama_exe():
    """Return the path to ollama.exe if installed, else None."""
    candidates = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(os.path.join(local, "Programs", "Ollama", "ollama.exe"))
    pf = os.environ.get("ProgramFiles")
    if pf:
        candidates.append(os.path.join(pf, "Ollama", "ollama.exe"))
    for c in candidates:
        if os.path.isfile(c):
            return c
    # PATH fallback
    from shutil import which
    return which("ollama")


def server_up(url=OLLAMA_URL, timeout=2):
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/tags", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def ensure_server(url=OLLAMA_URL):
    """Make sure the Ollama server is running; start it if the exe exists."""
    if server_up(url):
        return True
    exe = ollama_exe()
    if not exe:
        return False
    try:
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = _NO_WINDOW | 0x00000008  # DETACHED_PROCESS
        subprocess.Popen([exe, "serve"], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, **kwargs)
    except Exception:
        log.error("Could not start ollama serve", exc_info=True)
    for _ in range(20):
        if server_up(url):
            return True
        time.sleep(0.5)
    return server_up(url)


def model_installed(model, url=OLLAMA_URL):
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/tags", timeout=3) as r:
            data = json.loads(r.read().decode())
        names = {m.get("name", "") for m in data.get("models", [])}
        return model in names or (model + ":latest") in names or any(
            n.split(":")[0] == model.split(":")[0] and n.startswith(model)
            for n in names)
    except Exception:
        return False


def install_ollama(progress_cb=None):
    """Download + silently install Ollama. Returns (ok, message). Windows only."""
    def emit(m):
        log.info(m)
        if progress_cb:
            try:
                progress_cb(m)
            except Exception:
                pass

    if ollama_exe():
        emit("Ollama is already installed.")
        return True, "already installed"
    if os.name != "nt":
        return False, "Automatic Ollama install is only wired up for Windows; "\
                      "install it from https://ollama.com/download"
    tmp = os.path.join(os.environ.get("TEMP", "."), "OllamaSetup.exe")
    emit("Downloading Ollama (~700 MB)... this can take a while.")
    try:
        with urllib.request.urlopen(OLLAMA_INSTALLER_URL, timeout=60) as r:
            total = int(r.headers.get("Content-Length") or 0)
            got = 0
            with open(tmp, "wb") as f:
                while True:
                    chunk = r.read(1024 * 256)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    if total:
                        emit("Downloading Ollama: %d%%" % int(got * 100 / total))
    except Exception as exc:
        return False, "Download failed: %s" % exc
    emit("Installing Ollama...")
    try:
        kwargs = {"creationflags": _NO_WINDOW} if os.name == "nt" else {}
        # Ollama's installer is Inno Setup -> /VERYSILENT for an unattended install.
        rc = subprocess.call([tmp, "/VERYSILENT", "/NORESTART"], **kwargs)
    except Exception as exc:
        return False, "Installer failed to launch: %s" % exc
    for _ in range(30):
        if ollama_exe():
            break
        time.sleep(1)
    if not ollama_exe():
        return False, "Install finished (rc=%s) but ollama.exe wasn't found." % rc
    emit("Ollama installed.")
    return True, "installed"


def pull_model(model, progress_cb=None, url=OLLAMA_URL):
    """Pull a model via the Ollama HTTP API with streaming progress. Returns
    (ok, message). progress_cb(fraction0to1, status_text)."""
    if not ensure_server(url):
        return False, "Ollama server isn't running."
    body = json.dumps({"model": model, "stream": True}).encode()
    req = urllib.request.Request(url.rstrip("/") + "/api/pull", data=body,
                                 headers={"Content-Type": "application/json"})
    last_status = ""
    try:
        with urllib.request.urlopen(req, timeout=None) as r:
            for line in r:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line.decode())
                except Exception:
                    continue
                if ev.get("error"):
                    return False, ev["error"]
                status = ev.get("status", "")
                total = ev.get("total") or 0
                completed = ev.get("completed") or 0
                frac = (completed / total) if total else None
                if status != last_status or frac is not None:
                    last_status = status
                    if progress_cb:
                        try:
                            progress_cb(frac, status)
                        except Exception:
                            pass
                if status == "success":
                    return True, "pulled"
    except Exception as exc:
        return False, "Pull failed: %s" % exc
    return model_installed(model, url), "done"


def setup(model, progress_cb=None):
    """Full one-click setup: install Ollama (if needed) -> start -> pull `model`.
    progress_cb(text). Returns (ok, message)."""
    def emit(m):
        if progress_cb:
            try:
                progress_cb(m)
            except Exception:
                pass

    ok, msg = install_ollama(progress_cb=lambda m: emit(m))
    if not ok:
        return False, msg
    if not ensure_server():
        return False, "Could not start the Ollama server."
    emit("Downloading the AI model '%s' (this is the big one)..." % model)

    def pcb(frac, status):
        if frac is not None:
            emit("%s: %d%%" % (status, int(frac * 100)))
        else:
            emit(status)

    ok, msg = pull_model(model, progress_cb=pcb)
    if not ok:
        return False, msg
    emit("Smart AI editing is ready.")
    return True, "ready"
