"""hardware.detect_hardware() session caching (B.1): the probe (nvidia-smi /
nvml / registry) runs at most once per process, so re-opening Settings is free.
"""

from voiceflow import hardware


def test_detect_hardware_cached(monkeypatch):
    """detect_hardware() probes once, then returns the cached result. force=True
    re-probes."""
    # Reset the module cache so the test is order-independent.
    monkeypatch.setattr(hardware, "_HW_CACHE", None)

    calls = {"n": 0}

    def fake_detect_gpu(via):
        calls["n"] += 1
        via["gpu"] = "fake"
        return {"present": False, "name": None, "vram_mb": None,
                "cuda": None, "driver": None, "count": 0}

    monkeypatch.setattr(hardware, "_detect_gpu", fake_detect_gpu)
    monkeypatch.setattr(hardware, "_detect_cpu",
                        lambda via: {"name": "x", "cores": 1, "threads": 1})
    monkeypatch.setattr(hardware, "_detect_ram", lambda via: 8.0)

    r1 = hardware.detect_hardware()
    r2 = hardware.detect_hardware()
    assert r1 is r2                  # same cached object
    assert calls["n"] == 1           # probed only once

    # force=True re-probes (and refreshes the cache).
    r3 = hardware.detect_hardware(force=True)
    assert calls["n"] == 2
    assert r3 is not r1
