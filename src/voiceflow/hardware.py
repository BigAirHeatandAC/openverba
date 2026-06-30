"""
voiceflow.hardware - hardware detection + model recommendation.

Pure detection: no dependency on the engine, faster_whisper, or ctranslate2.
Everything degrades gracefully when optional libraries (psutil, nvidia-ml-py)
are missing, falling back to nvidia-smi parsing and stdlib/ctypes probes so this
module is safe to import on any machine (GPU or not, fresh venv or not).

Public API
----------
detect_hardware() -> dict
    {
      "gpu": {"present": bool, "name": str|None, "vram_mb": int|None,
              "cuda": str|None, "driver": str|None, "count": int},
      "cpu": {"name": str|None, "cores": int|None, "threads": int|None},
      "ram_gb": float|None,
      "os": str,
      "detected_via": {...},   # which backend supplied each field (diagnostics)
    }

recommend_models(hw) -> list[dict]
    Ordered list (best-first) of recommendations, each:
    {"model_id": str, "reason": str, "tier": "recommended"|"max"|"light"}
    The model_id values match ids in voiceflow.models.MODEL_CATALOG.
"""

from __future__ import annotations

import os
import re
import sys
import shutil
import platform
import subprocess

__all__ = ["detect_hardware", "recommend_models"]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _no_window_kwargs():
    """Keep subprocess from flashing a console window on Windows (the GUI runs
    windowed / no-console)."""
    kw = {}
    if os.name == "nt":
        try:
            kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        except Exception:
            pass
        try:
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            kw["startupinfo"] = si
        except Exception:
            pass
    return kw


def _run(cmd, timeout=6):
    """Run a command, return stdout text or None. Never raises."""
    try:
        out = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            text=True,
            **_no_window_kwargs(),
        )
        if out.returncode == 0 and out.stdout:
            return out.stdout
        # Some tools write useful output even with a nonzero code.
        return out.stdout or None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# GPU detection
# ---------------------------------------------------------------------------
def _gpu_via_nvml():
    """Try nvidia-ml-py (the `pynvml` module). Returns dict or None."""
    try:
        import pynvml  # provided by the 'nvidia-ml-py' wheel
    except Exception:
        return None
    try:
        pynvml.nvmlInit()
    except Exception:
        return None
    try:
        count = pynvml.nvmlDeviceGetCount()
        if count <= 0:
            return None
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        name = pynvml.nvmlDeviceGetName(handle)
        if isinstance(name, bytes):
            name = name.decode("utf-8", "replace")
        mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
        vram_mb = int(mem.total / (1024 * 1024))
        try:
            driver = pynvml.nvmlSystemGetDriverVersion()
            if isinstance(driver, bytes):
                driver = driver.decode("utf-8", "replace")
        except Exception:
            driver = None
        cuda = None
        try:
            # Returns e.g. 12020 -> "12.2"
            ver = pynvml.nvmlSystemGetCudaDriverVersion_v2()
            cuda = "%d.%d" % (ver // 1000, (ver % 1000) // 10)
        except Exception:
            try:
                ver = pynvml.nvmlSystemGetCudaDriverVersion()
                cuda = "%d.%d" % (ver // 1000, (ver % 1000) // 10)
            except Exception:
                cuda = None
        return {
            "present": True,
            "name": (name or "").strip() or None,
            "vram_mb": vram_mb,
            "cuda": cuda,
            "driver": (driver or None),
            "count": int(count),
        }
    except Exception:
        return None
    finally:
        try:
            pynvml.nvmlShutdown()
        except Exception:
            pass


def _find_nvidia_smi():
    exe = shutil.which("nvidia-smi")
    if exe:
        return exe
    if os.name == "nt":
        candidates = [
            os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                         "System32", "nvidia-smi.exe"),
            os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                         "NVIDIA Corporation", "NVSMI", "nvidia-smi.exe"),
        ]
        for c in candidates:
            if c and os.path.isfile(c):
                return c
    return None


