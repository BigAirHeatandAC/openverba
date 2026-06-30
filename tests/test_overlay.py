"""
Smoke tests for the Live-preview overlay bar (voiceflow.ui.overlay.PreviewOverlay).

These are headless-guarded: they need a real Tk display + customtkinter. On a CI
runner without a display (the macOS/Linux GitHub Actions runners) the whole
module is skipped, so it can never break the green suite. The load-bearing
preview tests live in test_dictation.py / test_streaming.py (no GUI required).
"""

from __future__ import annotations

import os

import pytest

ctk = pytest.importorskip("customtkinter")


@pytest.fixture
def tk_root():
    """A withdrawn CTk root, or skip if no display is available."""
    try:
        root = ctk.CTk()
    except Exception as exc:                      # no display / headless
        pytest.skip("no Tk display available: %s" % exc)
    root.withdraw()
    try:
        yield root
    finally:
        try:
            root.destroy()
        except Exception:
            pass


def test_overlay_construct_show_set_hide_no_throw(tk_root):
    from voiceflow.ui.overlay import PreviewOverlay
    ov = PreviewOverlay(tk_root, max_chars=120)
    # None of these may raise (the UI half of fail-open).
    ov.show()
    ov.set_text("hello world forming live")
    ov.set_text("")                # empty -> placeholder, still no throw
    ov.hide()
    ov.destroy()


def test_overlay_truncates_to_last_max_chars(tk_root):
    from voiceflow.ui.overlay import PreviewOverlay
    ov = PreviewOverlay(tk_root, max_chars=20)
    long = "x" * 100 + "END"
    ov.set_text(long)
    shown = ov._text_lbl.cget("text")
    assert len(shown) <= 20
    assert shown.endswith("END")   # the MOST RECENT words stay visible
    ov.destroy()


def test_overlay_methods_are_noop_when_win_missing(tk_root):
    """If the underlying Toplevel failed to build, every public method is a
    silent no-op (fail-open)."""
    from voiceflow.ui.overlay import PreviewOverlay
    ov = PreviewOverlay(tk_root)
    ov._win = None                 # simulate a failed build
    ov._text_lbl = None
    # No exceptions, no effect.
    ov.show()
    ov.set_text("anything")
    ov.hide()
    ov.destroy()


def test_apply_noactivate_is_noop_off_windows(tk_root, monkeypatch):
    """On a non-Windows runner _apply_noactivate must do nothing (and never
    raise) -- so the macOS/Linux suites stay green."""
    from voiceflow.ui.overlay import PreviewOverlay
    ov = PreviewOverlay(tk_root)
    monkeypatch.setattr(os, "name", "posix")
    ov._apply_noactivate()         # must be a no-op, no raise
    ov.destroy()
