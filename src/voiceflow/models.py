"""
voiceflow.models - the faster-whisper model catalog + download / install state.

This module owns the canonical list of models VoiceFlow offers (MODEL_CATALOG),
maps a friendly model id (e.g. "small.en", "distil-large-v3") to its HuggingFace
repo (Systran/faster-whisper-* and Systran/faster-distil-whisper-*), and handles
downloading / locating models in the HuggingFace cache.

It does NOT import faster_whisper / ctranslate2 / the engine, so it is safe to
import in the GUI before any CUDA setup. Downloads use huggingface_hub's
snapshot_download with retry (the network on first run is often flaky).

Public API
----------
MODEL_CATALOG : list[dict]
    Each entry has: id, label, repo, params, disk_mb, vram_mb_int8,
    languages ("en"|"multi"), quality (1-5), speed (1-5), notes.

get_model(model_id) -> dict | None
catalog_ids() -> list[str]

is_downloaded(model_id, download_root=None) -> bool
download_model(model_id, progress_cb=None, download_root=None) -> str
    Downloads to the HF cache (or download_root) with retries. progress_cb is
    called as progress_cb(fraction_0_to_1, downloaded_bytes, total_bytes, desc).
    Returns the local snapshot path. Raises on permanent failure.

local_model_path(model_id, download_root=None) -> str | None
model_disk_size_mb(model_id, download_root=None) -> float | None
list_installed(download_root=None) -> list[dict]
delete_model(model_id, download_root=None) -> bool
"""

from __future__ import annotations

import os
import time
import shutil
import logging

log = logging.getLogger("voiceflow.models")

__all__ = [
    "MODEL_CATALOG",
    "get_model",
    "catalog_ids",
    "is_downloaded",
    "download_model",
    "local_model_path",
    "model_disk_size_mb",
    "list_installed",
    "delete_model",
    "repo_for",
]


# ---------------------------------------------------------------------------
# The catalog. disk_mb / vram_mb_int8 are realistic approximations for the
# faster-whisper (CTranslate2) builds; vram_mb_int8 is the rough working-set
# VRAM at int8/int8_float16, the compute types VoiceFlow uses on GPU.
# quality / speed are relative 1 (low) - 5 (high) ratings for dictation.
# ---------------------------------------------------------------------------
MODEL_CATALOG = [
    {
        "id": "tiny.en",
        "label": "Tiny (English)",
        "repo": "Systran/faster-whisper-tiny.en",
        "params": "39M",
        "disk_mb": 75,
        "vram_mb_int8": 350,
        "languages": "en",
        "quality": 2,
        "speed": 5,
        "notes": "Fastest and lightest. Good on weak CPUs; lower accuracy.",
    },
    {
        "id": "base.en",
        "label": "Base (English)",
        "repo": "Systran/faster-whisper-base.en",
        "params": "74M",
        "disk_mb": 145,
        "vram_mb_int8": 500,
        "languages": "en",
        "quality": 3,
        "speed": 5,
        "notes": "Great speed/accuracy balance for CPU dictation. Recommended "
                 "default when there is no GPU.",
    },
    {
        "id": "small.en",
        "label": "Small (English)",
        "repo": "Systran/faster-whisper-small.en",
        "params": "244M",
        "disk_mb": 480,
        "vram_mb_int8": 1100,
        "languages": "en",
        "quality": 4,
        "speed": 4,
        "notes": "Accurate and still fast, especially on a GPU. The sweet spot "
                 "for most modern PCs with a 4 GB+ NVIDIA card.",
    },
    {
        "id": "distil-small.en",
        "label": "Distil-Small (English)",
        "repo": "Systran/faster-distil-whisper-small.en",
        "params": "166M",
        "disk_mb": 340,
        "vram_mb_int8": 900,
        "languages": "en",
        "quality": 4,
        "speed": 5,
        "notes": "Distilled small model: near small.en accuracy but faster and "
                 "lighter. Good low-latency English pick.",
    },
    {
        "id": "medium.en",
        "label": "Medium (English)",
        "repo": "Systran/faster-whisper-medium.en",
        "params": "769M",
        "disk_mb": 1530,
        "vram_mb_int8": 2600,
        "languages": "en",
        "quality": 5,
        "speed": 3,
        "notes": "Very high accuracy. Comfortable on 6 GB+ GPUs; fits ~4 GB at "
                 "int8 but uses most of the VRAM. Slow on CPU.",
    },
    {
        "id": "distil-large-v3",
        "label": "Distil-Large v3 (English)",
        "repo": "Systran/faster-distil-whisper-large-v3",
        "params": "756M",
        "disk_mb": 1520,
        "vram_mb_int8": 2800,
        "languages": "en",
        "quality": 5,
        "speed": 4,
        "notes": "Distilled large-v3: close to large-v3 accuracy but much "
                 "faster and lighter. Best high-end English pick.",
    },
    {
        "id": "large-v3",
        "label": "Large v3 (Multilingual)",
        "repo": "Systran/faster-whisper-large-v3",
        "params": "1.55B",
        "disk_mb": 3090,
        "vram_mb_int8": 4700,
        "languages": "multi",
        "quality": 5,
        "speed": 2,
        "notes": "Best accuracy and full multilingual support. Needs a 6 GB+ "
                 "(ideally 8-10 GB) GPU; very slow on CPU.",
    },
]

