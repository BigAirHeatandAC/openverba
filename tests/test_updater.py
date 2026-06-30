"""
Hermetic tests for voiceflow.updater - NO network, NO subprocess, NO real exit.

We exercise:
  * parse_version / is_newer ordering (equal, upgrade, downgrade, prefixes,
    pre-release suffixes, junk).
  * fetch_manifest validation by monkeypatching urllib.request.urlopen with a
    fake response (good manifest, missing required keys, non-https url rejected,
    oversized body rejected).
  * download() sha256 verify pass/fail against a real temp file, with urlopen
    faked to stream bytes (no network).
  * check_for_updates returning up_to_date when remote <= local and
    update_available when remote > local (fetch_manifest monkeypatched);
    save_config is stubbed so nothing is written to disk.
"""

from __future__ import annotations

import hashlib
import io
import json

import pytest

from voiceflow import updater


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class _FakeResp:
    """Minimal context-manager stand-in for the object urlopen returns."""

    def __init__(self, body: bytes, headers=None):
        self._buf = io.BytesIO(body)
        self.headers = headers or {}

    def read(self, n=-1):
        return self._buf.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _patch_urlopen(monkeypatch, body: bytes, headers=None):
    def fake_urlopen(req, timeout=None):
        return _FakeResp(body, headers)
    monkeypatch.setattr(updater.urllib.request, "urlopen", fake_urlopen)


# ---------------------------------------------------------------------------
# parse_version / is_newer
# ---------------------------------------------------------------------------
def test_parse_version_basic():
    assert updater.parse_version("1.2.3") == (1, 2, 3)
    assert updater.parse_version("v1.2.3") == (1, 2, 3)
    assert updater.parse_version("1.2") == (1, 2, 0)
    assert updater.parse_version("1") == (1, 0, 0)
    assert updater.parse_version("1.2.3-beta.1") == (1, 2, 3)
    assert updater.parse_version("1.2.3+build7") == (1, 2, 3)
    assert updater.parse_version("") == (0, 0, 0)
    assert updater.parse_version(None) == (0, 0, 0)
    assert updater.parse_version("garbage") == (0, 0, 0)


def test_is_newer_ordering():
    # strictly newer
    assert updater.is_newer("1.1.0", "1.0.0") is True
    assert updater.is_newer("2.0.0", "1.9.9") is True
    assert updater.is_newer("1.0.1", "1.0.0") is True
    assert updater.is_newer("1.1.0", "1.0.5") is True
    # equal -> not newer
    assert updater.is_newer("1.0.0", "1.0.0") is False
    assert updater.is_newer("v1.0.0", "1.0.0") is False
    # downgrade -> not newer
    assert updater.is_newer("1.0.0", "1.1.0") is False
    assert updater.is_newer("0.9.0", "1.0.0") is False
    # prerelease compares equal to the release on the numeric tuple (fail-safe)
    assert updater.is_newer("1.0.0-rc1", "1.0.0") is False


# ---------------------------------------------------------------------------
# fetch_manifest
# ---------------------------------------------------------------------------
_GOOD = {
    "version": "1.1.0",
    "url": "https://openverba.com/download/OpenVerba-Setup-1.1.0.exe",
    "sha256": "a" * 64,
    "size": 76812345,
    "notes": "Faster cold start.",
    "mandatory": False,
    "min_version": "1.0.0",
    "pub_date": "2026-07-01T12:00:00Z",
}


def test_fetch_manifest_good(monkeypatch):
    _patch_urlopen(monkeypatch, json.dumps(_GOOD).encode("utf-8"))
    info = updater.fetch_manifest("https://openverba.com/latest.json")
    assert info is not None
    assert info.version == "1.1.0"
    assert info.url.startswith("https://")
    assert info.sha256 == "a" * 64
    assert info.size == 76812345
    assert info.min_version == "1.0.0"
    assert info.mandatory is False


def test_fetch_manifest_missing_keys(monkeypatch):
    for bad in (
        {"url": "https://x/y.exe", "sha256": "a" * 64},          # no version
        {"version": "1.1.0", "sha256": "a" * 64},                # no url
        {"version": "1.1.0", "url": "https://x/y.exe"},          # no sha256
        {"version": "1.1.0", "url": "https://x/y.exe",
         "sha256": "tooshort"},                                  # bad sha length
        {"version": "1.1.0", "url": "https://x/y.exe",
         "sha256": "z" * 64},                                    # non-hex sha
    ):
        _patch_urlopen(monkeypatch, json.dumps(bad).encode("utf-8"))
        assert updater.fetch_manifest("https://openverba.com/latest.json") is None


def test_fetch_manifest_rejects_non_https_manifest_url(monkeypatch):
    # Should bail before ever calling urlopen.
    called = {"n": 0}

    def boom(req, timeout=None):
        called["n"] += 1
        raise AssertionError("urlopen must not be called for non-https")
    monkeypatch.setattr(updater.urllib.request, "urlopen", boom)
    assert updater.fetch_manifest("http://openverba.com/latest.json") is None
    assert called["n"] == 0


def test_fetch_manifest_rejects_non_https_download_url(monkeypatch):
    bad = dict(_GOOD)
    bad["url"] = "http://openverba.com/download/x.exe"  # not https
    _patch_urlopen(monkeypatch, json.dumps(bad).encode("utf-8"))
    assert updater.fetch_manifest("https://openverba.com/latest.json") is None


def test_fetch_manifest_oversized_body_rejected(monkeypatch):
    big = b'{"x":"' + b"a" * (updater._MAX_MANIFEST_BYTES + 100) + b'"}'
    _patch_urlopen(monkeypatch, big)
    assert updater.fetch_manifest("https://openverba.com/latest.json") is None


