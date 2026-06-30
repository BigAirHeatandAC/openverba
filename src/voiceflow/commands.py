"""
voiceflow.commands - tiny spoken-command parser for hands-free editing.

In BATCH mode, each finalized utterance is checked here first: if the whole
utterance is a recognized command (e.g. "delete the selected text",
"backspace five", "select all", "press enter"), we execute the corresponding
keystroke(s) via the platform Typer instead of typing the words. Otherwise the
utterance is treated as normal dictation.

Design goals: small, predictable, and safe. A phrase is a command ONLY if the
ENTIRE utterance matches (so dictating the sentence "press enter to continue"
still types it). Numbers may be spoken ("backspace five") or digits
("backspace 5"). An optional leading wake word ("computer ..."/"command ...")
is accepted but not required.
"""

from __future__ import annotations

import re

# Spoken number words -> int (enough for everyday counts).
_ONES = {
    "zero": 0, "a": 1, "an": 1, "one": 1, "two": 2, "to": 2, "too": 2,
    "three": 3, "four": 4, "for": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
         "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}


def _to_int(token):
    """Parse '5' or 'five' or 'twenty five'/'twenty-five' -> int, else None."""
    if token is None:
        return None
    token = token.strip().lower().replace("-", " ")
    if not token:
        return None
    if token.isdigit():
        return int(token)
    parts = token.split()
    if len(parts) == 1:
        if parts[0] in _ONES:
            return _ONES[parts[0]]
        if parts[0] in _TENS:
            return _TENS[parts[0]]
        return None
    if len(parts) == 2 and parts[0] in _TENS and parts[1] in _ONES:
        return _TENS[parts[0]] + _ONES[parts[1]]
    return None


def _norm(text):
    t = (text or "").lower()
    t = re.sub(r"[^a-z0-9\s]", " ", t)      # drop ALL punctuation (commas, periods)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def _keys(spec, count=1):
    return {"type": "keys", "spec": spec, "count": int(count)}


# Exact whole-utterance phrases -> action. Many synonyms because ASR varies.
_EXACT = {
    # selection / clipboard / history
    "select all": _keys("ctrl+a"), "select everything": _keys("ctrl+a"),
    "copy": _keys("ctrl+c"), "copy that": _keys("ctrl+c"),
    "paste": _keys("ctrl+v"), "paste that": _keys("ctrl+v"),
    "cut": _keys("ctrl+x"), "cut that": _keys("ctrl+x"),
    "undo": _keys("ctrl+z"), "undo that": _keys("ctrl+z"),
    "redo": _keys("ctrl+y"),
    # delete the current selection (Delete removes selected text)
    "delete": _keys("delete"), "delete that": _keys("delete"),
    "delete selection": _keys("delete"),
    "delete the selection": _keys("delete"),
    "delete selected text": _keys("delete"),
    "delete the selected text": _keys("delete"),
    "remove selection": _keys("delete"),
    "remove the selection": _keys("delete"),
    "remove the selected text": _keys("delete"),
    "clear selection": _keys("delete"),
    "forward delete": _keys("delete"),
    # backspace
    "backspace": _keys("backspace"), "back space": _keys("backspace"),
    "delete that character": _keys("backspace"),
    "delete last character": _keys("backspace"),
    "delete the last character": _keys("backspace"),
    "delete a character": _keys("backspace"),
    "delete word": _keys("ctrl+backspace"),
    "delete the last word": _keys("ctrl+backspace"),
    "delete last word": _keys("ctrl+backspace"),
    # enter / run / whitespace / cancel (great for PowerShell)
    "enter": _keys("enter"), "press enter": _keys("enter"),
    "hit enter": _keys("enter"), "new line": _keys("enter"),
    "newline": _keys("enter"), "return": _keys("enter"),
    "run it": _keys("enter"), "run that": _keys("enter"),
    "run the command": _keys("enter"), "execute": _keys("enter"),
    "submit": _keys("enter"),
    "tab": _keys("tab"), "press tab": _keys("tab"),
    "space": _keys("space"), "press space": _keys("space"),
    "escape": _keys("escape"), "press escape": _keys("escape"),
    "cancel": _keys("escape"), "cancel that": _keys("escape"),
    "stop the command": _keys("ctrl+c"),  # terminal interrupt
    # navigation
    "go up": _keys("up"), "go down": _keys("down"),
    "go left": _keys("left"), "go right": _keys("right"),
    "move up": _keys("up"), "move down": _keys("down"),
    "move left": _keys("left"), "move right": _keys("right"),
    "previous command": _keys("up"), "next command": _keys("down"),
    "page up": _keys("page_up"), "page down": _keys("page_down"),
    "go to the beginning": _keys("home"), "go to start": _keys("home"),
    "beginning of line": _keys("home"),
    "go to the end": _keys("end"), "go to end": _keys("end"),
    "end of line": _keys("end"),
    "select to end": _keys("shift+end"),
    "select to start": _keys("shift+home"),
    "select line": _keys("home"),  # then shift+end below via combos? keep simple
}

def _extract_number(t):
    """Find a count anywhere in the words: '5', 'five', 'twenty five'. Ignores
    the bare article 'a'/'an' so 'delete a word' isn't read as a count."""
    toks = t.split()
    for i, tok in enumerate(toks):
        if tok in _TENS and i + 1 < len(toks) and toks[i + 1] in _ONES:
            return _TENS[tok] + _ONES[toks[i + 1]]
        if tok.isdigit():
            return int(tok)
        if tok in _ONES and tok not in ("a", "an"):
            return _ONES[tok]
        if tok in _TENS:
            return _TENS[tok]
    return None


def parse_command(text, command_word="computer", require_command_word=True):
    """Return an action dict if the utterance is a command, else None.

    With an activation word (e.g. "computer"), only utterances that START with it
    are treated as commands. In command-TRIGGER mode the caller passes
    require_command_word=False (the trigger already means 'command'), so parsing
    is tolerant -- intent is matched by KEYWORDS rather than exact phrasing, since
    speech varies ("hit the backspace button 8 times", "backspace five spaces").
    """
    t = _norm(text)
    if not t:
        return None
    cw = (command_word or "").lower().strip()
    had_word = False
    if cw:
        m = re.match(r"^" + re.escape(cw) + r"\b\s*", t)
        if m:
            t = t[m.end():].strip()
            had_word = True
    if require_command_word and not had_word:
        return None
    if not t:
        return None
    # Drop leading filler verbs/articles ("hit the", "press", "please", "just").
    for _ in range(3):
        t = re.sub(r"^(?:hit|press|key|do|the|a|an|please|just)\s+", "", t).strip()
    if not t:
        return None
    return _interpret(t)


def _interpret(t):
    if t in _EXACT:
        return _EXACT[t]

    combo = _parse_modifier_combo(t)        # "control a", "ctrl shift t", "alt f4"
    if combo is not None:
        return combo

    fm = re.match(r"^f([0-9]{1,2})$", t)     # bare function key "f5"
    if fm and 1 <= int(fm.group(1)) <= 24:
        return _keys("f" + fm.group(1))

    w = set(t.split())
    n = _extract_number(t)

    def has(*words):
        return any(x in w for x in words)

    # selection / clipboard / history
    if "select" in w and ("all" in w or "everything" in w):
        return _keys("ctrl+a")
    if has("copy"):
        return _keys("ctrl+c")
    if has("paste"):
        return _keys("ctrl+v")
    if has("undo"):
        return _keys("ctrl+z")
    if has("redo"):
        return _keys("ctrl+y")
    if has("cut"):
        return _keys("ctrl+x")

    # delete a word / N words
    if ("delete" in w or "remove" in w) and ("word" in w or "words" in w):
        return _keys("ctrl+backspace", n or 1)
    # delete the current selection
    if ("delete" in w or "remove" in w or "clear" in w) and \
            ("selection" in w or "selected" in w or "highlighted" in w):
        return _keys("delete")
    # backspace [N]  (and "delete N characters/letters/spaces")
    if "backspace" in w or ("back" in w and "space" in w):
        return _keys("backspace", n or 1)
    if "delete" in w and n and has("character", "characters", "char", "chars",
                                   "letter", "letters", "space", "spaces"):
        return _keys("backspace", n)
    if has("delete", "forward"):
        return _keys("delete")

    # whitespace / submit / cancel
    if has("enter", "return", "newline", "submit", "execute", "run") or \
            ("new" in w and "line" in w):
        return _keys("enter")
    if has("tab"):
        return _keys("tab")
    if has("escape", "cancel"):
        return _keys("escape")
    if has("space", "spacebar"):
        return _keys("space")

    # navigation
    for a in ("left", "right", "up", "down"):
        if a in w and (has("go", "move", "arrow") or n):
            return _keys(a, n or 1)
    if has("home") or "beginning" in w:
        return _keys("home")
    if has("end"):
        return _keys("end")

    return None


_MODS = {"control": "ctrl", "ctrl": "ctrl", "shift": "shift", "alt": "alt",
         "option": "alt", "command": "win", "windows": "win", "win": "win",
         "super": "win"}
_KEY_TOKENS = {
    "enter": "enter", "return": "enter", "delete": "delete",
    "backspace": "backspace", "space": "space", "tab": "tab",
    "escape": "escape", "esc": "escape", "home": "home", "end": "end",
    "up": "up", "down": "down", "left": "left", "right": "right",
    "insert": "insert",
}


def _key_token(tok):
    """Map a spoken key word to a key name usable by Typer.press_keys."""
    tok = tok.lower()
    if tok in _KEY_TOKENS:
        return _KEY_TOKENS[tok]
    if len(tok) == 1 and tok.isalnum():
        return tok
    m = re.match(r"^f([0-9]{1,2})$", tok)   # function keys f1..f24
    if m and 1 <= int(m.group(1)) <= 24:
        return "f" + m.group(1)
    return None


def _parse_modifier_combo(text):
    """"control a" / "ctrl shift t" / "alt f4" -> _keys("ctrl+a") etc., else None.
    Requires at least one modifier so plain dictation isn't captured."""
    toks = text.split()
    if len(toks) < 2 or toks[0] not in _MODS:
        return None
    mods = []
    i = 0
    while i < len(toks) and toks[i] in _MODS:
        m = _MODS[toks[i]]
        if m not in mods:
            mods.append(m)
        i += 1
    rest = toks[i:]
    if len(rest) != 1:
        return None
    key = _key_token(rest[0])
    if not key:
        return None
    return _keys("+".join(mods + [key]))


def describe(action):
    """Human-readable summary of an action (for logs/UI)."""
    if not action or action.get("type") != "keys":
        return "?"
    spec, count = action["spec"], action.get("count", 1)
    return spec if count == 1 else "%s x%d" % (spec, count)


def execute_command(action, typer):
    """Run an action's keystrokes via a platform Typer. Returns True if done."""
    if not action or typer is None:
        return False
    if action.get("type") == "keys":
        typer.press_keys(action["spec"], action.get("count", 1))
        return True
    return False