_BY_ID = {m["id"]: m for m in MODEL_CATALOG}


# ---------------------------------------------------------------------------
# Catalog lookups
# ---------------------------------------------------------------------------
def get_model(model_id):
    """Return the catalog dict for model_id, or None if unknown."""
    return _BY_ID.get(model_id)


def catalog_ids():
    """Return the list of all known model ids (catalog order)."""
    return [m["id"] for m in MODEL_CATALOG]


def repo_for(model_id):
    """Return the HuggingFace repo id for a model id.

    Known catalog ids resolve to their declared repo. Unknown ids are mapped
    using the faster-whisper naming convention (so the engine's own model
    strings like "small"/"large-v3" still work): a bare name becomes
    Systran/faster-whisper-<name>; a "distil-..." name becomes
    Systran/faster-distil-whisper-<rest>.
    """
    m = _BY_ID.get(model_id)
    if m:
        return m["repo"]
    name = (model_id or "").strip()
    if not name:
        return None
    low = name.lower()
    if low.startswith("distil-"):
        return "Systran/faster-distil-whisper-" + name[len("distil-"):]
    if "/" in name:
        return name  # already a full repo id
    return "Systran/faster-whisper-" + name


# ---------------------------------------------------------------------------
# HuggingFace cache resolution.
#
# Convention (matches voiceflow.constants / config.resolve_download_root):
#   - download_root, when provided by the caller (the GUI passes
#     config.resolve_download_root(cfg)), IS the cache dir used directly.
#   - when download_root is None we default to the app's per-user MODELS_DIR
#     (%LOCALAPPDATA%\VoiceFlow\models) via a SOFT import of voiceflow.constants
#     so this module keeps no hard dependency. If constants can't be imported we
#     fall back to the standard HuggingFace cache (HF env vars / ~/.cache).
#
# For *lookups* (is_downloaded/size/list/delete) we additionally check the
# standard HF cache as a fallback, so a model that was downloaded by the engine
# into the default HF cache is still found even before the app set a MODELS_DIR.
# ---------------------------------------------------------------------------
def _app_models_dir():
    """The app's canonical models dir, or None if constants isn't importable."""
    try:
        from . import constants  # soft, optional dependency
        return getattr(constants, "MODELS_DIR", None)
    except Exception:
        return None


def _default_hf_cache():
    """Standard HuggingFace hub cache: HF env vars, then ~/.cache/huggingface/hub."""
    for env in ("HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE"):
        v = os.environ.get(env)
        if v:
            return v
    hf_home = os.environ.get("HF_HOME")
    if hf_home:
        return os.path.join(hf_home, "hub")
    return os.path.join(os.path.expanduser("~"), ".cache", "huggingface", "hub")


def _hf_cache_dir(download_root=None):
    """Return the cache dir snapshot_download should DOWNLOAD into (cache_dir=)."""
    if download_root:
        return download_root
    app = _app_models_dir()
    if app:
        return app
    return None  # let HF resolve its own default


