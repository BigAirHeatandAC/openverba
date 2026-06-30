"""Self-healing low-level keyboard hook (KeyboardHookLL): tap-vs-hold state
machine + that ONLY the combo's main key is suppressed (modifiers pass through).
Drives the pure `_on_key` state machine directly -- no real OS hook installed."""

import sys

import pytest

if sys.platform != "win32":
    pytest.skip("Windows-only keyboard hook", allow_module_level=True)

from voiceflow.platform import windows as w

CTRL, SHIFT, ALT, SPACE = 0xA2, 0xA0, 0xA4, 0x20   # left-variant modifier vks
SC_CTRL, SC_SHIFT, SC_SPACE = 29, 42, 57


def _hook(ev, combo="ctrl+shift+space"):
    h = w.KeyboardHookLL(
        combo,
        lambda: ev.__setitem__("tap", ev["tap"] + 1),
        lambda: ev.__setitem__("hs", ev["hs"] + 1),
        lambda: ev.__setitem__("he", ev["he"] + 1))
    h._dispatch_sync = True      # run callbacks inline for deterministic tests
    return h


def _ev():
    return {"tap": 0, "hs": 0, "he": 0}


def test_resolves_combo():
    h = _hook(_ev())
    assert h._resolved
    assert set(h._mod_groups) == {"ctrl", "shift"}
    assert 0x20 in h._main_vks


def test_modifier_only_combo_unresolved():
    h = _hook(_ev(), combo="ctrl+shift")
    assert h._resolved is False     # no main key -> caller falls back


def test_tap_fires_on_tap():
    ev = _ev()
    h = _hook(ev)
    h._on_key(CTRL, SC_CTRL, True)
    h._on_key(SHIFT, SC_SHIFT, True)
    assert h._on_key(SPACE, SC_SPACE, True) is True    # engage + suppress down
    assert h._on_key(SPACE, SC_SPACE, False) is True   # suppress up -> tap
    h._cancel_hold_timer()
    assert ev == {"tap": 1, "hs": 0, "he": 0}


def test_hold_fires_start_then_end():
    ev = _ev()
    h = _hook(ev)
    h._on_key(CTRL, SC_CTRL, True)
    h._on_key(SHIFT, SC_SHIFT, True)
    h._on_key(SPACE, SC_SPACE, True)
    h._on_hold_timeout()                               # held past threshold
    h._on_key(SPACE, SC_SPACE, False)                  # release -> hold end
    h._cancel_hold_timer()
    assert ev == {"tap": 0, "hs": 1, "he": 1}


def test_modifiers_never_suppressed():
    h = _hook(_ev())
    assert h._on_key(CTRL, SC_CTRL, True) is False
    assert h._on_key(SHIFT, SC_SHIFT, True) is False
    assert h._on_key(CTRL, SC_CTRL, False) is False


def test_main_without_modifiers_passes_through():
    ev = _ev()
    h = _hook(ev)
    assert h._on_key(SPACE, SC_SPACE, True) is False   # no mods held -> not ours
    assert h._on_key(SPACE, SC_SPACE, False) is False
    assert ev["tap"] == 0


def test_partial_modifiers_passes_through():
    h = _hook(_ev())
    h._on_key(CTRL, SC_CTRL, True)                      # only ctrl, no shift
    assert h._on_key(SPACE, SC_SPACE, True) is False


def test_releasing_modifier_ends_engaged_combo_and_eats_main_up():
    ev = _ev()
    h = _hook(ev)
    h._on_key(CTRL, SC_CTRL, True)
    h._on_key(SHIFT, SC_SHIFT, True)
    assert h._on_key(SPACE, SC_SPACE, True) is True     # engaged
    h._on_key(CTRL, SC_CTRL, False)                     # drop a modifier -> end
    h._cancel_hold_timer()
    assert ev["tap"] == 1
    # the still-suppressed main key's UP is swallowed -> no stray space leaks
    assert h._on_key(SPACE, SC_SPACE, False) is True
    assert ev["tap"] == 1                               # and does NOT re-fire


