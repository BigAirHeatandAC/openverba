"""
Tests for voiceflow.history -- the local transcript history store.

Hermetic: the history file path is redirected into a tmp dir (so the real
%LOCALAPPDATA%\\VoiceFlow\\history.jsonl is never touched), and no model, GUI, or
OS clipboard is involved. Covers the append -> load round-trip (newest-first),
edit rewrite, bad-line tolerance, clear, the line cap, and the never-raise
contract.
"""

from __future__ import annotations

import os
import json

import pytest

import voiceflow.history as H


@pytest.fixture
def hist_path(tmp_path, monkeypatch):
    """Redirect the history file (and ensure_data_dir) into a tmp dir."""
    p = os.path.join(str(tmp_path), "history.jsonl")
    monkeypatch.setattr(H, "HISTORY_PATH", p)
    monkeypatch.setattr(H, "ensure_data_dir", lambda: str(tmp_path))
    # Never probe the foreground window in tests.
    monkeypatch.setattr(H, "_detect_source_app", lambda: None)
    return p


def test_append_and_load_roundtrip(hist_path):
    a = H.append("first transcript", "dictation")
    b = H.append("second transcript", "streaming")
    assert a and b and a != b
    recs = H.load()
    assert len(recs) == 2
    # Newest-first.
    assert recs[0]["original"] == "second transcript"
    assert recs[1]["original"] == "first transcript"
    assert recs[0]["mode"] == "streaming"
    assert recs[0]["edited"] is None
    assert "ts" in recs[0] and isinstance(recs[0]["ts"], float)


def test_append_rstrips_and_skips_empty(hist_path):
    assert H.append("   ", "dictation") is None
    assert H.append("", "dictation") is None
    assert H.append(None, "dictation") is None
    rid = H.append("  padded text  ", "dictation")
    assert rid is not None
    recs = H.load()
    assert len(recs) == 1
    assert recs[0]["original"] == "padded text"


def test_update_edit(hist_path):
    rid = H.append("meet me at big air", "dictation")
    assert H.update_edit(rid, "Meet me at Big Air") is True
    recs = H.load()
    assert recs[0]["edited"] == "Meet me at Big Air"
    # original is preserved (reversibility).
    assert recs[0]["original"] == "meet me at big air"


def test_update_edit_unknown_id(hist_path):
    H.append("hello", "dictation")
    assert H.update_edit("nope-does-not-exist", "x") is False


def test_load_tolerates_bad_lines(hist_path):
    H.append("good one", "dictation")
    # Inject a corrupt line + a non-dict JSON line.
    with open(hist_path, "a", encoding="utf-8") as f:
        f.write("this is not json\n")
        f.write("[1, 2, 3]\n")
        f.write("\n")
    H.append("good two", "dictation")
    recs = H.load()
    assert [r["original"] for r in recs] == ["good two", "good one"]


def test_load_limit(hist_path):
    for i in range(10):
        H.append("line %d" % i, "dictation")
    recs = H.load(limit=3)
    assert len(recs) == 3
    assert recs[0]["original"] == "line 9"


def test_clear(hist_path):
    H.append("x", "dictation")
    assert os.path.exists(hist_path)
    assert H.clear() is True
    assert not os.path.exists(hist_path)
    assert H.load() == []


def test_load_missing_file_returns_empty(hist_path):
    assert H.load() == []


def test_cap_enforced(hist_path, monkeypatch):
    monkeypatch.setattr(H, "HISTORY_MAX_LINES", 10)
    monkeypatch.setattr(H, "HISTORY_KEEP_LINES", 4)
    for i in range(15):
        H.append("line %d" % i, "dictation")
    with open(hist_path, "r", encoding="utf-8") as f:
        lines = [ln for ln in f.read().splitlines() if ln.strip()]
    # After crossing the cap, the file was rewritten to the keep size, then a
    # few more appends followed -> bounded well under MAX, never the full 15.
    assert len(lines) <= 10
    recs = H.load()
    assert recs[0]["original"] == "line 14"


def test_append_never_raises(monkeypatch):
    # Force ensure_data_dir to blow up; append must swallow it and return None.
    monkeypatch.setattr(H, "ensure_data_dir",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert H.append("x", "dictation") is None


def test_record_is_valid_json(hist_path):
    H.append("unicode: café — naïve", "dictation")
    with open(hist_path, "r", encoding="utf-8") as f:
        line = f.readline().strip()
    rec = json.loads(line)
    assert rec["original"] == "unicode: café — naïve"
    assert set(rec) >= {"id", "ts", "mode", "original", "edited", "source_app"}