# ---------------------------------------------------------------------------
# download (sha256 verify)
# ---------------------------------------------------------------------------
def test_download_verifies_sha256_pass(monkeypatch, tmp_path):
    payload = b"PRETEND-INSTALLER-BYTES" * 1000
    good_sha = hashlib.sha256(payload).hexdigest()
    _patch_urlopen(monkeypatch, payload,
                   headers={"Content-Length": str(len(payload))})

    seen = []
    path = updater.download(
        "https://openverba.com/download/OpenVerba-Setup-1.1.0.exe",
        good_sha, dest=str(tmp_path),
        progress_cb=lambda frac, done, total: seen.append((frac, done, total)))
    assert path is not None
    assert path.endswith(".exe")
    with open(path, "rb") as fh:
        assert fh.read() == payload
    # progress callback fired and reported the full size at the end
    assert seen and seen[-1][1] == len(payload)


def test_download_sha256_mismatch_deletes_file(monkeypatch, tmp_path):
    payload = b"these-bytes-do-not-match-the-hash"
    wrong_sha = "b" * 64
    _patch_urlopen(monkeypatch, payload,
                   headers={"Content-Length": str(len(payload))})
    path = updater.download(
        "https://openverba.com/download/OpenVerba-Setup-1.1.0.exe",
        wrong_sha, dest=str(tmp_path))
    assert path is None
    # the bad partial must have been removed
    assert list(tmp_path.iterdir()) == []


def test_download_rejects_non_https(monkeypatch, tmp_path):
    def boom(req, timeout=None):
        raise AssertionError("urlopen must not be called for non-https")
    monkeypatch.setattr(updater.urllib.request, "urlopen", boom)
    assert updater.download("http://x/y.exe", "a" * 64, dest=str(tmp_path)) is None


def test_download_rejects_bad_sha_arg(monkeypatch, tmp_path):
    def boom(req, timeout=None):
        raise AssertionError("urlopen must not be called for bad sha arg")
    monkeypatch.setattr(updater.urllib.request, "urlopen", boom)
    assert updater.download("https://x/y.exe", "short", dest=str(tmp_path)) is None


# ---------------------------------------------------------------------------
# check_for_updates
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _no_disk_writes(monkeypatch):
    """Never write config.json from check_for_updates during tests."""
    from voiceflow import config as vf_config
    monkeypatch.setattr(vf_config, "save_config", lambda cfg: True)


def _cfg():
    return {
        "auto_update_check": True,
        "update_manifest_url": "https://openverba.com/latest.json",
        "last_update_check": 0,
    }


def test_check_up_to_date_when_remote_not_newer(monkeypatch):
    monkeypatch.setattr(updater, "get_current_version", lambda: "1.5.0")
    monkeypatch.setattr(updater, "fetch_manifest", lambda url=None, timeout=10.0:
                        updater.UpdateInfo(version="1.0.0",
                                           url="https://o/x.exe",
                                           sha256="a" * 64))
    res = updater.check_for_updates(_cfg(), interactive=True)
    assert res.status == "up_to_date"
    assert res.available is False
    assert res.current == "1.5.0"


def test_check_update_available_when_remote_newer(monkeypatch):
    monkeypatch.setattr(updater, "get_current_version", lambda: "1.0.0")
    monkeypatch.setattr(updater, "fetch_manifest", lambda url=None, timeout=10.0:
                        updater.UpdateInfo(version="1.1.0",
                                           url="https://o/x.exe",
                                           sha256="a" * 64,
                                           notes="new stuff",
                                           min_version="1.0.0"))
    res = updater.check_for_updates(_cfg(), interactive=True)
    assert res.status == "update_available"
    assert res.available is True
    assert res.version == "1.1.0"
    assert res.notes == "new stuff"
    assert res.info is not None and res.info.url == "https://o/x.exe"


def test_check_mandatory_via_min_version(monkeypatch):
    monkeypatch.setattr(updater, "get_current_version", lambda: "0.9.0")
    monkeypatch.setattr(updater, "fetch_manifest", lambda url=None, timeout=10.0:
                        updater.UpdateInfo(version="1.1.0",
                                           url="https://o/x.exe",
                                           sha256="a" * 64,
                                           mandatory=False,
                                           min_version="1.0.0"))
    res = updater.check_for_updates(_cfg(), interactive=True)
    assert res.status == "update_available"
    assert res.mandatory is True


def test_check_skipped_when_auto_off_and_background(monkeypatch):
    # Background (non-interactive) + auto_update_check off -> never fetch.
    def boom(*a, **k):
        raise AssertionError("must not fetch when auto-check off in background")
    monkeypatch.setattr(updater, "fetch_manifest", boom)
    cfg = _cfg()
    cfg["auto_update_check"] = False
    res = updater.check_for_updates(cfg, interactive=False)
    assert res.status == "up_to_date"


def test_check_error_when_manifest_unreachable(monkeypatch):
    monkeypatch.setattr(updater, "get_current_version", lambda: "1.0.0")
    monkeypatch.setattr(updater, "fetch_manifest", lambda url=None, timeout=10.0: None)
    res = updater.check_for_updates(_cfg(), interactive=True)
    assert res.status == "error"
    assert res.error


def test_due_for_check(monkeypatch):
    import time as _t
    now = _t.time()
    assert updater.due_for_check({"auto_update_check": True,
                                  "last_update_check": 0}) is True
    assert updater.due_for_check({"auto_update_check": True,
                                  "last_update_check": now}) is False
    assert updater.due_for_check({"auto_update_check": False,
                                  "last_update_check": 0}) is False
