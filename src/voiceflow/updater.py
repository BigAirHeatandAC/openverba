"""
voiceflow.updater - check openverba.com for a newer build and (optionally)
download + verify + hand off to the Inno installer for an in-place upgrade.

Stdlib only (urllib.request, json, hashlib, subprocess, os, sys, tempfile,
logging) so this module is importable from both the headless background runtime
(``cli.py``) and the customtkinter GUI without dragging in any GUI deps. It must
NEVER block startup or raise into its callers: every network/file/JSON operation
is wrapped, and the public entry points return small result objects instead of
throwing.

Why "download installer + exit + let Inno upgrade in place":
---------------------------------------------------------------------------
This app ships as a PyInstaller *onedir* build (``VoiceFlow.exe`` next to an
``_internal`` tree of DLLs). While the app is running, Windows holds those files
open with a share-deny-write lock, so the installer's file-copy step cannot
overwrite ``VoiceFlow.exe`` or its DLLs. A process also cannot replace its own
running image. Therefore the safe upgrade model is:

    1. Download the new per-user installer to %TEMP% (NOT the install dir, which
       is about to be overwritten / may be read-only).
    2. Verify its SHA-256 against the manifest. **Verification is mandatory** -
       we never launch an unverified installer (defends against a corrupted or
       tampered download).
    3. Spawn the installer DETACHED so it outlives us, then immediately exit the
       app so every file lock + the single-instance mutex
       (``constants.SINGLETON_MUTEX``) release.
    4. Inno (AppId-keyed, PrivilegesRequired=lowest, AppMutex on our singleton,
       CloseApplications=yes) overwrites the onedir in place and relaunches.

The default posture is *interactive* (the user sees the wizard and consents);
``launch_installer_and_exit(silent=True)`` exists for a future opt-in but is not
wired to any default. ``check_for_updates`` never downloads on its own - download
is an explicit second step a caller triggers after the user agrees.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from typing import Callable, Optional

log = logging.getLogger("voiceflow.updater")

DEFAULT_MANIFEST_URL = "https://openverba.com/latest.json"

# Hard safety caps so a malicious/broken server can't exhaust memory or disk.
_MAX_MANIFEST_BYTES = 64 * 1024            # 64 KiB - a manifest is tiny
_MAX_INSTALLER_BYTES = 500 * 1024 * 1024   # 500 MiB - installer is ~75 MiB
_CHUNK = 256 * 1024

# Inno Setup silent flags. /SP- suppresses the "This will install..." prompt.
_SILENT_FLAGS = ["/SILENT", "/SP-", "/SUPPRESSMSGBOXES", "/NORESTART"]

# Windows process-creation flags so the installer outlives our exit.
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------
@dataclasses.dataclass
class UpdateInfo:
    version: str
    url: str
    sha256: str
    size: Optional[int] = None
    notes: str = ""
    mandatory: bool = False
    min_version: Optional[str] = None
    pub_date: str = ""


@dataclasses.dataclass
class CheckResult:
    """Small, JSON-ish result the UI/tray consume. ``status`` is one of
    'up_to_date' | 'update_available' | 'error' | 'downloaded'."""
    status: str
    current: str = ""
    version: str = ""
    notes: str = ""
    error: Optional[str] = None
    mandatory: bool = False
    info: Optional[UpdateInfo] = None

    # Convenience for callers written against an "available" flag.
    @property
    def available(self) -> bool:
        return self.status == "update_available"


# ---------------------------------------------------------------------------
# Version helpers
# ---------------------------------------------------------------------------
def get_current_version() -> str:
    """Return the running app version. Single source of truth is
    ``voiceflow.__version__``; falls back to '0.0.0' if it can't be imported
    (which makes any real remote version look newer - fail toward "offer the
    update" rather than silently never updating)."""
    try:
        from voiceflow import __version__
        v = str(__version__).strip()
        return v or "0.0.0"
    except Exception:
        return "0.0.0"


def parse_version(s) -> tuple:
    """Parse a SemVer-ish string into a comparable numeric tuple.

    Tolerant: strips a leading 'v'/'V', ignores any pre-release/build suffix
    after the first '-' or '+', pads to 3 components, and coerces each numeric
    field. Junk components become 0 (never raises). Examples:
        "v1.2.3"        -> (1, 2, 3)
        "1.2"           -> (1, 2, 0)
        "1.2.3-beta.1"  -> (1, 2, 3)
        "" / None       -> (0, 0, 0)
    """
    try:
        text = str(s or "").strip().lstrip("vV").strip()
    except Exception:
        return (0, 0, 0)
    # Drop pre-release / build metadata.
    for sep in ("-", "+"):
        idx = text.find(sep)
        if idx != -1:
            text = text[:idx]
    parts = text.split(".")
    out = []
    for p in parts[:3]:
        digits = ""
        for ch in p.strip():
            if ch.isdigit():
                digits += ch
            else:
                break
        out.append(int(digits) if digits else 0)
    while len(out) < 3:
        out.append(0)
    return tuple(out[:3])


def is_newer(remote, local) -> bool:
    """True iff ``remote`` is a strictly newer version than ``local``. Any parse
    trouble yields False (fail safe: don't push an update we can't reason about)."""
    try:
        return parse_version(remote) > parse_version(local)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------
def _require_https(url: str) -> bool:
    try:
        return isinstance(url, str) and url.lower().startswith("https://")
    except Exception:
        return False


def fetch_manifest(url: str = DEFAULT_MANIFEST_URL, timeout: float = 10.0):
    """GET the manifest JSON and return an ``UpdateInfo``, or ``None`` on any
    failure (network/HTTP/parse/missing-required-field/non-https). Never raises.

    Required keys: ``version``, ``url`` (https), ``sha256`` (64 hex)."""
    if not _require_https(url):
        log.warning("Refusing non-https manifest URL: %r", url)
        return None
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "OpenVerba/%s" % get_current_version(),
                "Cache-Control": "no-cache",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(_MAX_MANIFEST_BYTES + 1)
        if len(raw) > _MAX_MANIFEST_BYTES:
            log.warning("Manifest too large (> %d bytes); ignoring.",
                        _MAX_MANIFEST_BYTES)
            return None
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        log.info("Update check: could not fetch/parse manifest: %s", exc)
        return None

    if not isinstance(data, dict):
        log.warning("Manifest is not a JSON object; ignoring.")
        return None

    version = data.get("version")
    dl_url = data.get("url")
    sha = data.get("sha256")

    if not isinstance(version, str) or not version.strip():
        log.warning("Manifest missing/invalid 'version'.")
        return None
    if not _require_https(dl_url):
        log.warning("Manifest 'url' missing or not https: %r", dl_url)
        return None
    if not isinstance(sha, str) or len(sha.strip()) != 64 \
            or not _is_hex(sha.strip()):
        log.warning("Manifest 'sha256' missing or not 64 hex chars.")
        return None

    size = data.get("size")
    try:
        size = int(size) if size is not None else None
    except Exception:
        size = None

    return UpdateInfo(
        version=version.strip(),
        url=dl_url.strip(),
        sha256=sha.strip().lower(),
        size=size,
        notes=str(data.get("notes") or ""),
        mandatory=bool(data.get("mandatory", False)),
        min_version=(str(data["min_version"]).strip()
                     if data.get("min_version") else None),
        pub_date=str(data.get("pub_date") or ""),
    )


def _is_hex(s: str) -> bool:
    try:
        int(s, 16)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Download + verify
# ---------------------------------------------------------------------------
def _dest_dir(dest: Optional[str]) -> str:
    if dest:
        return dest
    return os.path.join(tempfile.gettempdir(), "OpenVerba-update")


def download(url: str, sha256: str,
             dest: Optional[str] = None,
             progress_cb: Optional[Callable[[float, int, int], None]] = None
             ) -> Optional[str]:
    """Stream the installer at ``url`` to a temp file, verify its SHA-256 against
    ``sha256``, and return the absolute path on success. Returns ``None`` on any
    failure (non-https, network error, size cap exceeded, sha mismatch); the
    partial/bad file is deleted. Never raises.

    ``progress_cb(frac, downloaded_bytes, total_bytes)`` is called periodically
    (total_bytes is 0 if the server sends no Content-Length). This deliberately
    matches the model-download progress shape so the Settings UI can reuse its
    progress-bar plumbing (it passes a 4th ``desc`` only for model downloads)."""
    if not _require_https(url):
        log.warning("Refusing non-https download URL: %r", url)
        return None
    if not (isinstance(sha256, str) and len(sha256.strip()) == 64):
        log.warning("Refusing download without a valid 64-hex sha256.")
        return None
    want = sha256.strip().lower()

    out_dir = _dest_dir(dest)
    try:
        os.makedirs(out_dir, exist_ok=True)
    except Exception as exc:
        log.warning("Could not create update dir %s: %s", out_dir, exc)
        return None

    fname = os.path.basename(url.split("?", 1)[0]) or "OpenVerba-Setup.exe"
    if not fname.lower().endswith(".exe"):
        fname += ".exe"
    path = os.path.join(out_dir, fname)

    hasher = hashlib.sha256()
    downloaded = 0
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "OpenVerba/%s" % get_current_version()})
        with urllib.request.urlopen(req, timeout=60) as resp:
            total = 0
            try:
                total = int(resp.headers.get("Content-Length") or 0)
            except Exception:
                total = 0
            if total and total > _MAX_INSTALLER_BYTES:
                log.warning("Installer Content-Length %d exceeds cap; aborting.",
                            total)
                return None
            with open(path, "wb") as fh:
                while True:
                    chunk = resp.read(_CHUNK)
                    if not chunk:
                        break
                    downloaded += len(chunk)
                    if downloaded > _MAX_INSTALLER_BYTES:
                        log.warning("Installer exceeded size cap (%d); aborting.",
                                    _MAX_INSTALLER_BYTES)
                        _safe_unlink(path)
                        return None
                    fh.write(chunk)
                    hasher.update(chunk)
                    if progress_cb:
                        try:
                            frac = (downloaded / total) if total else 0.0
                            progress_cb(frac, downloaded, total)
                        except Exception:
                            pass
    except Exception as exc:
        log.warning("Download failed: %s", exc)
        _safe_unlink(path)
        return None

    got = hasher.hexdigest().lower()
    if got != want:
        log.warning("SHA-256 mismatch (got %s, want %s); deleting download.",
                    got, want)
        _safe_unlink(path)
        return None

    log.info("Update downloaded + verified: %s (%d bytes)", path, downloaded)
    return path


