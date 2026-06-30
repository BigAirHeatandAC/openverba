"""
Tests for the core dictation flow: snapshot -> set -> paste -> restore, plus the
engine's transcript handling.

Two layers, both with the clipboard + paste mocked (no real clipboard touched,
no keystroke synthesized):

  1. The Windows ``ClipboardManager.paste_text`` cycle (the verified
     implementation of the plan's section 2.6 ``emit_transcript`` flow). We mock
     its low-level OS calls (save/set/send_paste/restore/clear, time.sleep) and
     assert the ORDER: snapshot the old clipboard -> set the transcript -> paste
     -> settle -> restore the original (or clear when there was nothing to
     restore). This is the cross-platform contract every backend mirrors.

  2. The engine's ``_handle_recording`` which drives ``clip.paste_text`` after a
     transcription, with the hallucination filter + callbacks. We use a fake
     clipboard so no OS is involved; this part runs on any OS.
"""

from __future__ import annotations

from unittest import mock

import numpy as np
import pytest

import voiceflow.engine as engine_mod
import voiceflow.platform.windows as win


# ===========================================================================
# 1. ClipboardManager.paste_text -- the snapshot -> set -> paste -> restore cycle
# ===========================================================================
@pytest.fixture
def clip_manager():
    """A ClipboardManager with zero waits. The low-level OS calls are mocked per
    test; the manager itself is import-safe + constructible on any OS."""
    return win.ClipboardManager(restore_delay_ms=0, read_timeout_ms=300)


def _instrument_cycle(cm, save_returns):
    """Patch the four cycle steps + sleep on a ClipboardManager and return a list
    that records the call order. ``save_returns`` is what save() yields."""
    order = []
    patches = [
        mock.patch.object(cm, "save",
                          side_effect=lambda: (order.append("save")
                                               or save_returns)),
        mock.patch.object(cm, "_set_text_immediate",
                          side_effect=lambda t: order.append(("set", t))),
        mock.patch.object(win, "send_paste",
                          side_effect=lambda: order.append("paste")),
        mock.patch.object(cm, "restore",
                          side_effect=lambda s: order.append(("restore", s))),
        mock.patch.object(cm, "_clear",
                          side_effect=lambda: order.append("clear")),
        mock.patch.object(win.time, "sleep", lambda s: None),
    ]
    return order, patches


def test_paste_cycle_order_with_restore(clip_manager):
    saved = {win.CF_UNICODETEXT: b"o\x00r\x00i\x00g\x00\x00\x00"}
    order, patches = _instrument_cycle(clip_manager, saved)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
        result = clip_manager.paste_text("hello")

    assert result is True
    # snapshot BEFORE set; set BEFORE paste; restore LAST (the original goes back)
    assert order == ["save", ("set", "hello"), "paste", ("restore", saved)]


def test_paste_cycle_clears_when_nothing_to_restore(clip_manager):
    """When save() found nothing (empty dict), the transcript must be cleared
    afterwards so it doesn't linger in Clipboard History -- never restore({})."""
    order, patches = _instrument_cycle(clip_manager, {})
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
        result = clip_manager.paste_text("hi")

    assert result is True
    assert order == ["save", ("set", "hi"), "paste", "clear"]
    assert not any(isinstance(o, tuple) and o[0] == "restore" for o in order)


def test_paste_restores_even_if_paste_raises(clip_manager):
    """The original clipboard must be put back even if the paste keystroke throws
    (restore is in a finally)."""
    saved = {win.CF_UNICODETEXT: b"x\x00\x00\x00"}
    restored = []
    with mock.patch.object(clip_manager, "save", return_value=saved), \
         mock.patch.object(clip_manager, "_set_text_immediate"), \
         mock.patch.object(win, "send_paste",
                           side_effect=RuntimeError("paste blew up")), \
         mock.patch.object(clip_manager, "restore",
                           side_effect=lambda s: restored.append(s)), \
         mock.patch.object(win.time, "sleep", lambda s: None):
        with pytest.raises(RuntimeError):
            clip_manager.paste_text("boom")
    assert restored == [saved]


def test_paste_empty_text_is_noop(clip_manager):
    """Empty/blank text never touches the clipboard and returns False."""
    with mock.patch.object(clip_manager, "save") as save, \
         mock.patch.object(win, "send_paste") as send:
        assert clip_manager.paste_text("") is False
    save.assert_not_called()
    send.assert_not_called()


