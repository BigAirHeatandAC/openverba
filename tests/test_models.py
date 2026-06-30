"""
Tests for the model catalog + recommendation matrix.

Two pure, OS-agnostic modules (plan section 3.3/3.4):
  * ``voiceflow.models.repo_for`` -- resolves a friendly model id to its
    HuggingFace repo, including the engine's bare/distil naming convention for
    ids that aren't in the catalog.
  * ``voiceflow.hardware.recommend_models`` -- maps a detected-hardware dict to an
    ordered (best-first) list of recommendations whose ``model_id``s must exist in
    the catalog.

Nothing here touches the network, the HF cache, or a GPU.
"""

from __future__ import annotations

import pytest

from voiceflow import models
from voiceflow import hardware


# ---------------------------------------------------------------------------
# Catalog basics
# ---------------------------------------------------------------------------
def test_catalog_ids_nonempty_and_unique():
    ids = models.catalog_ids()
    assert ids
    assert len(ids) == len(set(ids))


def test_get_model_known_and_unknown():
    m = models.get_model("small.en")
    assert m and m["id"] == "small.en"
    assert m["repo"] == "Systran/faster-whisper-small.en"
    assert models.get_model("does-not-exist") is None


def test_every_catalog_entry_has_required_fields():
    required = {"id", "label", "repo", "params", "disk_mb", "vram_mb_int8",
               "languages", "quality", "speed", "notes"}
    for m in models.MODEL_CATALOG:
        assert required <= set(m), m["id"]
        assert m["languages"] in ("en", "multi")
        assert 1 <= m["quality"] <= 5
        assert 1 <= m["speed"] <= 5


# ---------------------------------------------------------------------------
# repo_for(): catalog ids resolve to their declared repo
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("model_id,repo", [
    ("tiny.en", "Systran/faster-whisper-tiny.en"),
    ("base.en", "Systran/faster-whisper-base.en"),
    ("small.en", "Systran/faster-whisper-small.en"),
    ("medium.en", "Systran/faster-whisper-medium.en"),
    ("large-v3", "Systran/faster-whisper-large-v3"),
    ("distil-small.en", "Systran/faster-distil-whisper-small.en"),
    ("distil-large-v3", "Systran/faster-distil-whisper-large-v3"),
])
def test_repo_for_catalog_ids(model_id, repo):
    assert models.repo_for(model_id) == repo


def test_repo_for_catalog_matches_catalog_field():
    """repo_for must agree with each entry's own 'repo' field."""
    for m in models.MODEL_CATALOG:
        assert models.repo_for(m["id"]) == m["repo"]


# ---------------------------------------------------------------------------
# repo_for(): the faster-whisper naming convention for non-catalog ids
# (so the engine's own model strings like "small"/"large-v3" still resolve)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name,repo", [
    ("small", "Systran/faster-whisper-small"),
    ("base", "Systran/faster-whisper-base"),
    ("tiny", "Systran/faster-whisper-tiny"),
    ("large-v3-turbo", "Systran/faster-whisper-large-v3-turbo"),
])
def test_repo_for_bare_name_convention(name, repo):
    assert models.repo_for(name) == repo


@pytest.mark.parametrize("name,repo", [
    ("distil-medium.en", "Systran/faster-distil-whisper-medium.en"),
    ("distil-large-v3.5", "Systran/faster-distil-whisper-large-v3.5"),
])
def test_repo_for_distil_convention(name, repo):
    assert models.repo_for(name) == repo


def test_repo_for_full_repo_id_passthrough():
    assert models.repo_for("MyOrg/custom-whisper") == "MyOrg/custom-whisper"


@pytest.mark.parametrize("bad", [None, "", "   "])
def test_repo_for_empty_returns_none(bad):
    assert models.repo_for(bad) is None


# ---------------------------------------------------------------------------
# recommend_models(): the hardware -> model matrix.
# Every recommendation's model_id must exist in the catalog, and the tiers /
# ordering must match the plan's matrix.
# ---------------------------------------------------------------------------
def _hw(present=False, vram_mb=None, name="Test GPU"):
    return {
        "gpu": {"present": present, "name": name, "vram_mb": vram_mb,
                "cuda": None, "driver": None, "count": 1 if present else 0},
        "cpu": {"name": "Test CPU", "cores": 4, "threads": 8},
        "ram_gb": 16.0,
        "os": "Windows 11",
        "detected_via": {},
    }