def _gpu_via_smi():
    """Parse nvidia-smi for name / total VRAM / driver / CUDA version."""
    exe = _find_nvidia_smi()
    if not exe:
        return None

    name = vram_mb = driver = None
    count = 0
    # Machine-readable query for name + memory + driver.
    q = _run([
        exe,
        "--query-gpu=name,memory.total,driver_version",
        "--format=csv,noheader,nounits",
    ])
    if q:
        for line in q.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = [p.strip() for p in line.split(",")]
            count += 1
            if name is None and parts:
                name = parts[0] or None
                if len(parts) >= 2:
                    try:
                        vram_mb = int(round(float(parts[1])))
                    except Exception:
                        vram_mb = None
                if len(parts) >= 3:
                    driver = parts[2] or None
    if name is None:
        return None

    # CUDA driver version comes from the plain header, not the query API.
    cuda = None
    head = _run([exe])
    if head:
        m = re.search(r"CUDA Version:\s*([0-9]+\.[0-9]+)", head)
        if m:
            cuda = m.group(1)

    return {
        "present": True,
        "name": name,
        "vram_mb": vram_mb,
        "cuda": cuda,
        "driver": driver,
        "count": count or 1,
    }


def _detect_gpu(via):
    gpu = _gpu_via_nvml()
    if gpu:
        via["gpu"] = "nvml"
        return gpu
    gpu = _gpu_via_smi()
    if gpu:
        via["gpu"] = "nvidia-smi"
        return gpu
    via["gpu"] = "none"
    return {"present": False, "name": None, "vram_mb": None,
            "cuda": None, "driver": None, "count": 0}


