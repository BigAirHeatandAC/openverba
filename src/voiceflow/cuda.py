"""
voiceflow.cuda - CUDA DLL discovery + GPU runtime helpers.

CRITICAL ORDERING: register_cuda_dlls() MUST run BEFORE faster_whisper /
ctranslate2 are imported anywhere. pip's nvidia-* wheels drop cublas64_12.dll /
cudnn_*64_9.dll into the venv at site-packages\\nvidia\\<pkg>\\bin, but the
Windows loader does NOT search there. We must make those dirs loadable first or
inference dies with "cublas64_12.dll not found".

FINDING (preserved from main.py): os.add_dll_directory only governs the
*directly* named LoadLibrary call, NOT transitive LoadLibraryA() resolution that
happens from inside an already-loaded DLL (cudnn_ops64_9.dll itself pulls in
cudnn_cnn64_9.dll and cublasLt64_12.dll at runtime). PATH *is* searched by
transitive LoadLibraryA, so we make PATH authoritative and deterministic:
prepend cublas, then cudnn, then the rest, in a fixed order regardless of glob
enumeration order. We keep add_dll_directory too (it helps the first-level load).

engine.py calls register_cuda_dlls() at import time (before importing
faster_whisper). The GUI "Enable GPU acceleration" flow calls
gpu_runtime_present() / install_gpu_runtime() to ensure cuBLAS + cuDNN wheels
exist, then must restart the runtime so register_cuda_dlls() runs fresh.
"""

import os
import sys
import glob
import subprocess

# Cached result of the last register_cuda_dlls() call.
_CUDA_DLL_DIRS = []
_CUDA_MISSING = []

# The pip packages that provide the GPU runtime DLLs.
GPU_RUNTIME_PACKAGES = ("nvidia-cublas-cu12", "nvidia-cudnn-cu12==9.*")


def _nvidia_roots():
    """Candidate 'nvidia' package roots on sys.path (site-packages first)."""
    site_roots = [os.path.join(sys.prefix, "Lib", "site-packages", "nvidia")]
    for p in sys.path:
        if p and os.path.isdir(p):
            cand = os.path.join(p, "nvidia")
            if os.path.isdir(cand) and cand not in site_roots:
                site_roots.append(cand)
    return site_roots


def _discover_pkg_bins():
    """Map nvidia subpackage-name -> its 'bin' dir, deduped."""
    pkg_bin = {}
    for root in _nvidia_roots():
        if not os.path.isdir(root):
            continue
        for bindir in glob.glob(os.path.join(root, "*", "bin")):
            if not os.path.isdir(bindir):
                continue
            pkg = os.path.basename(os.path.dirname(bindir)).lower()
            pkg_bin.setdefault(pkg, bindir)
    return pkg_bin


def register_cuda_dlls():
    """Make pip-installed cuBLAS / cuDNN DLLs (and their transitive deps)
    loadable on Windows. Returns (added_dirs, missing_pkgs). Idempotent:
    re-prepending an already-present dir is harmless."""
    global _CUDA_DLL_DIRS, _CUDA_MISSING
    if os.name != "nt":
        _CUDA_DLL_DIRS, _CUDA_MISSING = [], []
        return _CUDA_DLL_DIRS, _CUDA_MISSING

    pkg_bin = _discover_pkg_bins()

    # Deterministic order: cublas first, then cudnn, then everything else.
    priority = ["cublas", "cudnn"]
    ordered = [pkg_bin[k] for k in priority if k in pkg_bin]
    for k in sorted(pkg_bin):
        if k not in priority:
            ordered.append(pkg_bin[k])

    seen = set()
    # Prepend to PATH in reverse so 'ordered[0]' ends up first on PATH.
    for bindir in reversed(ordered):
        norm = os.path.normcase(os.path.abspath(bindir))
        if norm in seen:
            continue
        seen.add(norm)
        try:
            os.add_dll_directory(bindir)  # helps the first-level load
        except (OSError, AttributeError):
            pass
        os.environ["PATH"] = bindir + os.pathsep + os.environ.get("PATH", "")

    _CUDA_DLL_DIRS = list(ordered)
    _CUDA_MISSING = [p for p in ("cublas", "cudnn") if p not in pkg_bin]
    return _CUDA_DLL_DIRS, _CUDA_MISSING


_RUNTIME_PRESENT = None


def gpu_runtime_present(force=False):
    """True if BOTH cuBLAS and cuDNN runtime DLL dirs are present in the venv
    (i.e. the GPU runtime wheels are installed). This does NOT mean a GPU exists
    or the driver works -- only that the CTranslate2 CUDA backend can load.

    MEMOIZED for the session: _discover_pkg_bins() walks the venv, and this is
    called on every Settings open. The "Enable GPU" flow restarts the app (so the
    new wheels are seen on the next launch), making a session cache correct. Pass
    ``force=True`` to re-scan within the same process."""
    global _RUNTIME_PRESENT
    if _RUNTIME_PRESENT is not None and not force:
        return _RUNTIME_PRESENT
    pkg_bin = _discover_pkg_bins()
    _RUNTIME_PRESENT = "cublas" in pkg_bin and "cudnn" in pkg_bin
    return _RUNTIME_PRESENT


def missing_gpu_packages():
    """Return the list of missing runtime package names ("cublas"/"cudnn")."""
    pkg_bin = _discover_pkg_bins()
    return [p for p in ("cublas", "cudnn") if p not in pkg_bin]


def install_gpu_runtime(progress_cb=None):
    """pip-install the NVIDIA cuBLAS + cuDNN runtime wheels into the current
    interpreter's environment (the "Enable GPU acceleration" flow).

    progress_cb(line: str) - optional; receives pip output lines for a live log.

    Returns (ok: bool, message: str). On success the caller MUST restart the
    runtime so register_cuda_dlls() runs again with the new DLLs on PATH (a
    process that already imported ctranslate2 cannot pick up new CUDA DLLs).
    """
    def _emit(msg):
        if progress_cb:
            try:
                progress_cb(msg)
            except Exception:
                pass

    cmd = [sys.executable, "-m", "pip", "install", "--upgrade",
           *GPU_RUNTIME_PACKAGES]
    _emit("Installing GPU runtime: %s" % " ".join(GPU_RUNTIME_PACKAGES))
    try:
        # CREATE_NO_WINDOW so a pythonw/windowed app doesn't flash a console.
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            creationflags=creationflags)
        for line in proc.stdout:
            _emit(line.rstrip("\n"))
        proc.wait()
        if proc.returncode == 0:
            if gpu_runtime_present():
                _emit("GPU runtime installed. Restart OpenVerba to use the GPU.")
                return True, ("GPU runtime installed. Restart OpenVerba to "
                              "enable GPU acceleration.")
            return False, ("pip reported success but cuBLAS/cuDNN are still not "
                           "present. Check the log.")
        return False, "pip exited with code %s. See the log for details." % \
            proc.returncode
    except Exception as exc:
        _emit("GPU runtime install failed: %s" % exc)
        return False, "GPU runtime install failed: %s" % exc


def supported_cuda_compute(requested):
    """Downselect the requested GPU compute_type to one this CTranslate2 build
    supports; return a sensible default if unknown. Safe to call (it imports
    ctranslate2 lazily, so import-time DLL registration must already be done)."""
    try:
        import ctranslate2
        supported = ctranslate2.get_supported_compute_types("cuda")
        if requested in supported:
            return requested
        for alt in ("int8_float16", "int8", "float16", "int8_float32", "float32"):
            if alt in supported:
                return alt
    except Exception:
        pass
    return requested
