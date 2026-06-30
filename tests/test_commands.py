"""Tests for the voice-command parser (activation word, counts, synonyms)."""

from voiceflow import commands


def p(text, word="computer", require=True):
    return commands.parse_command(text, command_word=word,
                                  require_command_word=require)


def test_requires_activation_word_by_default():
    # Bare command phrases are dictation when the wake word is required.
    assert p("delete the selected text") is None
    assert p("backspace five") is None
    assert p("hello world") is None
    # With the wake word they execute.
    assert p("computer delete the selected text") == {"type": "keys",
                                                      "spec": "delete", "count": 1}
    assert p("computer backspace five") == {"type": "keys",
                                            "spec": "backspace", "count": 5}


def test_wake_word_alone_is_not_a_command():
    assert p("computer") is None
    assert p("computer.") is None


def test_counts_words_and_digits():
    assert p("computer backspace 3")["count"] == 3
    assert p("computer backspace three")["count"] == 3
    assert p("computer backspace twenty")["count"] == 20
    assert p("computer backspace twenty five")["count"] == 25
    assert p("computer delete last 4 characters") == {"type": "keys",
                                                      "spec": "backspace", "count": 4}


def test_common_commands():
    assert p("computer select all")["spec"] == "ctrl+a"
    assert p("computer copy")["spec"] == "ctrl+c"
    assert p("computer paste")["spec"] == "ctrl+v"
    assert p("computer undo")["spec"] == "ctrl+z"
    assert p("computer enter")["spec"] == "enter"
    assert p("computer press enter")["spec"] == "enter"
    assert p("computer run it")["spec"] == "enter"
    assert p("computer escape")["spec"] == "escape"
    assert p("computer delete the last word")["spec"] == "ctrl+backspace"


def test_navigation_with_count():
    assert p("computer go left 5") == {"type": "keys", "spec": "left", "count": 5}
    assert p("computer move up three") == {"type": "keys", "spec": "up", "count": 3}


def test_bare_commands_when_not_required():
    assert p("select all", require=False)["spec"] == "ctrl+a"
    assert p("backspace five", require=False)["count"] == 5
    # A non-command sentence is still dictation.
    assert p("the quick brown fox", require=False) is None


def test_custom_activation_word():
    assert p("jarvis enter", word="jarvis")["spec"] == "enter"
    assert p("computer enter", word="jarvis") is None  # wrong wake word -> dictation


def test_tolerant_natural_phrasing():
    # Real ASR phrasings that must still be understood (command-trigger mode).
    f = lambda s: p(s, require=False)  # noqa: E731
    assert f("Backspace, five spaces.") == {"type": "keys",
                                            "spec": "backspace", "count": 5}
    assert f("Hit the backspace button 8 times.")["count"] == 8
    assert f("delete last 4 characters") == {"type": "keys",
                                             "spec": "backspace", "count": 4}
    assert f("select everything")["spec"] == "ctrl+a"
    assert f("undo that")["spec"] == "ctrl+z"
    assert f("run it")["spec"] == "enter"
    assert f("go left three") == {"type": "keys", "spec": "left", "count": 3}
    assert f("control alt delete")["spec"] == "ctrl+alt+delete"
    # Still not a command -> dictation.
    assert f("the weather is nice today") is None


def test_execute_command_uses_typer():
    calls = []

    class FakeTyper:
        def type_text(self, s):
            calls.append(("type", s))

        def press_keys(self, spec, count=1):
            calls.append(("keys", spec, count))

        @property
        def supports_incremental(self):
            return True

    action = p("computer backspace five")
    assert commands.execute_command(action, FakeTyper()) is True
    assert calls == [("keys", "backspace", 5)]
    assert commands.execute_command(None, FakeTyper()) is False