def _safe_unlink(path: str) -> None:
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Hand off to the installer + exit (so file locks release)
# ---------------------------------------------------------------------------
def _request_app_exit() -> None:
    """Tear the app down so all onedir file locks + the singleton mutex release,
    then hard-exit. Mirrors cli._quit / main_window._really_quit.

    The grace delay must be long enough that THIS process is fully gone (file
    handles + the Global\\VoiceFlowSingleton mutex released) before the detached
    installer reaches its file-copy step -- otherwise Windows' share-deny-write
    lock on VoiceFlow.exe / its DLLs makes the in-place overwrite fail. The
    installer also runs a PrepareToInstall taskkill as a belt-and-braces closer,
    but we still want a clean self-exit here first."""
    def _bye():
        time.sleep(1.8)
        os._exit(0)
    threading.Thread(target=_bye, daemon=True).start()


def launch_installer_and_exit(path: str, silent: bool = False) -> bool:
    """Spawn the downloaded Inno installer DETACHED (so it survives our exit),
    then schedule the app to quit so locks release and Inno can overwrite the
    onedir in place. Returns True if the installer was launched.

    ``silent`` adds the Inno unattended flags; the default (interactive) shows
    the wizard so the user consents. Never raises."""
    if not (path and os.path.isfile(path)):
        log.warning("launch_installer_and_exit: no such file %r", path)
        return False
    flags = list(_SILENT_FLAGS) if silent else []
    try:
        creationflags = 0
        if os.name == "nt":
            creationflags = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP
        subprocess.Popen([path] + flags, creationflags=creationflags,
                         close_fds=True, shell=False)
        log.info("Launched installer %s (silent=%s); exiting app to release "
                 "file locks.", path, silent)
    except Exception as exc:
        log.warning("Could not launch installer: %s", exc)
        return False
    _request_app_exit()
    return True


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def due_for_check(cfg: dict, interval_hours: float = 24.0) -> bool:
    """True if auto-update checking is on AND at least ``interval_hours`` have
    elapsed since the last check. Safe against missing/garbage cfg values."""
    try:
        if not cfg.get("auto_update_check", True):
            return False
        last = float(cfg.get("last_update_check") or 0)
    except Exception:
        return bool(cfg.get("auto_update_check", True))
    now = time.time()
    # Clock skew / reset: a future-stamped last-check would otherwise suppress
    # checks forever -> treat it as "due".
    if last > now:
        return True
    return (now - last) >= (interval_hours * 3600.0)