def test_set_text_immediate_encodes_utf16le_nul_terminated(clip_manager):
    """The transcript is set as concrete CF_UNICODETEXT bytes (UTF-16-LE + NUL),
    NOT delayed-rendering -- so it's on the clipboard the instant paste fires."""
    captured = {}
    with mock.patch.object(win, "_HAVE_WIN32CLIP", True), \
         mock.patch.object(clip_manager, "_open", return_value=True), \
         mock.patch.object(win, "_wcb", create=True) as wcb, \
         mock.patch.object(win.ClipboardManager, "_set_clip_bytes",
                           staticmethod(lambda fmt, data: captured.update(
                               fmt=fmt, data=data))):
        clip_manager._set_text_immediate("Hi")
    wcb.EmptyClipboard.assert_called_once()
    assert captured["fmt"] == win.CF_UNICODETEXT
    assert captured["data"] == "Hi".encode("utf-16-le") + b"\x00\x00"


# ===========================================================================
# 2. The engine's transcription -> paste path (any OS, fake clipboard)
# ===========================================================================
def test_handle_recording_pastes_and_reports(make_engine):
    transcripts = []
    eng, clip, _trig = make_engine(
        {"add_trailing_space": True},
        on_transcript=lambda t: transcripts.append(t),
    )
    with mock.patch.object(eng, "_transcribe_with_fallback",
                           return_value="hello world"):
        eng._handle_recording(np.zeros(eng.sr, dtype=np.float32))

    # paste_text gets the text WITH the trailing space we add for pasting...
    assert clip.pasted == ["hello world "]
    # ...but the reported transcript is trimmed.
    assert eng.last_transcript == "hello world"
    assert transcripts == ["hello world"]


def test_handle_recording_drops_hallucination(make_engine):
    transcripts = []
    eng, clip, _trig = make_engine(on_transcript=lambda t: transcripts.append(t))
    with mock.patch.object(eng, "_transcribe_with_fallback",
                           return_value="thanks for watching"):
        eng._handle_recording(np.zeros(eng.sr, dtype=np.float32))
    assert clip.pasted == []          # filtered -> nothing pasted
    assert transcripts == []          # no transcript callback for a drop


def test_handle_recording_empty_transcript_no_paste(make_engine):
    eng, clip, _trig = make_engine()
    with mock.patch.object(eng, "_transcribe_with_fallback", return_value="   "):
        eng._handle_recording(np.zeros(eng.sr, dtype=np.float32))
    assert clip.pasted == []


def test_handle_recording_discards_too_short(make_engine):
    """A recording shorter than min_record_seconds never reaches transcription."""
    eng, clip, _trig = make_engine({"min_record_seconds": 1.0})
    with mock.patch.object(eng, "_transcribe_with_fallback") as tr:
        eng._handle_recording(np.zeros(int(eng.sr * 0.1), dtype=np.float32))
    tr.assert_not_called()
    assert clip.pasted == []


def test_handle_recording_reports_undelivered(make_engine, fakes):
    """If paste_text reports it couldn't deliver, we still set last_transcript
    (the text WAS transcribed) but the delivered flag is False."""
    clip = fakes.FakeClipboard(deliver=False)
    eng, clip, _trig = make_engine(clip=clip)
    with mock.patch.object(eng, "_transcribe_with_fallback",
                           return_value="undelivered text"):
        eng._handle_recording(np.zeros(eng.sr, dtype=np.float32))
    assert clip.pasted == ["undelivered text "]
    assert eng.last_transcript == "undelivered text"


# ===========================================================================
# Voice snippets (text expansion) -- batch path, after corrections, before paste
# ===========================================================================
def test_handle_recording_expands_snippet(make_engine):
    """A loaded snippet expands the transcript before paste, and the pasted +
    recorded text reflect the expansion."""
    transcripts = []
    eng, clip, _trig = make_engine(
        {"add_trailing_space": True},
        on_transcript=lambda t: transcripts.append(t),
    )
    eng._snippets = [{"trigger": "brb", "expansion": "be right back",
                      "enabled": True}]
    with mock.patch.object(eng, "_transcribe_with_fallback",
                           return_value="ok brb"):
        eng._handle_recording(np.zeros(eng.sr, dtype=np.float32))
    assert clip.pasted == ["ok be right back "]      # expanded + trailing space
    assert eng.last_transcript == "ok be right back"  # history sees expansion
    assert transcripts == ["ok be right back"]