def _candidate_hub_roots(download_root=None):
    """Directories that may contain 'models--Org--Name' folders, in priority
    order, for LOOKUPS. Deduped, preserves order."""
    roots = []
    if download_root:
        roots.append(download_root)
    else:
        app = _app_models_dir()
        if app:
            roots.append(app)
        roots.append(_default_hf_cache())
    seen = set()
    out = []
    for r in roots:
        if r and r not in seen:
            seen.add(r)
            out.append(r)
    return out


def _hub_root(download_root=None):
    """Primary directory for WRITES/DELETES: where snapshot_download lays out
    'models--Org--Name'. (For reads, use _candidate_hub_roots.)"""
    roots = _candidate_hub_roots(download_root)
    return roots[0] if roots else _default_hf_cache()


def _repo_folder_name(repo_id):
    """HF cache folder naming: 'Org/Name' -> 'models--Org--Name'."""
    return "models--" + repo_id.replace("/", "--")


def _snapshot_dir(repo_id, download_root=None):
    """Return the path to the most recent COMPLETE local snapshot for repo_id
    across all candidate cache roots, or None.

    The HF cache stores snapshots under
        <hub_root>/models--Org--Name/snapshots/<commit_hash>/
    where the actual files are (usually) symlinks/refs into ../../blobs.
    We prefer a complete snapshot; if none is complete we return the newest one
    found (so callers can still report a partial/in-progress location).
    """
    all_candidates = []
    for hub in _candidate_hub_roots(download_root):
        repo_dir = os.path.join(hub, _repo_folder_name(repo_id))
        snaps = os.path.join(repo_dir, "snapshots")
        if not os.path.isdir(snaps):
            continue
        try:
            for name in os.listdir(snaps):
                full = os.path.join(snaps, name)
                if os.path.isdir(full):
                    all_candidates.append(full)
        except Exception:
            continue
    if not all_candidates:
        return None
    # Newest first (handles multiple revisions / multiple roots).
    try:
        all_candidates.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    except Exception:
        pass
    # Prefer the newest COMPLETE snapshot; else fall back to newest overall.
    for c in all_candidates:
        if _snapshot_is_complete(c):
            return c
    return all_candidates[0]


# The files faster-whisper needs from a CTranslate2 model snapshot. We consider a
# model "downloaded" only when both the weights and the tokenizer/config exist.
_REQUIRED_ANY = (("model.bin",),)
_REQUIRED_TOKENIZER_ANY = (("tokenizer.json",), ("vocabulary.txt", "vocabulary.json"))


def _snapshot_is_complete(snap_dir):
    if not snap_dir or not os.path.isdir(snap_dir):
        return False

    def _present(name):
        p = os.path.join(snap_dir, name)
        # In the HF cache these are symlinks into ../blobs; islink/exists both ok.
        return os.path.exists(p) or os.path.islink(p)

    # Weights must exist and (when resolvable) be non-empty.
    if not _present("model.bin"):
        return False
    try:
        real = os.path.realpath(os.path.join(snap_dir, "model.bin"))
        if os.path.exists(real) and os.path.getsize(real) <= 0:
            return False
    except Exception:
        pass

    # Some kind of tokenizer/config must exist.
    if not (_present("tokenizer.json")
            or _present("vocabulary.txt")
            or _present("vocabulary.json")):
        return False
    return True


# ---------------------------------------------------------------------------
# Public: install state
# ---------------------------------------------------------------------------
def local_model_path(model_id, download_root=None):
    """Return the local snapshot directory for model_id if it is fully
    downloaded, else None."""
    repo = repo_for(model_id)
    if not repo:
        return None
    snap = _snapshot_dir(repo, download_root)
    if _snapshot_is_complete(snap):
        return snap
    return None


def is_downloaded(model_id, download_root=None):
    """True if model_id is present and complete in the cache."""
    return local_model_path(model_id, download_root) is not None