def test_autorepeat_suppressed_without_refire():
    ev = _ev()
    h = _hook(ev)
    h._on_key(CTRL, SC_CTRL, True)
    h._on_key(SHIFT, SC_SHIFT, True)
    h._on_key(SPACE, SC_SPACE, True)                    # engage
    assert h._on_key(SPACE, SC_SPACE, True) is True     # OS auto-repeat -> eat
    assert ev["tap"] == 0                               # no premature tap
    h._on_key(SPACE, SC_SPACE, False)
    h._cancel_hold_timer()
    assert ev["tap"] == 1


def test_scancode_matches_main_when_vk_differs():
    # If the vk doesn't match but the scancode does, it's still our main key.
    ev = _ev()
    h = _hook(ev)
    h._main_vks = set()                                 # force scancode path
    h._main_scans = {SC_SPACE}
    h._on_key(CTRL, SC_CTRL, True)
    h._on_key(SHIFT, SC_SHIFT, True)
    assert h._on_key(0x00, SC_SPACE, True) is True      # matched by scancode
    h._on_key(0x00, SC_SPACE, False)
    h._cancel_hold_timer()
    assert ev["tap"] == 1


def test_letter_trigger_resolves_via_ord():
    h = _hook(_ev(), combo="ctrl+alt+j")
    assert h._resolved
    assert ord("J") in h._main_vks
    assert set(h._mod_groups) == {"ctrl", "alt"}


class _FakeUser32:
    """Stand-in for user32.GetAsyncKeyState in reconcile tests."""
    def __init__(self, down=()):
        self._down = set(down)

    def GetAsyncKeyState(self, vk):
        return 0x8000 if vk in self._down else 0


def test_passthrough_then_modifiers_never_makes_stuck_key():
    # H3: Space alone is delivered to the app; pressing modifiers mid-repeat must
    # NOT engage (which would later suppress the UP whose DOWNs were delivered).
    ev = _ev()
    h = _hook(ev)
    assert h._on_key(SPACE, SC_SPACE, True) is False     # fresh space, no mods
    assert h._on_key(SPACE, SC_SPACE, True) is False     # auto-repeat, pass thru
    h._on_key(CTRL, SC_CTRL, True)
    h._on_key(SHIFT, SC_SHIFT, True)
    assert h._on_key(SPACE, SC_SPACE, True) is False     # repeat must NOT engage
    assert ev["tap"] == 0
    assert h._on_key(SPACE, SC_SPACE, False) is False    # UP delivered, not eaten
    assert ev["tap"] == 0


def test_reconcile_clears_wedged_state_when_nothing_down():
    # H2: a lost main-UP leaves _main_suppressed/_main_held stuck; reconcile must
    # clear it once nothing combo-relevant is physically down.
    h = _hook(_ev())
    h._main_held = True
    h._main_suppressed = True
    h._active = True
    h._down_vks = {CTRL, SHIFT}
    h._reconcile(_FakeUser32(down=()))               # nothing physically down
    assert h._active is False
    assert h._main_suppressed is False
    assert h._main_held is False
    assert h._down_vks == set()


def test_reconcile_leaves_state_when_a_key_is_physically_down():
    h = _hook(_ev())
    h._main_suppressed = True
    h._active = True
    h._down_vks = {CTRL}
    h._reconcile(_FakeUser32(down={CTRL}))           # ctrl still held
    assert h._active is True                          # untouched mid-combo
    assert h._main_suppressed is True


def test_lost_up_then_reconcile_lets_trigger_work_again():
    # End-to-end of the H2 wedge + recovery: engage, lose the UP, reconcile, then
    # a fresh tap must fire again (not be silently eaten forever).
    ev = _ev()
    h = _hook(ev)
    h._on_key(CTRL, SC_CTRL, True)
    h._on_key(SHIFT, SC_SHIFT, True)
    assert h._on_key(SPACE, SC_SPACE, True) is True   # engaged, DOWN suppressed
    # ... main-UP is lost (UAC/secure desktop). State is wedged.
    h._reconcile(_FakeUser32(down=()))                # nothing held -> recover
    assert ev["tap"] == 0                             # recovery is not a tap
    # Fresh combo now works:
    h._on_key(CTRL, SC_CTRL, True)
    h._on_key(SHIFT, SC_SHIFT, True)
    assert h._on_key(SPACE, SC_SPACE, True) is True
    h._on_key(SPACE, SC_SPACE, False)
    assert ev["tap"] == 1
