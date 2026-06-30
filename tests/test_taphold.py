"""Tap-vs-hold chord: a quick tap fires on_tap (dictate); holding past the
threshold fires on_hold_start then on_hold_end on release (command mode)."""

import sys

import pytest

if sys.platform != "win32":
    pytest.skip("Windows-only mouse hook", allow_module_level=True)

from voiceflow.platform import windows as w


def _hook(ev, monkeypatch):
    monkeypatch.setattr(w, "_synth_mouse", lambda f: None)
    return w.TapHoldChordHook(
        lambda: ev.__setitem__("tap", ev["tap"] + 1),
        lambda: ev.__setitem__("hs", ev["hs"] + 1),
        lambda: ev.__setitem__("he", ev["he"] + 1),
        "left", "right")


def test_tap_fires_on_tap(monkeypatch):
    ev = {"tap": 0, "hs": 0, "he": 0}
    h = _hook(ev, monkeypatch)
    LD, LU, RD, RU = (w._WM_LBUTTONDOWN, w._WM_LBUTTONUP,
                      w._WM_RBUTTONDOWN, w._WM_RBUTTONUP)
    h._handle(LD, 1, 1); h._handle(RD, 1, 1)   # chord engaged
    h._handle(LU, 1, 1); h._handle(RU, 1, 1)   # released before hold -> tap
    h._cancel_hold(); h._cancel_timer()
    assert ev == {"tap": 1, "hs": 0, "he": 0}


def test_hold_fires_start_then_end(monkeypatch):
    ev = {"tap": 0, "hs": 0, "he": 0}
    h = _hook(ev, monkeypatch)
    LD, LU, RD, RU = (w._WM_LBUTTONDOWN, w._WM_LBUTTONUP,
                      w._WM_RBUTTONDOWN, w._WM_RBUTTONUP)
    h._handle(LD, 1, 1); h._handle(RD, 1, 1)   # chord engaged
    h._on_hold_timeout()                       # held past threshold
    h._handle(LU, 1, 1); h._handle(RU, 1, 1)   # release -> command end
    h._cancel_hold(); h._cancel_timer()
    assert ev == {"tap": 0, "hs": 1, "he": 1}