def test_handle_recording_no_snippets_is_passthrough(make_engine):
    """With no snippets loaded the transcript is pasted unchanged."""
    eng, clip, _trig = make_engine({"add_trailing_space": False})
    eng._snippets = []
    with mock.patch.object(eng, "_transcribe_with_fallback",
                           return_value="ok brb"):
        eng._handle_recording(np.zeros(eng.sr, dtype=np.float32))
    assert clip.pasted == ["ok brb"]


def test_reload_snippets_respects_disabled_flag(make_engine, tmp_path,
                                                monkeypatch):
    """reload_snippets() loads from disk when enabled, and clears to [] when the
    snippets_enabled flag is off."""
    import voiceflow.snippets as snip_mod
    path = str(tmp_path / "snippets.json")
    monkeypatch.setattr(snip_mod, "SNIPPETS_PATH", path)
    monkeypatch.setattr(snip_mod, "ensure_data_dir", lambda: str(tmp_path))
    snip_mod.save_snippets([{"trigger": "brb", "expansion": "be right back",
                             "enabled": True}])

    eng, _clip, _trig = make_engine({"snippets_enabled": True})
    eng.reload_snippets()
    assert eng._snippets == [{"trigger": "brb", "expansion": "be right back",
                              "enabled": True}]

    eng.cfg["snippets_enabled"] = False
    eng.reload_snippets()
    assert eng._snippets == []


# ===========================================================================
# Multi-language + translate-to-English: _transcribe passes the right
# language + task to the model (via voiceflow.transcribe.transcribe_kwargs).
# ===========================================================================
def _capture_transcribe_kwargs(eng, audio):
    """Replace eng.model with a recorder, call eng._transcribe(audio), and
    return the kwargs the model.transcribe() received."""
    captured = {}

    class _FakeModel:
        def transcribe(self, _audio, **kwargs):
            captured.update(kwargs)
            return iter([]), None  # (segments, info)

    eng.model = _FakeModel()
    eng._transcribe(audio)
    return captured


def test_transcribe_uses_specific_language_transcribe_task(make_engine):
    eng, _clip, _trig = make_engine(
        {"language": "es", "translate_to_english": False, "model": "small"})
    kw = _capture_transcribe_kwargs(eng, np.zeros(eng.sr, dtype=np.float32))
    assert kw["language"] == "es"
    assert kw["task"] == "transcribe"


def test_transcribe_translate_task_on_multilingual_model(make_engine):
    eng, _clip, _trig = make_engine(
        {"language": "es", "translate_to_english": True, "model": "large-v3"})
    kw = _capture_transcribe_kwargs(eng, np.zeros(eng.sr, dtype=np.float32))
    assert kw["language"] == "es"
    assert kw["task"] == "translate"


def test_transcribe_auto_detect_language_none(make_engine):
    eng, _clip, _trig = make_engine(
        {"language": None, "translate_to_english": False, "model": "small"})
    kw = _capture_transcribe_kwargs(eng, np.zeros(eng.sr, dtype=np.float32))
    assert kw["language"] is None
    assert kw["task"] == "transcribe"


def test_transcribe_translate_degrades_on_english_only_model(make_engine):
    """Fail-open: translate requested on an English-only model degrades to
    transcribe so the live paste path can never fail on an incompatible task."""
    eng, _clip, _trig = make_engine(
        {"language": "es", "translate_to_english": True, "model": "small.en"})
    kw = _capture_transcribe_kwargs(eng, np.zeros(eng.sr, dtype=np.float32))
    assert kw["task"] == "transcribe"


# ===========================================================================
# C1: model access is serialized by self._model_lock. The batch _transcribe and
# the streaming _stream_transcribe both hold the lock AROUND model.transcribe().
# ===========================================================================
class _LockSentinel:
    """Records enter/exit + whether it was held at inference time."""

    def __init__(self):
        self.enters = 0
        self.exits = 0
        self.held = False

    def __enter__(self):
        self.enters += 1
        self.held = True
        return self

    def __exit__(self, *exc):
        self.held = False
        self.exits += 1
        return False


