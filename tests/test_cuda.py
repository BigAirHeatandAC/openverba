"""cuda.gpu_runtime_present() session memoization (B.1): the venv scan
(_discover_pkg_bins) runs at most once per process so Settings opens instantly.
"""

from voiceflow import cuda


def test_gpu_runtime_present_cached(monkeypatch):
    monkeypatch.setattr(cuda, "_RUNTIME_PRESENT", None)
    calls = {"n": 0}

    def fake_discover():
        calls["n"] += 1
        return {"cublas": "/x", "cudnn": "/y"}

    monkeypatch.setattr(cuda, "_discover_pkg_bins", fake_discover)

    assert cuda.gpu_runtime_present() is True
    assert cuda.gpu_runtime_present() is True
    assert calls["n"] == 1            # scanned only once

    # force=True re-scans.
    cuda.gpu_runtime_present(force=True)
    assert calls["n"] == 2


def test_gpu_runtime_present_false_when_missing(monkeypatch):
    monkeypatch.setattr(cuda, "_RUNTIME_PRESENT", None)
    monkeypatch.setattr(cuda, "_discover_pkg_bins", lambda: {"cublas": "/x"})
    assert cuda.gpu_runtime_present() is False
