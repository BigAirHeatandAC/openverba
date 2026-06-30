"""
Tests for voiceflow.snippets -- loading, saving, and applying text expansion.

Hermetic: the snippets.json path is redirected into a tmp dir, so the real
snippets file is never touched. No model / GUI / OS. apply_snippets() touches no
disk, so most matching tests need no fixture.
"""

from __future__ import annotations

import os

import pytest

import voiceflow.snippets as S


@pytest.fixture
def snippets_path(tmp_path, monkeypatch):
    path = os.path.join(str(tmp_path), "snippets.json")
    monkeypatch.setattr(S, "SNIPPETS_PATH", path)
    monkeypatch.setattr(S, "ensure_data_dir", lambda: str(tmp_path))
    return path


# ===========================================================================
# load / save
# ===========================================================================
def test_load_missing_returns_empty(snippets_path):
    assert S.load_snippets() == []


def test_save_and_load_roundtrip(snippets_path):
    data = [
        {"trigger": "brb", "expansion": "be right back", "enabled": True},
        {"trigger": "fyi", "expansion": "for your information", "enabled": False},
    ]
    assert S.save_snippets(data) is True
    assert S.load_snippets() == data


def test_save_creates_file_atomically(snippets_path):
    S.save_snippets([{"trigger": "ty", "expansion": "thank you",
                      "enabled": True}])
    assert os.path.exists(snippets_path)
    # No stray temp file left behind.
    assert not os.path.exists(snippets_path + ".tmp")


def test_load_corrupt_file_returns_empty(snippets_path):
    with open(snippets_path, "w", encoding="utf-8") as f:
        f.write("{not valid json]")
    assert S.load_snippets() == []


def test_load_non_list_returns_empty(snippets_path):
    with open(snippets_path, "w", encoding="utf-8") as f:
        f.write('{"trigger": "x"}')   # a dict, not a list
    assert S.load_snippets() == []


def test_save_non_list_writes_empty_list(snippets_path):
    assert S.save_snippets("nope") is True
    assert S.load_snippets() == []


# ===========================================================================
# apply_snippets(): matching + expansion (no disk)
# ===========================================================================
def test_apply_simple_expansion():
    snippets = [{"trigger": "brb", "expansion": "be right back",
                 "enabled": True}]
    assert S.apply_snippets("see you later brb", snippets) == \
        "see you later be right back"


def test_apply_case_insensitive():
    snippets = [{"trigger": "brb", "expansion": "be right back",
                 "enabled": True}]
    assert S.apply_snippets("See you later BRB", snippets) == \
        "See you later be right back"


def test_apply_word_boundary_only():
    """Whole-word match only: don't expand 'cat' inside 'catalog'."""
    snippets = [{"trigger": "cat", "expansion": "feline", "enabled": True}]
    result = S.apply_snippets("the catalog and a cat are here", snippets)
    assert "catalog" in result            # untouched substring
    assert result.count("feline") == 1    # only the standalone word expanded
    assert "a feline are here" in result


def test_apply_longest_trigger_first():
    """A longer trigger wins over a contained shorter one."""
    snippets = [
        {"trigger": "imo", "expansion": "in my opinion", "enabled": True},
        {"trigger": "imho", "expansion": "in my humble opinion",
         "enabled": True},
    ]
    result = S.apply_snippets("imho it's great imo too", snippets)
    assert "in my humble opinion" in result
    assert "in my opinion too" in result


def test_apply_skip_disabled():
    snippets = [{"trigger": "brb", "expansion": "be right back",
                 "enabled": False}]
    assert S.apply_snippets("brb", snippets) == "brb"


def test_apply_skip_missing_expansion():
    snippets = [{"trigger": "brb", "enabled": True}]
    assert S.apply_snippets("brb", snippets) == "brb"


def test_apply_skip_inside_url():
    """A trigger appearing inside a URL/path is guarded; the standalone word is
    still expanded."""
    snippets = [{"trigger": "ftp", "expansion": "file transfer protocol",
                 "enabled": True}]
    result = S.apply_snippets("visit ftp://server.com or say ftp", snippets)
    assert "ftp://server.com" in result                 # URL untouched
    assert result.endswith("file transfer protocol")    # standalone expanded


def test_apply_skip_inside_code_token():
    """A trigger inside a code-ish token (underscores) is guarded."""
    snippets = [{"trigger": "my", "expansion": "MINE", "enabled": True}]
    result = S.apply_snippets("my_var = my thing", snippets)
    assert "my_var" in result                # code token untouched
    assert "= MINE thing" in result          # standalone word expanded


def test_apply_skip_inside_email():
    snippets = [{"trigger": "user", "expansion": "USERNAME", "enabled": True}]
    result = S.apply_snippets("email user@domain.com please", snippets)
    assert "user@domain.com" in result       # email untouched
    assert "USERNAME" not in result          # only token was the email


def test_apply_no_error_on_bad_snippet():
    """Mixed good + bad snippets: apply the good, skip the bad, never raise."""
    snippets = [
        {"trigger": "ok", "expansion": "okay", "enabled": True},
        {"expansion": "no trigger key"},          # missing "trigger"
        {"trigger": "bad", "expansion": None},    # None expansion
        "not even a dict",                        # garbage entry
    ]
    result = S.apply_snippets("ok that is bad", snippets)
    assert "okay" in result
    assert result.endswith("bad")             # "bad" left as-is (None expansion)


def test_apply_empty_text():
    snippets = [{"trigger": "x", "expansion": "y", "enabled": True}]
    assert S.apply_snippets("", snippets) == ""


def test_apply_empty_snippets():
    assert S.apply_snippets("hello world", []) == "hello world"


def test_apply_none_inputs_never_raise():
    assert S.apply_snippets(None, None) is None
    assert S.apply_snippets("hi", None) == "hi"


def test_apply_multiple_occurrences():
    snippets = [{"trigger": "lol", "expansion": "laughing out loud",
                 "enabled": True}]
    result = S.apply_snippets("lol that was funny lol", snippets)
    assert result == "laughing out loud that was funny laughing out loud"


def test_apply_enabled_defaults_true():
    """A snippet without an 'enabled' key is treated as enabled."""
    snippets = [{"trigger": "ty", "expansion": "thank you"}]
    assert S.apply_snippets("ty", snippets) == "thank you"