def _install_lock_aware_model(eng):
    """Swap eng.model for a recorder that captures whether eng._model_lock was
    held when transcribe() was invoked. Returns the sentinel lock."""
    lock = _LockSentinel()
    eng._model_lock = lock

    class _Model:
        held_at_call = None

        def transcribe(self, _audio, **kwargs):
            _Model.held_at_call = lock.held
            return iter([]), None

    eng.model = _Model()
    return lock, _Model


def test_engine_batch_transcribe_holds_model_lock(make_engine):
    """The batch _transcribe path must hold _model_lock around the inference."""
    eng, _clip, _trig = make_engine({"model": "small"})
    lock, model_cls = _install_lock_aware_model(eng)
    eng._transcribe(np.zeros(eng.sr, dtype=np.float32))
    assert lock.enters >= 1
    assert model_cls.held_at_call is True       # held during model.transcribe
    assert lock.exits == lock.enters and lock.held is False


def test_engine_stream_transcribe_holds_model_lock(make_engine):
    """The streaming _stream_transcribe path must hold _model_lock too."""
    eng, _clip, _trig = make_engine({"model": "small"})
    lock, model_cls = _install_lock_aware_model(eng)
    eng._stream_transcribe(np.zeros(eng.sr, dtype=np.float32))
    assert lock.enters >= 1
    assert model_cls.held_at_call is True
    assert lock.exits == lock.enters and lock.held is False


def test_engine_model_lock_created_before_use(make_engine):
    """The lock exists on a freshly constructed engine (before any model load)."""
    eng, _clip, _trig = make_engine()
    assert eng._model_lock is not None
    # It is usable as a context manager.
    with eng._model_lock:
        pass


# ===========================================================================
# AI auto-cleanup integration in _handle_recording (cleanup mocked)
# ===========================================================================
def test_handle_recording_with_cleanup_enabled(make_engine):
    """When auto_cleanup is enabled and AI is available, cleanup runs between
    corrections and paste. The pasted + recorded text reflect the polish."""
    import voiceflow.ai as ai_mod

    transcripts = []
    eng, clip, _trig = make_engine(
        {"add_trailing_space": True, "auto_cleanup": True,
         "cleanup_level": "light"},
        on_transcript=lambda t: transcripts.append(t),
    )

    def fake_cleanup(text, level, cfg):
        return text.upper(), None  # Fake polish: uppercase.

    with mock.patch.object(eng, "_transcribe_with_fallback",
                           return_value="hello world"), \
         mock.patch.object(ai_mod, "is_available", return_value=True), \
         mock.patch.object(ai_mod, "cleanup_text", side_effect=fake_cleanup):
        eng._handle_recording(np.zeros(eng.sr, dtype=np.float32))

    assert clip.pasted == ["HELLO WORLD "]
    assert eng.last_transcript == "HELLO WORLD"
    assert transcripts == ["HELLO WORLD"]


def test_handle_recording_cleanup_fallback_on_error(make_engine):
    """When cleanup errors, fall back to raw text without blocking paste."""
    import voiceflow.ai as ai_mod

    eng, clip, _trig = make_engine(
        {"add_trailing_space": True, "auto_cleanup": True,
         "cleanup_level": "light"},
    )

    def fake_cleanup(text, level, cfg):
        return None, "Ollama not running"

    with mock.patch.object(eng, "_transcribe_with_fallback",
                           return_value="hello world"), \
         mock.patch.object(ai_mod, "is_available", return_value=True), \
         mock.patch.object(ai_mod, "cleanup_text", side_effect=fake_cleanup):
        eng._handle_recording(np.zeros(eng.sr, dtype=np.float32))

    # Paste gets the ORIGINAL (raw) text, not None.
    assert clip.pasted == ["hello world "]
    assert eng.last_transcript == "hello world"


def test_handle_recording_cleanup_skipped_when_disabled(make_engine):
    """When auto_cleanup is False, cleanup never runs."""
    import voiceflow.ai as ai_mod

    eng, clip, _trig = make_engine({"add_trailing_space": True,
                                    "auto_cleanup": False})
    cleanup_called = []

    def fake_cleanup(text, level, cfg):
        cleanup_called.append(True)
        return text.upper(), None

    with mock.patch.object(eng, "_transcribe_with_fallback",
                           return_value="hello world"), \
         mock.patch.object(ai_mod, "is_available", return_value=True), \
         mock.patch.object(ai_mod, "cleanup_text", side_effect=fake_cleanup):
        eng._handle_recording(np.zeros(eng.sr, dtype=np.float32))

    assert not cleanup_called
    assert clip.pasted == ["hello world "]


