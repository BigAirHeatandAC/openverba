"""
Tests for voiceflow.transcribe: the pure transcribe_kwargs helper + English-only
model detection + task validation. No engine/model imports -> runs on any OS.
"""

from voiceflow import transcribe


# ---------------------------------------------------------------- transcribe_kwargs
def test_transcribe_kwargs_auto_detect():
    cfg = {"language": None, "translate_to_english": False}
    assert transcribe.transcribe_kwargs(cfg) == {
        "language": None, "task": "transcribe"}


def test_transcribe_kwargs_auto_string():
    cfg = {"language": "auto", "translate_to_english": False}
    assert transcribe.transcribe_kwargs(cfg)["language"] is None


def test_transcribe_kwargs_empty_string():
    cfg = {"language": "", "translate_to_english": False}
    assert transcribe.transcribe_kwargs(cfg)["language"] is None


def test_transcribe_kwargs_specific_language():
    cfg = {"language": "es", "translate_to_english": False}
    assert transcribe.transcribe_kwargs(cfg) == {
        "language": "es", "task": "transcribe"}


def test_transcribe_kwargs_strips_language():
    cfg = {"language": "  fr  ", "translate_to_english": False}
    assert transcribe.transcribe_kwargs(cfg)["language"] == "fr"


def test_transcribe_kwargs_translate_enabled():
    cfg = {"language": "es", "translate_to_english": True}
    assert transcribe.transcribe_kwargs(cfg) == {
        "language": "es", "task": "translate"}


def test_transcribe_kwargs_translate_with_auto_detect():
    cfg = {"language": None, "translate_to_english": True}
    assert transcribe.transcribe_kwargs(cfg) == {
        "language": None, "task": "translate"}


def test_transcribe_kwargs_missing_keys_default_safe():
    # No keys at all -> auto-detect + transcribe (back-compat with old configs).
    assert transcribe.transcribe_kwargs({}) == {
        "language": None, "task": "transcribe"}


def test_transcribe_kwargs_none_cfg():
    assert transcribe.transcribe_kwargs(None) == {
        "language": None, "task": "transcribe"}


def test_transcribe_kwargs_non_str_language():
    cfg = {"language": 123, "translate_to_english": False}
    assert transcribe.transcribe_kwargs(cfg)["language"] is None


# ---------------------------------------------------------- is_english_only_model
def test_is_english_only_tiny_en():
    assert transcribe.is_english_only_model("tiny.en") is True


def test_is_english_only_base_en():
    assert transcribe.is_english_only_model("base.en") is True


def test_is_english_only_small_en():
    assert transcribe.is_english_only_model("small.en") is True


def test_is_english_only_medium_en():
    assert transcribe.is_english_only_model("medium.en") is True


def test_is_english_only_distil_small_en():
    assert transcribe.is_english_only_model("distil-small.en") is True


def test_is_english_only_distil_large_v3():
    assert transcribe.is_english_only_model("distil-large-v3") is True


def test_is_english_only_large_v3():
    assert transcribe.is_english_only_model("large-v3") is False


def test_is_english_only_small():
    assert transcribe.is_english_only_model("small") is False


def test_is_english_only_distil_small():
    assert transcribe.is_english_only_model("distil-small") is False


def test_is_english_only_distil_medium():
    assert transcribe.is_english_only_model("distil-medium") is False


def test_is_english_only_none_and_empty():
    assert transcribe.is_english_only_model(None) is False
    assert transcribe.is_english_only_model("") is False


# ---------------------------------------------------------- validate_task_for_model
def test_validate_transcribe_always_ok():
    valid, msg = transcribe.validate_task_for_model("small.en", "transcribe")
    assert valid is True
    assert msg is None


def test_validate_translate_english_only_invalid():
    valid, msg = transcribe.validate_task_for_model("small.en", "translate")
    assert valid is False
    assert msg is not None
    assert "multilingual" in msg.lower()


def test_validate_translate_multilingual_ok():
    valid, msg = transcribe.validate_task_for_model("large-v3", "translate")
    assert valid is True
    assert msg is None


def test_validate_translate_distil_large_v3_invalid():
    valid, msg = transcribe.validate_task_for_model("distil-large-v3", "translate")
    assert valid is False
    assert msg is not None
