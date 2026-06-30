"""
voiceflow._cuda_shim - import this BEFORE faster_whisper / ctranslate2.

This is the single entry point the plan mandates: ``__main__.py`` imports it
first so the Windows CUDA DLL search path is set up before CTranslate2 loads.

On Windows it delegates to :func:`voiceflow.cuda.register_cuda_dlls`, which is
the hard-won, deterministic registration of the pip ``nvidia-*`` wheel bin dirs
(cublas first, then cudnn, then the rest) via ``os.add_dll_directory`` + a
PATH prepend so transitive ``LoadLibraryA`` resolution also works. Without this
CTranslate2 dies with "cublas64_12.dll not found" / "cudnn_ops64_9.dll".

On Linux ``LD_LIBRARY_PATH`` must be set before the process launches (it cannot
be fixed from inside a running interpreter), and on macOS there is no CUDA, so
this is a no-op on those platforms (the fallback ladder loads CPU int8 there).
"""

import os
import sys


def enable_cuda_dlls():
    """Make pip-installed cuBLAS / cuDNN DLLs loadable on Windows. No-op on
    non-Windows. Returns (added_dirs, missing_pkgs). Idempotent."""
    if sys.platform != "win32":
        return [], []
    # Reuse the project's authoritative, deterministic registration.
    try:
        from . import cuda
        return cuda.register_cuda_dlls()
    except Exception:
        # Last-resort fallback: the plan's minimal nvidia-wheel path, so that
        # CUDA can still load even if voiceflow.cuda failed to import.
        added = []
        try:
            import nvidia.cublas.lib  # type: ignore
            import nvidia.cudnn.lib   # type: ignore
        except ImportError:
            return [], []
        for mod in (nvidia.cublas.lib, nvidia.cudnn.lib):
            d = os.path.dirname(mod.__file__)
            if os.path.isdir(d):
                try:
                    os.add_dll_directory(d)
                except (OSError, AttributeError):
                    pass
                os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
                added.append(d)
        return added, []


# Run on import so `import voiceflow._cuda_shim` (per the plan) is sufficient.
_ADDED, _MISSING = enable_cuda_dlls()