def model_disk_size_mb(model_id, download_root=None):
    """Actual on-disk size (MB) of a downloaded model, following symlinks into
    the HF blob store. Returns None if the model isn't downloaded."""
    snap = local_model_path(model_id, download_root)
    if not snap:
        return None
    total = 0
    seen = set()
    try:
        for root, _dirs, files in os.walk(snap):
            for fn in files:
                p = os.path.join(root, fn)
                try:
                    real = os.path.realpath(p)
                    if real in seen:
                        continue
                    seen.add(real)
                    total += os.path.getsize(real)
                except Exception:
                    continue
    except Exception:
        return None
    return round(total / (1024 * 1024), 1)


def list_installed(download_root=None):
    """Return a list of installed catalog models with their on-disk info:
    [{"id", "label", "repo", "path", "disk_mb"}]. Catalog order."""
    out = []
    for m in MODEL_CATALOG:
        path = local_model_path(m["id"], download_root)
        if path:
            out.append({
                "id": m["id"],
                "label": m["label"],
                "repo": m["repo"],
                "path": path,
                "disk_mb": model_disk_size_mb(m["id"], download_root),
            })
    return out


def delete_model(model_id, download_root=None):
    """Remove a model's entire cache folder (snapshots + blobs + refs).

    Returns True if something was deleted, False if it wasn't present. Raises on
    a real filesystem error so the GUI can surface it.
    """
    repo = repo_for(model_id)
    if not repo:
        return False
    deleted = False
    for hub in _candidate_hub_roots(download_root):
        repo_dir = os.path.join(hub, _repo_folder_name(repo))
        if os.path.isdir(repo_dir):
            shutil.rmtree(repo_dir)
            deleted = True
    return deleted


# ---------------------------------------------------------------------------
# Download with progress + retry.
#
# huggingface_hub 1.x uses the tqdm_class we pass in TWO ways inside
# snapshot_download:
#   1) as the shared "bytes downloaded" aggregate bar (one instance whose
#      .total is incremented per file and whose .update(n) receives every byte
#      across all files/threads), and
#   2) as the class handed to thread_map(), which calls CLASSMETHODS on it
#      (get_lock / set_lock / etc.).
# Re-implementing all of that by hand is fragile (it broke on get_lock). The
# robust approach is to SUBCLASS the real tqdm so we inherit every classmethod
# and internal (get_lock/set_lock for thread_map), and override the byte hooks
# to forward aggregate progress to the user's callback.
#
# IMPORTANT: we must NOT pass disable=True. A disabled tqdm short-circuits
# update() and never advances self.n, so we'd always report 0 bytes done.
# Instead we keep tqdm "enabled" but route its rendering to os.devnull (so
# nothing prints in a windowed/no-console app) and track downloaded bytes in our
# own counter inside update(). total comes from self.total, which hf_hub sets
# directly (bytes_progress.total += file_size) as each file size is learned.
# ---------------------------------------------------------------------------
def _make_progress_tqdm(progress_cb, default_desc=""):
    """Build a tqdm subclass that forwards aggregate progress to progress_cb as
    progress_cb(fraction_0_to_1, downloaded_bytes, total_bytes, desc). Returns
    None if a real tqdm isn't importable (caller then downloads without a bar)."""
    try:
        from tqdm.auto import tqdm as _base_tqdm
    except Exception:
        try:
            from tqdm import tqdm as _base_tqdm
        except Exception:
            return None

    _devnull = open(os.devnull, "w")

    class _ProgressTqdm(_base_tqdm):
        def __init__(self, *args, **kwargs):
            # Render to /dev/null so nothing prints, but stay enabled so the
            # base class keeps maintaining self.total. We count bytes ourselves.
            kwargs["file"] = _devnull
            kwargs.setdefault("mininterval", 0)   # don't throttle our callback
            kwargs["leave"] = False
            self._done_bytes = 0
            super().__init__(*args, **kwargs)
            self._emit()

        def _emit(self):
            try:
                total = int(self.total or 0)
                done = int(self._done_bytes)
                frac = (done / total) if total else 0.0
                if frac > 1.0:
                    frac = 1.0
                desc = getattr(self, "desc", None) or default_desc
                progress_cb(frac, done, total, desc)
            except Exception:
                pass

        def update(self, n=1):
            try:
                self._done_bytes += int(n or 0)
            except Exception:
                pass
            try:
                ret = super().update(n)
            except Exception:
                ret = None
            self._emit()
            return ret

        def refresh(self, *a, **k):
            try:
                ret = super().refresh(*a, **k)
            except Exception:
                ret = None
            self._emit()
            return ret

        def set_description(self, desc=None, *a, **k):
            try:
                super().set_description(desc, *a, **k)
            except Exception:
                pass
            self._emit()

    return _ProgressTqdm


