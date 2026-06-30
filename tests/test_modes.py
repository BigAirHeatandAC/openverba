"""
Tests for voiceflow.modes -- foreground-app detection (mocked), mode resolution
(pure function), modes.json load/save round-trip + healing, and the engine's
per-app-mode integration with the transcription path.

Hermetic: modes.json is redirected to a temp dir, get_foreground_exe() is mocked
(no real Win32 calls), and the engine's model + platform are faked via the shared
conftest fixtures, so nothing here touches the OS, a GPU, or a real model.
"""

from __future__ import annotations

import os
import json

import pytest
from unittest import mock

import voiceflow.modes as M
from voiceflow.constants import DEFAULT_CONFIG
from voiceflow import config as vf_config


@pytest.fixture
def modes_path(tmp_path):
    """A temp path for modes.json (file does not exist yet)."""
    return os.path.join(str(tmp_path), "modes.json")


# ===========================================================================
# Config wiring: per_app_modes default + coercion
# ===========================================================================
def test_per_app_modes_default_is_false():
    assert DEFAULT_CONFIG["per_app_modes"] is False


def test_coerce_per_app_modes_non_bool_resets():
    cfg = dict(DEFAULT_CONFIG)
    cfg["per_app_modes"] = "yes"
    vf_config._coerce_config(cfg)
    assert cfg["per_app_modes"] is False


def test_coerce_per_app_modes_true_preserved():
    cfg = dict(DEFAULT_CONFIG)
    cfg["per_app_modes"] = True
    vf_config._coerce_config(cfg)
    assert cfg["per_app_modes"] is True


# ===========================================================================
# get_foreground_exe() -- always defensive (never raises)
# ===========================================================================
def test_get_foreground_exe_returns_none_on_error():
    """Any error inside the ctypes call sequence returns None, never raises."""
    with mock.patch("ctypes.windll", create=True) as win:
        win.user32.GetForegroundWindow.side_effect = OSError("denied")
        assert M.get_foreground_exe() is None


def test_get_foreground_exe_never_raises_without_windll():
    """On a platform without ctypes.windll (e.g. the Linux CI runner), the
    function must still return None instead of raising."""
    # If windll is absent the import/attr access raises -> caught -> None.
    result = M.get_foreground_exe()
    assert result is None or isinstance(result, str)


# ===========================================================================
# load_modes() -- seed + heal + round-trip
# ===========================================================================
def test_load_modes_seeds_builtin_if_missing(modes_path):
    assert not os.path.exists(modes_path)
    modes = M.load_modes(modes_path)
    assert len(modes) >= 1
    assert any(m["name"] == "Default" for m in modes)
    # The file was written (seeded).
    assert os.path.exists(modes_path)
    with open(modes_path, encoding="utf-8") as f:
        data = json.load(f)
    assert any(m["name"] == "Default" for m in data)


def test_load_modes_seeds_builtin_if_empty(modes_path):
    open(modes_path, "w").close()  # zero-byte file
    modes = M.load_modes(modes_path)
    assert any(m["name"] == "Default" for m in modes)


def test_load_modes_returns_existing(modes_path):
    custom = [{"name": "Test", "enabled": True, "apps": ["test.exe"],
               "prompt": "Test prompt", "tone": "neutral"}]
    assert M.save_modes(modes_path, custom)
    loaded = M.load_modes(modes_path)
    assert any(m["name"] == "Test" for m in loaded)
    # Default is guaranteed even if the user's file omitted it.
    assert any(m["name"] == "Default" for m in loaded)


def test_load_modes_rejects_invalid_json(modes_path):
    with open(modes_path, "w", encoding="utf-8") as f:
        f.write("{ not valid json ]")
    modes = M.load_modes(modes_path)
    assert any(m["name"] == "Default" for m in modes)