# ===========================================================================
# clean_transcript -- the sanitiser/hallucination filter feeding the paste
# ===========================================================================
_DEFAULTS = {
    "strip_whitespace": True, "allow_multiline": False,
    "filter_hallucinations": True, "add_trailing_space": True,
}


def _cfg(**over):
    c = dict(_DEFAULTS)
    c.update(over)
    return c


def test_clean_transcript_adds_trailing_space():
    text, filtered = engine_mod.clean_transcript("Hello there", _cfg())
    assert text == "Hello there "
    assert filtered is False


def test_clean_transcript_no_trailing_space_when_disabled():
    text, _ = engine_mod.clean_transcript("Hello", _cfg(add_trailing_space=False))
    assert text == "Hello"


def test_clean_transcript_collapses_newlines_when_single_line():
    """A stray newline acts as Enter in a chat/terminal -> collapse to a space."""
    text, _ = engine_mod.clean_transcript(
        "line one\nline two", _cfg(allow_multiline=False,
                                   add_trailing_space=False))
    assert text == "line one line two"


def test_clean_transcript_keeps_newlines_when_multiline_allowed():
    text, _ = engine_mod.clean_transcript(
        "line one\nline two", _cfg(allow_multiline=True,
                                   add_trailing_space=False))
    assert "\n" in text


@pytest.mark.parametrize("hallucination", [
    "Thank you.", "thanks for watching", "Please subscribe",
    "Subtitles by the Amara.org community", "Thanks for watching!",
])
def test_clean_transcript_filters_hallucinations(hallucination):
    text, filtered = engine_mod.clean_transcript(hallucination, _cfg())
    assert text == ""
    assert filtered is True


def test_clean_transcript_filter_can_be_disabled():
    text, filtered = engine_mod.clean_transcript(
        "Thank you.", _cfg(filter_hallucinations=False))
    assert text.strip() == "Thank you."
    assert filtered is False


def test_clean_transcript_strips_control_chars():
    text, filtered = engine_mod.clean_transcript(
        "a\x00b\x07c", _cfg(add_trailing_space=False))
    assert text == "abc"
    assert filtered is False


def test_clean_transcript_empty_input():
    assert engine_mod.clean_transcript("", _cfg()) == ("", False)
    assert engine_mod.clean_transcript("    ", _cfg()) == ("", False)


# ===========================================================================
# set_trigger -- persists to config + keeps the old trigger on failure
# ===========================================================================
def test_set_trigger_success_persists(make_engine):
    eng, _clip, triggers = make_engine({"trigger": "ctrl+shift+space"})
    with mock.patch("voiceflow.engine._config.save_config",
                    return_value=True) as save:
        ok = eng.set_trigger("f9")
    assert ok is True
    assert eng.trigger == "f9"
    assert eng.cfg["trigger"] == "f9"
    save.assert_called_once()
    # the fake triggers backend was asked to register f9
    assert ("f9", eng.on_trigger) in triggers.registered


def test_keyboard_trigger_uses_tap_hold_when_command_via_hold(make_engine):
    """With command_via_hold on, a KEYBOARD combo registers via the tap/hold
    seam (tap=dictate, hold=command), not the plain register backend."""
    captured = {}

    def fake_taphold(trigger, on_tap, on_hold_start, on_hold_end, hold_s):
        captured["args"] = (trigger, on_tap, on_hold_start, on_hold_end, hold_s)
        return win.TriggerHandle("taphold", mock.Mock())

    eng, _clip, triggers = make_engine(
        {"trigger": "ctrl+shift+space", "command_via_hold": True})
    with mock.patch.object(engine_mod._platform, "make_tap_hold_keyboard",
                           side_effect=fake_taphold) as mk:
        ok = eng.set_trigger("ctrl+shift+space")
    assert ok is True
    mk.assert_called_once()
    trig, on_tap, on_hold_start, on_hold_end, hold_s = captured["args"]
    assert trig == "ctrl+shift+space"
    assert on_tap == eng.on_trigger
    assert on_hold_start == eng._on_command_hold_start
    assert on_hold_end == eng._on_command_hold_end
    # plain register backend was NOT used for the keyboard combo
    assert ("ctrl+shift+space", eng.on_trigger) not in triggers.registered


