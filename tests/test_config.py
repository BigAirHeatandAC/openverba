"""
Tests for voiceflow.config coercion of the new translate_to_english key (and
that language=None / "auto" normalize to auto-detect). These call the pure
_coerce_config / load-time normalization seams; no real config.json is written.
"""

from voiceflow import config
from voiceflow.constants import DEFAULT_CONFIG


def test_translate_to_english_default_is_false():
    assert DEFAULT_CONFIG["translate_to_english"] is False


def test_coerce_translate_to_english_non_bool_resets():
    cfg = dict(DEFAULT_CONFIG)
    cfg["translate_to_english"] = "yes"   # not a bool
    corrections = config._coerce_config(cfg)
    assert cfg["translate_to_english"] is False
    assert any("translate_to_english" in c for c in corrections)


def test_coerce_translate_to_english_true_preserved():
    cfg = dict(DEFAULT_CONFIG)
    cfg["translate_to_english"] = True
    config._coerce_config(cfg)
    assert cfg["translate_to_english"] is True


def test_coerce_translate_to_english_false_preserved():
    cfg = dict(DEFAULT_CONFIG)
    cfg["translate_to_english"] = False
    corrections = config._coerce_config(cfg)
    assert cfg["translate_to_english"] is False
    assert not any("translate_to_english" in c for c in corrections)


# ===========================================================================
# Live-preview mode: mode coercion (3-way set) + preview_max_chars
# ===========================================================================
def test_mode_default_is_batch():
    assert DEFAULT_CONFIG["mode"] == "batch"


def test_mode_preview_preserved():
    cfg = dict(DEFAULT_CONFIG)
    cfg["mode"] = "preview"
    corrections = config._coerce_config(cfg)
    assert cfg["mode"] == "preview"
    assert not any(c.startswith("mode=") for c in corrections)


def test_mode_streaming_still_valid():
    cfg = dict(DEFAULT_CONFIG)
    cfg["mode"] = "streaming"
    corrections = config._coerce_config(cfg)
    assert cfg["mode"] == "streaming"
    assert not any(c.startswith("mode=") for c in corrections)


def test_mode_invalid_resets_to_batch():
    cfg = dict(DEFAULT_CONFIG)
    cfg["mode"] = "bogus"
    corrections = config._coerce_config(cfg)
    assert cfg["mode"] == "batch"
    assert any(c.startswith("mode=") for c in corrections)


def test_preview_max_chars_default():
    assert DEFAULT_CONFIG["preview_max_chars"] == 120


def test_preview_max_chars_coerce_non_int_resets():
    cfg = dict(DEFAULT_CONFIG)
    cfg["preview_max_chars"] = "lots"        # not an int
    corrections = config._coerce_config(cfg)
    assert cfg["preview_max_chars"] == DEFAULT_CONFIG["preview_max_chars"]
    assert any("preview_max_chars" in c for c in corrections)


def test_preview_max_chars_below_minimum_resets():
    cfg = dict(DEFAULT_CONFIG)
    cfg["preview_max_chars"] = 5             # below the minimum (20)
    corrections = config._coerce_config(cfg)
    assert cfg["preview_max_chars"] == DEFAULT_CONFIG["preview_max_chars"]
    assert any("preview_max_chars" in c for c in corrections)


def test_preview_max_chars_valid_preserved():
    cfg = dict(DEFAULT_CONFIG)
    cfg["preview_max_chars"] = 200
    corrections = config._coerce_config(cfg)
    assert cfg["preview_max_chars"] == 200
    assert not any("preview_max_chars" in c for c in corrections)


# ===========================================================================
# A.2: cleanup_model + cleanup_keep_alive config keys + coercion
# ===========================================================================
def test_cleanup_model_default():
    assert DEFAULT_CONFIG["cleanup_model"] == "qwen2.5:1.5b"


def test_cleanup_keep_alive_default():
    assert DEFAULT_CONFIG["cleanup_keep_alive"] == "10m"


def test_cleanup_model_valid_preserved():
    cfg = dict(DEFAULT_CONFIG)
    cfg["cleanup_model"] = "qwen2.5:3b"
    corrections = config._coerce_config(cfg)
    assert cfg["cleanup_model"] == "qwen2.5:3b"
    assert not any("cleanup_model" in c for c in corrections)


def test_cleanup_model_bad_resets():
    cfg = dict(DEFAULT_CONFIG)
    cfg["cleanup_model"] = 123          # not a string
    corrections = config._coerce_config(cfg)
    assert cfg["cleanup_model"] == DEFAULT_CONFIG["cleanup_model"]
    assert any("cleanup_model" in c for c in corrections)


def test_cleanup_model_empty_resets():
    cfg = dict(DEFAULT_CONFIG)
    cfg["cleanup_model"] = "   "        # blank
    corrections = config._coerce_config(cfg)
    assert cfg["cleanup_model"] == DEFAULT_CONFIG["cleanup_model"]
    assert any("cleanup_model" in c for c in corrections)


def test_cleanup_keep_alive_valid_preserved():
    cfg = dict(DEFAULT_CONFIG)
    cfg["cleanup_keep_alive"] = "1h"
    corrections = config._coerce_config(cfg)
    assert cfg["cleanup_keep_alive"] == "1h"
    assert not any("cleanup_keep_alive" in c for c in corrections)


def test_cleanup_keep_alive_bad_resets():
    cfg = dict(DEFAULT_CONFIG)
    cfg["cleanup_keep_alive"] = None    # not a string
    corrections = config._coerce_config(cfg)
    assert cfg["cleanup_keep_alive"] == DEFAULT_CONFIG["cleanup_keep_alive"]
    assert any("cleanup_keep_alive" in c for c in corrections)
