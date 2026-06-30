"""Two mouse chords that share the LEFT button must dispatch to the right
callback from a single hook (left+right vs left+middle), with no stray clicks."""

import sys

import pytest

if sys.platform != "win32":  # the multi-chord hook is the Windows backend
    pytest.skip("Windows-only mouse hook", allow_module_level=True)

from voiceflow.platform import windows as w


def _msgs():
    return (w._WM_LBUTTONDOWN, w._WM_LBUTTONUP, w._WM_RBUTTONDOWN,
            w._WM_RBUTTONUP, w._WM_MBUTTONDOWN, w._WM_MBUTTONUP)


def test_shared_left_button_dispatches_per_chord(monkeypatch):
    synth = []
    monkeypatch.setattr(w, "_synth_mouse", lambda f: synth.append(f))
    LD, LU, RD, RU, MD, MU = _msgs()
    fired = {"dict": 0, "cmd": 0}
    hook = w.MultiChordHook([
        ("left", "right", lambda: fired.__setitem__("dict", fired["dict"] + 1)),
        ("left", "middle", lambda: fired.__setitem__("cmd", fired["cmd"] + 1)),
    ])
    # left+right -> dictation chord
    assert hook._handle(LD, 1, 1) is True
    assert hook._handle(RD, 1, 1) is True
    assert hook._handle(LU, 1, 1) is True
    assert hook._handle(RU, 1, 1) is True
    hook._cancel_timer()
    # left+middle -> command chord
    assert hook._handle(LD, 1, 1) is True
    assert hook._handle(MD, 1, 1) is True
    assert hook._handle(LU, 1, 1) is True
    assert hook._handle(MU, 1, 1) is True
    hook._cancel_timer()
    assert fired == {"dict": 1, "cmd": 1}
    assert synth == []   # both chords fully suppressed -> no stray clicks


def test_lone_click_of_shared_button_replays(monkeypatch):
    synth = []
    monkeypatch.setattr(w, "_synth_mouse", lambda f: synth.append(f))
    LD, LU = w._WM_LBUTTONDOWN, w._WM_LBUTTONUP
    fired = {"n": 0}
    hook = w.MultiChordHook([
        ("left", "right", lambda: fired.__setitem__("n", fired["n"] + 1)),
        ("left", "middle", lambda: fired.__setitem__("n", fired["n"] + 1)),
    ])
    # A lone left click (released before any second button) replays as a click.
    hook._handle(LD, 5, 5)
    hook._handle(LU, 5, 5)
    hook._cancel_timer()
    assert fired["n"] == 0
    assert len(synth) == 2  # synthesized down + up (clean click)