def test_keyboard_tap_hold_falls_back_to_plain_register(make_engine):
    """If the keyboard tap/hold seam is unavailable (returns None), the engine
    falls back to plain single-fire keyboard registration."""
    eng, _clip, triggers = make_engine(
        {"trigger": "f9", "command_via_hold": True})
    with mock.patch.object(engine_mod._platform, "make_tap_hold_keyboard",
                           return_value=None):
        ok = eng.set_trigger("f9")
    assert ok is True
    assert ("f9", eng.on_trigger) in triggers.registered


def test_set_trigger_failure_keeps_old(make_engine, fakes):
    """If the new trigger fails to register, the engine re-registers the old one
    and does NOT change config."""

    class FlakyTriggers(fakes.FakeTriggers):
        def register(self, trigger, callback):
            self.registered.append((trigger, callback))
            # fail only for the NEW trigger; succeed for the original
            if trigger == "f13":
                return None
            return fakes.FakeTriggerHandle()

    triggers = FlakyTriggers()
    eng, _clip, _t = make_engine({"trigger": "ctrl+shift+space"},
                                 triggers=triggers)
    with mock.patch("voiceflow.engine._config.save_config") as save:
        ok = eng.set_trigger("f13")
    assert ok is False
    assert eng.trigger == "ctrl+shift+space"   # unchanged
    save.assert_not_called()


# ===========================================================================
# Live-preview mode: 3-way set_mode + the preview -> final batch flow
# ===========================================================================
def test_set_mode_preview(make_engine):
    eng, _clip, _trig = make_engine({"mode": "batch"})
    with mock.patch("voiceflow.engine._config.save_config", return_value=True):
        out = eng.set_mode("preview")
    assert out == "preview"
    assert eng.mode == "preview"
    assert eng.cfg["mode"] == "preview"


def test_set_mode_three_way_mapping(make_engine):
    eng, _clip, _trig = make_engine()
    with mock.patch("voiceflow.engine._config.save_config", return_value=True):
        assert eng.set_mode("Live preview") == "preview"
        assert eng.set_mode("prev") == "preview"
        assert eng.set_mode("streaming") == "streaming"
        assert eng.set_mode("stream-anything") == "streaming"
        assert eng.set_mode("batch") == "batch"
        assert eng.set_mode("whatever-else") == "batch"


class _FakePreviewSession:
    """A stand-in StreamingSession used by the preview-stop tests: it never runs a
    mic/model; full_audio() returns a known buffer (the WHOLE utterance)."""

    def __init__(self, audio):
        self._audio = audio
        self.keep_audio = True
        self.finalized = False

    def finalize_and_stop(self, *a, **k):
        self.finalized = True
        return "rough live words"   # discarded; the final decode re-runs on audio

    def full_audio(self):
        return self._audio


def _drain_one(eng):
    """Pop one (audio, kind) item from the engine's work_q without blocking."""
    return eng.work_q.get_nowait()


def test_preview_stop_queues_full_audio_as_dictation(make_engine):
    """_stop_preview hands the harvested full utterance to the batch worker path
    as ("audio", "dictation") -- it does NOT re-implement decode/paste."""
    eng, _clip, _trig = make_engine({"mode": "preview"})
    audio = np.zeros(eng.sr, dtype=np.float32)
    sess = _FakePreviewSession(audio)
    eng._stream_session = sess
    eng._preview_fellback = False

    hidden = {"n": 0}
    eng.on_preview_hide = lambda: hidden.__setitem__("n", hidden["n"] + 1)

    eng._stop_preview()

    assert sess.finalized is True
    assert eng._stream_session is None
    item = _drain_one(eng)
    queued_audio, kind = item
    assert kind == "dictation"
    assert queued_audio is audio                 # the WHOLE utterance
    assert hidden["n"] == 1                       # overlay hidden before paste