def _ids(recs):
    return [r["model_id"] for r in recs]


def _assert_all_in_catalog(recs):
    catalog = set(models.catalog_ids())
    assert recs, "recommendations must not be empty"
    for r in recs:
        assert set(r) >= {"model_id", "tier", "reason"}
        assert r["tier"] in {"recommended", "max", "light"}
        assert r["model_id"] in catalog, r["model_id"]
    # exactly one 'recommended', and it leads the list (best-first)
    assert sum(1 for r in recs if r["tier"] == "recommended") == 1
    assert recs[0]["tier"] == "recommended"


def test_recommend_no_gpu():
    recs = hardware.recommend_models(_hw(present=False))
    _assert_all_in_catalog(recs)
    assert _ids(recs) == ["base.en", "small.en", "tiny.en"]


def test_recommend_gpu_present_but_no_vram_treated_as_cpu():
    """A GPU with unknown VRAM falls back to the CPU recommendation set."""
    recs = hardware.recommend_models(_hw(present=True, vram_mb=None))
    assert _ids(recs) == ["base.en", "small.en", "tiny.en"]


def test_recommend_low_vram_under_4gb():
    recs = hardware.recommend_models(_hw(present=True, vram_mb=3072))
    _assert_all_in_catalog(recs)
    assert recs[0]["model_id"] == "small.en"
    assert set(_ids(recs)) == {"small.en", "base.en", "medium.en"}


def test_recommend_4_to_5gb():
    recs = hardware.recommend_models(_hw(present=True, vram_mb=4096))
    _assert_all_in_catalog(recs)
    assert recs[0]["model_id"] == "small.en"
    assert {r["model_id"] for r in recs if r["tier"] == "max"} == {"medium.en"}


def test_recommend_6_to_9gb():
    recs = hardware.recommend_models(_hw(present=True, vram_mb=8192))
    _assert_all_in_catalog(recs)
    assert recs[0]["model_id"] == "medium.en"
    assert {r["model_id"] for r in recs if r["tier"] == "max"} == {"distil-large-v3"}


def test_recommend_10gb_plus():
    recs = hardware.recommend_models(_hw(present=True, vram_mb=12288))
    _assert_all_in_catalog(recs)
    assert recs[0]["model_id"] == "large-v3"
    assert {r["model_id"] for r in recs if r["tier"] == "max"} == {"distil-large-v3"}


@pytest.mark.parametrize("vram_mb,expected_top", [
    (2048, "small.en"),     # < 4 GB
    (4096, "small.en"),     # 4-5 GB
    (5120, "small.en"),     # 4-5 GB
    (6144, "medium.en"),    # 6-9 GB
    (9216, "medium.en"),    # 6-9 GB
    (10240, "large-v3"),    # 10 GB+
    (24576, "large-v3"),    # 10 GB+
])
def test_recommend_vram_boundaries(vram_mb, expected_top):
    recs = hardware.recommend_models(_hw(present=True, vram_mb=vram_mb))
    _assert_all_in_catalog(recs)
    assert recs[0]["model_id"] == expected_top


def test_recommend_handles_none_and_empty_hw():
    """recommend_models must never raise on a degenerate hardware dict."""
    for hw in (None, {}, {"gpu": None}, {"gpu": {}}):
        recs = hardware.recommend_models(hw)
        _assert_all_in_catalog(recs)
        # no detectable GPU -> CPU set
        assert recs[0]["model_id"] == "base.en"


def test_every_recommendation_resolves_to_a_repo():
    """A recommended id must resolve via repo_for (download path won't 404)."""
    for vram in (None, 2048, 4096, 8192, 16384):
        present = vram is not None
        for r in hardware.recommend_models(_hw(present=present, vram_mb=vram)):
            assert models.repo_for(r["model_id"])
