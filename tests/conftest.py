"""
Shared pytest fixtures + helpers for the VoiceFlow test suite.

These tests are deliberately OS-agnostic: they run on the Windows dev box AND on
the macOS/Linux GitHub Actions runners. Every OS-specific call (clipboard, paste
keystroke, global hotkeys/mouse hooks, the CUDA/faster-whisper runtime) is
mocked, so nothing here touches a real microphone, a real GPU, the real
clipboard, or installs a global hook.

Two seams make the engine fully testable without an OS or a model:

  * The engine pulls its clipboard + trigger backend from the platform factory
    (``voiceflow.platform.make_clipboard`` / ``make_trigger_backend``), accessed
    through the module object ``voiceflow.engine._platform``. The ``patched_engine``
    fixture replaces both with fakes so constructing a DictationEngine never
    installs a hook or touches the system clipboard.
  * ``faster_whisper.WhisperModel`` is imported into the engine module at import
    time (after the CUDA DLL registration). Tests patch ``voiceflow.engine.WhisperModel``
    and ``DictationEngine._warmup`` so no model is ever downloaded or run.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest import mock

import pytest

import voiceflow.engine as engine_mod
from voiceflow.constants import DEFAULT_CONFIG


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------
@pytest.fixture
def base_config():
    """A fresh, safe config dict (a copy of DEFAULT_CONFIG) with audio/beeps and
    long waits neutralised so tests are fast and silent."""
    cfg = dict(DEFAULT_CONFIG)
    cfg["beep"] = False
    cfg["min_record_seconds"] = 0.0
    cfg["max_record_seconds"] = 0
    cfg["clipboard_restore_delay_ms"] = 0
    cfg["clipboard_read_timeout_ms"] = 300
    return cfg


# ---------------------------------------------------------------------------
# Fake platform backends (so the engine never installs a hook or touches the OS)
# ---------------------------------------------------------------------------
class FakeClipboard:
    """Stand-in for the platform clipboard object the engine uses.

    Records the paste cycle: snapshot -> set_text -> paste -> restore is what the
    real ClipboardManager.paste_text() does internally, so for the engine's
    purposes we only need to record what text was delivered.
    """

    def __init__(self, deliver=True):
        self.deliver = deliver
        self.pasted = []          # every text handed to paste_text()
        self.saved = None
        self.restored = []
        self.set_texts = []

    def paste_text(self, text):
        self.pasted.append(text)
        return bool(self.deliver)

    # The richer ClipboardManager surface, so a test can drive the full cycle.
    def save(self):
        self.saved = {"__fake__": b"original"}
        return self.saved

    def _set_text_immediate(self, text):
        self.set_texts.append(text)

    def restore(self, snap):
        self.restored.append(snap)


class FakeTriggerHandle:
    kind = "keyboard"

    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


class FakeTriggers:
    """Stand-in for the platform TriggerBackend. register() succeeds by default
    and remembers the callback so a test can fire the trigger by hand."""

    def __init__(self, succeed=True):
        self.succeed = succeed
        self.registered = []      # [(trigger, callback)]
        self.last_handle = None

    def register(self, trigger, callback):
        self.registered.append((trigger, callback))
        if not self.succeed:
            return None
        self.last_handle = FakeTriggerHandle()
        return self.last_handle

    def classify(self, trigger):
        return {"trigger": trigger, "label": trigger, "clean": True,
                "warning": None}

    @property
    def presets(self):
        return []


@pytest.fixture
def fake_clip():
    return FakeClipboard()


@pytest.fixture
def fake_triggers():
    return FakeTriggers()


@pytest.fixture
def fakes():
    """Access to the helper CLASSES (not instances) so tests can build variants
    without importing from tests.conftest (which only works when cwd == repo
    root). Use e.g. ``fakes.FakeClipboard(deliver=False)``."""
    class _Fakes:
        FakeClipboard = FakeClipboard
        FakeTriggers = FakeTriggers
        FakeTriggerHandle = FakeTriggerHandle

    return _Fakes


@contextmanager
def _patched_platform(clip, triggers):
    """Patch the platform factory the engine uses so construction is inert."""
    with mock.patch.object(engine_mod._platform, "make_clipboard",
                           return_value=clip), \
         mock.patch.object(engine_mod._platform, "make_trigger_backend",
                           return_value=triggers):
        yield


@pytest.fixture
def make_engine(base_config, fake_clip, fake_triggers):
    """Factory that builds a DictationEngine with fake clipboard + triggers and a
    no-op warmup, never persisting config to disk. Returns (engine, clip,
    triggers). Pass overrides to tweak the config; pass callbacks via kwargs."""
    created = []

    def _factory(config_overrides=None, clip=None, triggers=None, **callbacks):
        cfg = dict(base_config)
        if config_overrides:
            cfg.update(config_overrides)
        c = clip if clip is not None else fake_clip
        t = triggers if triggers is not None else fake_triggers
        # Keep the tap/hold seams inert for the engine's WHOLE lifetime (not just
        # construction): trigger registration then falls through to the injected
        # fake `triggers` backend, and no real OS hotkey/mouse hook is installed
        # during the test run. Tests exercising tap/hold patch these explicitly.
        for name in ("make_tap_hold_keyboard", "make_tap_hold_chord"):
            p = mock.patch.object(engine_mod._platform, name, return_value=None)
            p.start()
            created.append(p)
        with _patched_platform(c, t):
            eng = engine_mod.DictationEngine(cfg, **callbacks)
        # Never write config.json during tests.
        eng_save = mock.patch("voiceflow.config.save_config", return_value=True)
        eng_save.start()
        created.append(eng_save)
        return eng, c, t

    yield _factory

    for p in created:
        try:
            p.stop()
        except Exception:
            pass