def check_for_updates(cfg: dict, interactive: bool = False) -> CheckResult:
    """Compare openverba.com's manifest version against the running version.

    Honors ``cfg['auto_update_check']`` (a non-interactive/background check is
    skipped when it's off) and ``cfg['update_manifest_url']``. Stamps
    ``cfg['last_update_check']`` and persists config. **Never downloads or runs
    the installer** - that's an explicit second step the caller takes after the
    user consents. Never raises; failures come back as status='error'."""
    current = get_current_version()
    try:
        if not interactive and not cfg.get("auto_update_check", True):
            return CheckResult(status="up_to_date", current=current,
                               version=current)

        manifest_url = cfg.get("update_manifest_url") or DEFAULT_MANIFEST_URL
        info = fetch_manifest(manifest_url)

        # Stamp the check time regardless of outcome (best effort).
        try:
            cfg["last_update_check"] = time.time()
            from voiceflow import config as vf_config
            vf_config.save_config(cfg)
        except Exception:
            pass

        if info is None:
            return CheckResult(status="error", current=current,
                               error="Could not reach the update server.")

        if is_newer(info.version, current):
            mandatory = bool(info.mandatory) or (
                info.min_version is not None
                and is_newer(info.min_version, current))
            return CheckResult(
                status="update_available", current=current,
                version=info.version, notes=info.notes,
                mandatory=mandatory, info=info)

        return CheckResult(status="up_to_date", current=current,
                           version=info.version, info=info)
    except Exception as exc:
        log.info("check_for_updates failed: %s", exc)
        return CheckResult(status="error", current=current, error=str(exc))