def test_preview_final_paste_comes_from_batch_path(make_engine):
    """Driving the queued item through _handle_recording yields the SAME accurate
    batch paste (final decode on the full audio), proving parity with batch."""
    transcripts = []
    eng, clip, _trig = make_engine(
        {"mode": "preview", "add_trailing_space": True},
        on_transcript=lambda t: transcripts.append(t))
    audio = np.zeros(eng.sr, dtype=np.float32)
    eng._stream_session = _FakePreviewSession(audio)

    eng._stop_preview()
    queued_audio, kind = _drain_one(eng)
    with mock.patch.object(eng, "_transcribe_with_fallback",
                           return_value="hello world"):
        eng._handle_recording(queued_audio)

    assert clip.pasted == ["hello world "]        # final paste from the batch path
    assert eng.last_transcript == "hello world"
    assert transcripts == ["hello world"]


def test_preview_applies_corrections_snippets_cleanup(make_engine):
    """The preview final path inherits corrections + snippets + AI cleanup from
    _handle_recording (parity with batch)."""
    import voiceflow.ai as ai_mod

    eng, clip, _trig = make_engine(
        {"mode": "preview", "add_trailing_space": True,
         "auto_cleanup": True, "cleanup_level": "light"})
    eng._snippets = [{"trigger": "brb", "expansion": "be right back",
                      "enabled": True}]
    audio = np.zeros(eng.sr, dtype=np.float32)
    eng._stream_session = _FakePreviewSession(audio)

    eng._stop_preview()
    queued_audio, _kind = _drain_one(eng)

    def fake_cleanup(text, level, cfg):
        return text.upper(), None      # fake polish: uppercase

    with mock.patch.object(eng, "_transcribe_with_fallback",
                           return_value="ok brb"), \
         mock.patch.object(ai_mod, "is_available", return_value=True), \
         mock.patch.object(ai_mod, "cleanup_text", side_effect=fake_cleanup):
        eng._handle_recording(queued_audio)

    # snippet expansion ("brb" -> "be right back") + cleanup (uppercase) applied.
    assert clip.pasted == ["OK BE RIGHT BACK "]


def test_preview_start_failure_falls_back_to_batch(make_engine):
    """If the StreamingSession can't start, _start_preview hides the bar, sets the
    fallback flag, and starts a plain batch recording -> the user still gets text."""
    import voiceflow.engine as eng_mod

    eng, _clip, _trig = make_engine({"mode": "preview"})
    eng.model = object()             # non-None so _start_preview proceeds
    hidden = {"n": 0}
    eng.on_preview_hide = lambda: hidden.__setitem__("n", hidden["n"] + 1)

    with mock.patch.object(eng_mod._streaming, "StreamingSession",
                           side_effect=RuntimeError("mic busy")), \
         mock.patch.object(eng, "_start_recording", return_value=True) as rec:
        ok = eng._start_preview()

    assert ok is True
    rec.assert_called_once()         # fell back to plain batch capture
    assert eng._preview_fellback is True
    assert eng._stream_session is None
    assert hidden["n"] == 1          # the bar was hidden on fallback


def test_preview_stop_after_fallback_uses_batch_teardown(make_engine):
    """After a fallback (_preview_fellback / sess is None), _stop_preview harvests
    via the batch teardown and routes the audio through work_q as ("...","dictation")."""
    eng, _clip, _trig = make_engine({"mode": "preview"})
    eng._stream_session = None
    eng._preview_fellback = True
    audio = np.zeros(eng.sr, dtype=np.float32)

    with mock.patch.object(eng, "_teardown_stream_and_get_audio",
                           return_value=audio):
        eng._stop_preview()

    assert eng._preview_fellback is False
    queued_audio, kind = _drain_one(eng)
    assert kind == "dictation"
    assert queued_audio is audio


def test_preview_decode_holds_model_lock(make_engine):
    """Both the live preview decode (_stream_transcribe) and the final decode
    (_transcribe) hold _model_lock around model.transcribe."""
    eng, _clip, _trig = make_engine({"model": "small"})
    lock, model_cls = _install_lock_aware_model(eng)

    eng._stream_transcribe(np.zeros(eng.sr, dtype=np.float32))
    assert model_cls.held_at_call is True       # live preview decode is locked

    model_cls.held_at_call = None
    eng._transcribe(np.zeros(eng.sr, dtype=np.float32))
    assert model_cls.held_at_call is True       # final decode is locked
    assert lock.held is False