# ---------------------------------------------------------------------------
# CPU detection
# ---------------------------------------------------------------------------
def _cpu_name():
    # 1) Windows: registry holds a clean marketing name.
    if os.name == "nt":
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
            try:
                val, _ = winreg.QueryValueEx(key, "ProcessorNameString")
                if val:
                    return val.strip()
            finally:
                winreg.CloseKey(key)
        except Exception:
            pass
    # 2) platform.processor() (often empty on Windows, useful on Linux/mac).
    try:
        p = platform.processor()
        if p and p.strip():
            return p.strip()
    except Exception:
        pass
    # 3) Linux /proc/cpuinfo.
    try:
        if os.path.isfile("/proc/cpuinfo"):
            with open("/proc/cpuinfo", "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if line.lower().startswith("model name"):
                        return line.split(":", 1)[1].strip()
    except Exception:
        pass
    # 4) Fall back to the architecture string.
    try:
        m = platform.machine()
        if m:
            return m
    except Exception:
        pass
    return None


def _cpu_counts():
    """Return (physical_cores, logical_threads). Falls back to os.cpu_count()."""
    cores = threads = None
    try:
        import psutil
        try:
            cores = psutil.cpu_count(logical=False)
        except Exception:
            cores = None
        try:
            threads = psutil.cpu_count(logical=True)
        except Exception:
            threads = None
    except Exception:
        pass
    if threads is None:
        try:
            threads = os.cpu_count()
        except Exception:
            threads = None
    if cores is None:
        # No physical-core info -> best guess (most x86 CPUs are 2 threads/core).
        if threads:
            cores = max(1, threads // 2) if threads >= 2 else threads
    return cores, threads


def _detect_cpu(via):
    name = _cpu_name()
    cores, threads = _cpu_counts()
    via["cpu"] = "psutil" if _has_psutil() else "stdlib"
    return {"name": name, "cores": cores, "threads": threads}


def _has_psutil():
    try:
        import psutil  # noqa: F401
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# RAM detection
# ---------------------------------------------------------------------------
def _ram_via_psutil():
    try:
        import psutil
        total = psutil.virtual_memory().total
        return round(total / (1024 ** 3), 1)
    except Exception:
        return None


def _ram_via_ctypes():
    """Windows GlobalMemoryStatusEx fallback when psutil is missing."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", wintypes.DWORD),
                ("dwMemoryLoad", wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            return round(stat.ullTotalPhys / (1024 ** 3), 1)
    except Exception:
        pass
    return None


def _ram_via_proc():
    """Linux /proc/meminfo fallback."""
    try:
        if os.path.isfile("/proc/meminfo"):
            with open("/proc/meminfo", "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if line.startswith("MemTotal:"):
                        kb = int(re.search(r"(\d+)", line).group(1))
                        return round(kb / (1024 ** 2), 1)
    except Exception:
        pass
    return None


def _detect_ram(via):
    ram = _ram_via_psutil()
    if ram is not None:
        via["ram"] = "psutil"
        return ram
    ram = _ram_via_ctypes()
    if ram is not None:
        via["ram"] = "ctypes"
        return ram
    ram = _ram_via_proc()
    if ram is not None:
        via["ram"] = "/proc/meminfo"
        return ram
    via["ram"] = "none"
    return None


# ---------------------------------------------------------------------------
# OS string
# ---------------------------------------------------------------------------
def _detect_os():
    try:
        sysname = platform.system() or os.name
        rel = platform.release() or ""
        ver = platform.version() or ""
    except Exception:
        return sys.platform
    if sysname == "Windows":
        # platform.release() reports "10" for Win11; build >= 22000 is Win11.
        try:
            build = int(ver.split(".")[-1])
            if rel == "10" and build >= 22000:
                rel = "11"
        except Exception:
            pass
        return ("Windows %s" % rel).strip()
    return ("%s %s" % (sysname, rel)).strip()


# ---------------------------------------------------------------------------
# Public: detect_hardware
# ---------------------------------------------------------------------------
_HW_CACHE = None


def detect_hardware(force=False):
    """Probe the machine for GPU / CPU / RAM / OS. Never raises; missing fields
    come back as None so callers can render 'Unknown' gracefully.

    Result is CACHED for the process session: hardware doesn't change at runtime,
    and the probe spawns nvidia-smi / nvml (up to several seconds cold). Settings
    re-opens then cost nothing. Pass ``force=True`` to re-probe (e.g. diagnostics).
    """
    global _HW_CACHE
    if _HW_CACHE is not None and not force:
        return _HW_CACHE
    via = {}
    try:
        gpu = _detect_gpu(via)
    except Exception:
        gpu = {"present": False, "name": None, "vram_mb": None,
               "cuda": None, "driver": None, "count": 0}
        via["gpu"] = "error"
    try:
        cpu = _detect_cpu(via)
    except Exception:
        cpu = {"name": None, "cores": None, "threads": None}
        via["cpu"] = "error"
    try:
        ram = _detect_ram(via)
    except Exception:
        ram = None
        via["ram"] = "error"
    _HW_CACHE = {
        "gpu": gpu,
        "cpu": cpu,
        "ram_gb": ram,
        "os": _detect_os(),
        "detected_via": via,
    }
    return _HW_CACHE


# ---------------------------------------------------------------------------
# Public: recommend_models
# ---------------------------------------------------------------------------
def recommend_ai(hw=None):
    """Recommend whether this PC can run the OPTIONAL local AI editor (Ollama),
    and which model size. Returns {"capable","model","tier","reason"}. AI editing
    is always optional -- dictation + key commands work without it."""
    hw = hw or detect_hardware()
    ram = hw.get("ram_gb") or 0
    if ram and ram < 8:
        return {"capable": False, "model": None, "tier": "none",
                "reason": ("Under 8 GB RAM -- local AI editing would be too slow. "
                           "Dictation and key commands work great without it.")}
    if ram >= 32:
        return {"capable": True, "model": "qwen2.5:7b", "tier": "best",
                "reason": "32 GB+ RAM -- a 7B model gives the best local rewrites."}
    if ram >= 16:
        return {"capable": True, "model": "qwen2.5:3b", "tier": "good",
                "reason": "16 GB+ RAM -- a 3B model balances quality and speed."}
    return {"capable": True, "model": "qwen2.5:1.5b", "tier": "light",
            "reason": ("8-16 GB RAM -- a small 1.5B model keeps edits responsive "
                       "(lighter rewrites).")}


def recommend_models(hw):
    """Return an ordered (best-first) list of model recommendations for this PC.

    Each item: {"model_id": str, "reason": str, "tier": str}
    tier in {"recommended", "max", "light"}.

    Logic by VRAM (English-tuned *.en chosen for the English models):
      no GPU   -> base.en (recommended, fast on CPU) / small.en (more accurate,
                  slower) / tiny.en (light, fastest)
      <  4GB   -> small.en (rec) / base.en (light) / medium.en (max, may be tight)
      4-5GB    -> small.en (rec) / medium.en (max) / base.en (light)
      6-9GB    -> medium.en (rec) / distil-large-v3 or large-v3 (max) / small.en
      10GB+    -> large-v3 (rec) / distil-large-v3 (max, faster) / medium.en

    model_id values intentionally match ids in voiceflow.models.MODEL_CATALOG.
    """
    gpu = (hw or {}).get("gpu") or {}
    present = bool(gpu.get("present"))
    vram = gpu.get("vram_mb")
    gpu_name = gpu.get("name") or "your GPU"

    recs = []

    if not present or not vram:
        # ---- CPU-only machine ----
        recs.append({
            "model_id": "base.en",
            "tier": "recommended",
            "reason": ("No NVIDIA GPU detected, so transcription runs on the "
                       "CPU. base.en is the best balance of speed and accuracy "
                       "for CPU dictation."),
        })
        recs.append({
            "model_id": "small.en",
            "tier": "max",
            "reason": ("More accurate than base.en, but noticeably slower on "
                       "CPU (expect a short wait after you stop speaking)."),
        })
        recs.append({
            "model_id": "tiny.en",
            "tier": "light",
            "reason": ("Fastest option and very light on resources. Lower "
                       "accuracy, but great on older or low-power CPUs."),
        })
        return recs

    gb = vram / 1024.0

    if vram < 4096:
        # ~2-3 GB cards.
        recs.append({
            "model_id": "small.en",
            "tier": "recommended",
            "reason": ("%s has about %.1f GB of VRAM. small.en gives accurate, "
                       "fast GPU dictation and fits comfortably."
                       % (gpu_name, gb)),
        })
        recs.append({
            "model_id": "base.en",
            "tier": "light",
            "reason": "Even faster and uses less VRAM; slightly less accurate.",
        })
        recs.append({
            "model_id": "medium.en",
            "tier": "max",
            "reason": ("Highest accuracy in this range, but may be tight on "
                       "VRAM. Falls back to CPU if it doesn't fit."),
        })
    elif vram < 6144:
        # 4-5 GB cards (e.g. RTX 3050 Laptop 4GB - the dev machine).
        recs.append({
            "model_id": "small.en",
            "tier": "recommended",
            "reason": ("%s has about %.1f GB of VRAM. small.en is fast, "
                       "accurate, and leaves headroom for other apps."
                       % (gpu_name, gb)),
        })
        recs.append({
            "model_id": "medium.en",
            "tier": "max",
            "reason": ("Most accurate model that still fits ~4 GB at int8. A "
                       "little slower and uses most of the VRAM."),
        })
        recs.append({
            "model_id": "base.en",
            "tier": "light",
            "reason": "Fastest option with the lightest VRAM footprint.",
        })
    elif vram < 10240:
        # 6-9 GB cards.
        recs.append({
            "model_id": "medium.en",
            "tier": "recommended",
            "reason": ("%s has about %.1f GB of VRAM. medium.en gives excellent "
                       "accuracy with fast GPU transcription." % (gpu_name, gb)),
        })
        recs.append({
            "model_id": "distil-large-v3",
            "tier": "max",
            "reason": ("Near large-v3 accuracy but much faster and lighter on "
                       "VRAM - a great high-end pick for this card."),
        })
        recs.append({
            "model_id": "small.en",
            "tier": "light",
            "reason": "Fastest, leaves the most VRAM free for other apps.",
        })
    else:
        # 10 GB+ cards.
        recs.append({
            "model_id": "large-v3",
            "tier": "recommended",
            "reason": ("%s has about %.1f GB of VRAM - plenty for large-v3, the "
                       "most accurate model, with fast GPU transcription."
                       % (gpu_name, gb)),
        })
        recs.append({
            "model_id": "distil-large-v3",
            "tier": "max",
            "reason": ("Almost as accurate as large-v3 but noticeably faster "
                       "and lighter - the best choice if you want minimal lag."),
        })
        recs.append({
            "model_id": "medium.en",
            "tier": "light",
            "reason": "Fast, accurate, and frees up VRAM for other GPU apps.",
        })

    return recs


# ---------------------------------------------------------------------------
# Manual smoke test
# ---------------------------------------------------------------------------
if __name__ == "__main__":  # pragma: no cover
    import json
    hw = detect_hardware()
    print(json.dumps(hw, indent=2))
    print("\nRecommended models:")
    for r in recommend_models(hw):
        print("  [%s] %s\n      %s" % (r["tier"], r["model_id"], r["reason"]))
