"""
Tests for the platform-abstraction factory: ``voiceflow.platform``.

What is actually tested on any OS (the plan, section 7.1, calls out exactly this
as Windows-testable): the factory's *selection logic* via a mocked
``sys.platform`` + environment, and that ``make_backends`` / ``make_trigger_backend`` /
``make_clipboard`` / ``make_permissions`` dispatch to the chosen OS module.

We do NOT exercise the real OS backends' side effects (no hooks installed, no
clipboard touched). The macOS and Linux backend modules are written to be
import-safe on any OS (their pyobjc/evdev imports are lazy + guarded), so the
factory can import and instantiate them on the Windows dev box for selection
testing -- but their Triggers/classify/presets surface is pure, OS-agnostic
logic, which is all we assert against.
"""

from __future__ import annotations

import sys

import pytest

import voiceflow.platform as P


# ---------------------------------------------------------------------------
# detect_platform(): sys.platform + env selection matrix
# ---------------------------------------------------------------------------
def test_detect_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    # env shouldn't matter on Windows
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert P.detect_platform() == "windows"


def test_detect_macos(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    assert P.detect_platform() == "macos"


def test_detect_linux_x11_default(monkeypatch):
    """No Wayland markers -> X11 (the default Linux tier)."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
    assert P.detect_platform() == "linux-x11"


def test_detect_linux_x11_explicit_session(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    assert P.detect_platform() == "linux-x11"


def test_detect_linux_wayland_via_wayland_display(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
    assert P.detect_platform() == "linux-wayland"


def test_detect_linux_wayland_via_session_type(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    assert P.detect_platform() == "linux-wayland"


@pytest.mark.parametrize("plat", ["win32", "darwin", "linux"])
def test_detect_platform_value_is_known(monkeypatch, plat):
    monkeypatch.setattr(sys, "platform", plat)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
    assert P.detect_platform() in {"windows", "macos", "linux-wayland", "linux-x11"}


# ---------------------------------------------------------------------------
# _backend_module(): the factory routes detect_platform() -> the right module
# ---------------------------------------------------------------------------
_EXPECTED_MODULE = {
    "windows": "voiceflow.platform.windows",
    "macos": "voiceflow.platform.macos",
    "linux-wayland": "voiceflow.platform.linux_wayland",
    "linux-x11": "voiceflow.platform.linux_x11",
}


@pytest.mark.parametrize("detected,module_name", list(_EXPECTED_MODULE.items()))
def test_backend_module_selection(monkeypatch, detected, module_name):
    monkeypatch.setattr(P, "detect_platform", lambda: detected)
    mod = P._backend_module()
    assert mod.__name__ == module_name


# ---------------------------------------------------------------------------
# make_backends(): returns 5-tuple of concrete ABC instances for the chosen OS
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("detected", list(_EXPECTED_MODULE))
def test_make_backends_shape_and_types(monkeypatch, detected):
    monkeypatch.setattr(P, "detect_platform", lambda: detected)
    hotkeys, mouse, clip, paster, perms = P.make_backends()
    assert isinstance(hotkeys, P.HotkeyBackend)
    assert isinstance(mouse, P.MouseBackend)
    assert isinstance(clip, P.ClipboardBackend)
    assert isinstance(paster, P.Paster)
    assert isinstance(perms, P.Permissions)


def test_make_backends_uses_selected_module(monkeypatch):
    """The instances come from the module detect_platform() picked."""
    monkeypatch.setattr(P, "detect_platform", lambda: "windows")
    backends = P.make_backends()
    for obj in backends:
        assert type(obj).__module__ == "voiceflow.platform.windows"


# ---------------------------------------------------------------------------
# make_trigger_backend(): the single stable trigger API the engine uses
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("detected,module_name", list(_EXPECTED_MODULE.items()))
def test_make_trigger_backend_selection(monkeypatch, detected, module_name):
    monkeypatch.setattr(P, "detect_platform", lambda: detected)
    tb = P.make_trigger_backend()
    assert isinstance(tb, P.TriggerBackend)
    # make_trigger_backend() calls the selected module's Triggers() factory.
    # (Note: on Linux the *class* lives in the shared linux_common base, so we
    # assert the factory FUNCTION came from the session-specific module, which is
    # the real per-platform seam.)
    selected = P._backend_module()
    assert selected.__name__ == module_name
    assert isinstance(selected.Triggers(), type(tb))


@pytest.mark.parametrize("detected", list(_EXPECTED_MODULE))
def test_trigger_backend_classify_and_presets(monkeypatch, detected):
    """classify()/presets are OS-agnostic logic and identical across backends:
    a known clean combo classifies clean; the conflict-prone chord warns."""
    monkeypatch.setattr(P, "detect_platform", lambda: detected)
    tb = P.make_trigger_backend()

    clean = tb.classify("ctrl+shift+space")
    assert set(clean) == {"trigger", "label", "clean", "warning"}
    assert clean["clean"] is True
    assert clean["warning"] is None

    chord = tb.classify("mouse:left+right")
    assert chord["clean"] is False
    assert chord["warning"]  # conflict-prone -> a human-readable warning

    presets = tb.presets
    assert isinstance(presets, list)
    # Every backend ships the same picker presets.
    assert any(p.get("trigger") == "mouse:x1" for p in presets)


# ---------------------------------------------------------------------------
# make_clipboard() / make_permissions(): routed through the same selected module
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("detected,module_name", list(_EXPECTED_MODULE.items()))
def test_make_clipboard_selection(monkeypatch, detected, module_name):
    monkeypatch.setattr(P, "detect_platform", lambda: detected)
    clip = P.make_clipboard(restore_delay_ms=10, read_timeout_ms=400)
    # The engine relies on this object exposing paste_text(...).
    assert hasattr(clip, "paste_text")
    assert type(clip).__module__ == module_name


@pytest.mark.parametrize("detected,module_name", list(_EXPECTED_MODULE.items()))
def test_make_permissions_selection(monkeypatch, detected, module_name):
    monkeypatch.setattr(P, "detect_platform", lambda: detected)
    perms = P.make_permissions()
    assert isinstance(perms, P.Permissions)
    checked = perms.check()
    assert isinstance(checked, dict)
    assert type(perms).__module__ == module_name


def test_windows_permissions_all_ok(monkeypatch):
    """Windows needs no TCC grants -> check() is all-True and all_ok() True."""
    monkeypatch.setattr(P, "detect_platform", lambda: "windows")
    perms = P.make_permissions()
    assert perms.all_ok() is True
    assert all(perms.check().values())


# ---------------------------------------------------------------------------
# diagnostics(): returns a dict for whichever module is selected
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("detected", list(_EXPECTED_MODULE))
def test_diagnostics_returns_dict(monkeypatch, detected):
    monkeypatch.setattr(P, "detect_platform", lambda: detected)
    diag = P.diagnostics()
    assert isinstance(diag, dict)