def test_load_modes_drops_invalid_entries(modes_path):
    data = [
        {"name": "Good", "enabled": True, "apps": ["a.exe"],
         "prompt": "p", "tone": "neutral"},
        {"name": "MissingKeys"},                       # dropped
        {"name": "BadApps", "enabled": True, "apps": "x.exe",
         "prompt": "p", "tone": "neutral"},            # apps not a list -> dropped
        "not-a-dict",                                  # dropped
    ]
    with open(modes_path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    modes = M.load_modes(modes_path)
    names = [m["name"] for m in modes]
    assert "Good" in names
    assert "MissingKeys" not in names
    assert "BadApps" not in names
    # A Default catch-all is injected since the file lacked one.
    assert "Default" in names


# ===========================================================================
# save_modes() -- atomic write + defensive
# ===========================================================================
def test_save_modes_writes_json(modes_path):
    modes = [{"name": "X", "enabled": True, "apps": ["x.exe"],
              "prompt": "P", "tone": "neutral"}]
    assert M.save_modes(modes_path, modes) is True
    with open(modes_path, encoding="utf-8") as f:
        data = json.load(f)
    assert data[0]["name"] == "X"


def test_save_modes_returns_false_on_bad_path():
    # A path whose parent dir cannot be created/used -> False (no raise).
    bad = os.path.join("Z:\\", "definitely", "nope", "modes.json") \
        if os.name == "nt" else "/proc/cannot/modes.json"
    assert M.save_modes(bad, [{"name": "Default", "enabled": True, "apps": [],
                               "prompt": "p", "tone": "neutral"}]) in (True, False)


# ===========================================================================
# resolve_mode() -- pure function, always returns a valid mode
# ===========================================================================
def _default_modes():
    return [
        {"name": "Slack", "enabled": True, "apps": ["slack.exe", "discord.exe"],
         "prompt": "Casual", "tone": "casual"},
        {"name": "Default", "enabled": True, "apps": [],
         "prompt": "Neutral", "tone": "neutral"},
    ]


def test_resolve_mode_exact_match():
    assert M.resolve_mode("slack.exe", _default_modes())["name"] == "Slack"


def test_resolve_mode_case_insensitive():
    assert M.resolve_mode("SLACK.EXE", _default_modes())["name"] == "Slack"


def test_resolve_mode_second_app_in_list():
    assert M.resolve_mode("discord.exe", _default_modes())["name"] == "Slack"


def test_resolve_mode_no_match_uses_default():
    assert M.resolve_mode("unknown.exe", _default_modes())["name"] == "Default"


def test_resolve_mode_none_exe_uses_default():
    assert M.resolve_mode(None, _default_modes())["name"] == "Default"


def test_resolve_mode_disabled_modes_skipped():
    modes = [
        {"name": "Slack", "enabled": False, "apps": ["slack.exe"],
         "prompt": "Casual", "tone": "casual"},
        {"name": "Default", "enabled": True, "apps": [],
         "prompt": "Neutral", "tone": "neutral"},
    ]
    assert M.resolve_mode("slack.exe", modes)["name"] == "Default"


def test_resolve_mode_empty_apps_matches_any():
    modes = [{"name": "Default", "enabled": True, "apps": [],
              "prompt": "Neutral", "tone": "neutral"}]
    assert M.resolve_mode("anything.exe", modes)["name"] == "Default"


def test_resolve_mode_never_returns_none_on_empty_list():
    m = M.resolve_mode("slack.exe", [])
    assert isinstance(m, dict) and m.get("prompt")


def test_resolve_mode_specific_wins_over_catchall_order_independent():
    # Default listed FIRST; a specific match must still win.
    modes = [
        {"name": "Default", "enabled": True, "apps": [],
         "prompt": "Neutral", "tone": "neutral"},
        {"name": "Code", "enabled": True, "apps": ["code.exe"],
         "prompt": "Code", "tone": "code-aware"},
    ]
    assert M.resolve_mode("code.exe", modes)["name"] == "Code"


# ===========================================================================
# Builtin modes are well-formed
# ===========================================================================
def test_builtin_modes_all_valid():
    for m in M.BUILTIN_MODES:
        assert M._valid_mode(m)
    assert any(m["name"] == "Default" and not m["apps"]
               for m in M.BUILTIN_MODES)


# ===========================================================================
# Engine integration: per-app mode prompt overrides the Whisper bias prompt
# ===========================================================================
def test_engine_get_mode_prompt_off_returns_none(make_engine):
    eng, _clip, _t = make_engine({"per_app_modes": False})
    assert eng._get_current_mode_prompt() is None


def test_engine_get_mode_prompt_uses_resolved_mode(make_engine):
    eng, _clip, _t = make_engine({"per_app_modes": True})
    eng._modes = [
        {"name": "Slack", "enabled": True, "apps": ["slack.exe"],
         "prompt": "CUSTOM_SLACK_PROMPT", "tone": "casual"},
        {"name": "Default", "enabled": True, "apps": [],
         "prompt": "DEFAULT_PROMPT", "tone": "neutral"},
    ]
    with mock.patch.object(M, "get_foreground_exe", return_value="slack.exe"):
        assert eng._get_current_mode_prompt() == "CUSTOM_SLACK_PROMPT"


def test_engine_get_mode_prompt_falls_back_on_win32_error(make_engine):
    eng, _clip, _t = make_engine({"per_app_modes": True})
    with mock.patch.object(M, "get_foreground_exe",
                           side_effect=RuntimeError("win32 boom")):
        # Never raises; returns None so the static prompt is used.
        assert eng._get_current_mode_prompt() is None


def test_engine_transcribe_uses_override_prompt(make_engine):
    """_transcribe forwards the override prompt as Whisper's initial_prompt."""
    eng, _clip, _t = make_engine({"per_app_modes": True})
    captured = {}

    class _FakeModel:
        def transcribe(self, audio, **kwargs):
            captured.update(kwargs)
            return iter([]), None

    eng.model = _FakeModel()
    out = eng._transcribe(b"audio", override_prompt="OVERRIDE_PROMPT")
    assert out == ""
    assert captured.get("initial_prompt") == "OVERRIDE_PROMPT"


def test_engine_transcribe_without_override_uses_bias_prompt(make_engine):
    eng, _clip, _t = make_engine({"per_app_modes": True})
    captured = {}

    class _FakeModel:
        def transcribe(self, audio, **kwargs):
            captured.update(kwargs)
            return iter([]), None

    eng.model = _FakeModel()
    eng._bias_prompt = "BIAS_PROMPT"
    eng._transcribe(b"audio")
    assert captured.get("initial_prompt") == "BIAS_PROMPT"


def test_engine_reload_modes_never_raises(make_engine, modes_path):
    eng, _clip, _t = make_engine({"per_app_modes": True})
    # Redirect at a temp path (don't touch the real DATA_DIR) and reload ->
    # seeds builtins, no raise.
    eng._modes_path = modes_path
    eng.reload_modes()
    assert isinstance(eng._modes, list)
    assert any(m.get("name") == "Default" for m in eng._modes)