def download_model(model_id, progress_cb=None, download_root=None,
                   max_retries=10):
    """Download model_id into the HF cache (or download_root) with retries.

    progress_cb(fraction, downloaded_bytes, total_bytes, desc) is called as bytes
    arrive (fraction is 0..1). It is best-effort: any exception in the callback
    is swallowed.

    Returns the local snapshot directory path on success.
    Raises the last exception if all attempts fail.
    """
    repo = repo_for(model_id)
    if not repo:
        raise ValueError("Unknown model id: %r" % (model_id,))

    # Already fully present? Report complete and return immediately.
    existing = local_model_path(model_id, download_root)
    if existing:
        if progress_cb:
            try:
                progress_cb(1.0, 0, 0, "already downloaded")
            except Exception:
                pass
        return existing

    try:
        from huggingface_hub import snapshot_download
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(
            "huggingface_hub is required to download models (%s)" % exc)

    cache_dir = _hf_cache_dir(download_root)

    # Build the progress-forwarding tqdm subclass (or None if no callback / no
    # tqdm). snapshot_download maintains a single shared bytes bar, so this one
    # class instance naturally aggregates progress across all files/threads.
    tqdm_class = None
    if progress_cb is not None:
        tqdm_class = _make_progress_tqdm(progress_cb, default_desc=model_id)

    last_exc = None
    for attempt in range(1, max_retries + 1):
        try:
            if progress_cb and attempt > 1:
                try:
                    progress_cb(0.0, 0, 0,
                                "retrying (%d/%d)" % (attempt, max_retries))
                except Exception:
                    pass
            kwargs = dict(
                repo_id=repo,
                repo_type="model",
                cache_dir=cache_dir,
                # CTranslate2 weights + tokenizer/config only; skip the
                # original PyTorch .bin / .pt and unrelated junk.
                allow_patterns=[
                    "*.bin", "*.json", "*.txt", "config.json",
                    "tokenizer.json", "vocabulary.*", "preprocessor_config.json",
                ],
                max_workers=4,
            )
            if tqdm_class is not None:
                kwargs["tqdm_class"] = tqdm_class
            path = snapshot_download(**kwargs)
            # Verify it actually landed complete (guards a partial CDN read).
            if _snapshot_is_complete(path):
                if progress_cb:
                    try:
                        progress_cb(1.0, 0, 0, "done")
                    except Exception:
                        pass
                log.info("Downloaded model '%s' -> %s", model_id, path)
                return path
            # Snapshot dir returned but missing required files -> treat as a
            # retryable failure (next attempt resumes the cached blobs).
            last_exc = RuntimeError(
                "snapshot for %s incomplete after download" % model_id)
            log.warning("Model '%s' download attempt %d incomplete; retrying.",
                        model_id, attempt)
        except Exception as exc:
            last_exc = exc
            log.warning("Model '%s' download attempt %d/%d failed: %s",
                        model_id, attempt, max_retries, exc)
        # Backoff before the next try (network flakiness). Cap the wait.
        if attempt < max_retries:
            time.sleep(min(2.0 * attempt, 15.0))

    raise RuntimeError(
        "Failed to download model '%s' after %d attempts: %s"
        % (model_id, max_retries, last_exc))


# ---------------------------------------------------------------------------
# Manual smoke test
# ---------------------------------------------------------------------------
if __name__ == "__main__":  # pragma: no cover
    print("Catalog ids:", catalog_ids())
    for m in MODEL_CATALOG:
        installed = is_downloaded(m["id"])
        size = model_disk_size_mb(m["id"])
        print("  %-18s %-34s installed=%s size=%s"
              % (m["id"], m["repo"], installed, size))
    print("Installed:", [x["id"] for x in list_installed()])
