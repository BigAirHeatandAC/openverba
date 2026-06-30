"""
voiceflow.snippets - user-defined text-expansion snippets (TIER 1.5).

A snippet is a short spoken trigger that expands to a larger template/phrase
right before paste -- say "brb" -> get "be right back". Snippets are applied
DETERMINISTICALLY in the batch path AFTER transcript cleanup and learned
corrections (TIER 1) and BEFORE paste, so what's pasted == what's recorded in
history. They are NEVER applied in streaming mode (words type live; mid-sentence
expansion would be unsafe).

Mirrors the defensive design of ``learn.py``: stdlib only, every public function
is best-effort and NEVER raises into the dictation/paste path. Matching is
whole-word, case-insensitive, longest-trigger-first, and guarded so it never
rewrites inside URLs / paths / emails / code-ish / all-digit tokens (the same
guard family used by learned corrections, imported from ``learn``).
"""

from __future__ import annotations

import re
import json
import logging

from .constants import SNIPPETS_PATH, ensure_data_dir
# Reuse the exact URL/path/email/code/all-digit guard + the enclosing-token
# helper that learned corrections use, so snippets and corrections behave
# identically inside protected spans.
from .learn import _token_is_guarded, _enclosing_token

log = logging.getLogger("voiceflow.snippets")


# ---------------------------------------------------------------------------
# JSON helpers (defensive: default on missing/corrupt; never raise).
# ---------------------------------------------------------------------------
def load_snippets():
    """Load snippets.json from DATA_DIR. Returns [] on missing/corrupt.

    Structure: [{"trigger": str, "expansion": str, "enabled": bool}, ...]
    """
    try:
        import os
        if not os.path.exists(SNIPPETS_PATH):
            return []
        with open(SNIPPETS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        log.debug("snippets: load failed", exc_info=True)
        return []


def save_snippets(snippets):
    """Persist snippets to snippets.json (atomic temp-file + rename). Returns
    True on success, False on any failure. Never raises."""
    try:
        ensure_data_dir()
        import os
        data = snippets if isinstance(snippets, list) else []
        tmp = SNIPPETS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, SNIPPETS_PATH)
        return True
    except Exception:
        log.debug("snippets: save failed", exc_info=True)
        return False


# ---------------------------------------------------------------------------
# Runtime apply (TIER 1.5: deterministic text expansion). Batch only.
# ---------------------------------------------------------------------------
def _apply_one(text, snip):
    """Expand a single snippet across ``text``. Whole-word, case-insensitive,
    guarded. Returns ``text`` unchanged on any error or bad snippet."""
    try:
        trigger = str(snip.get("trigger", "") or "").strip()
        expansion = snip.get("expansion", "")
        if not trigger or not expansion:
            return text
        expansion = str(expansion)
        # Whole-word anchored, mirroring learn._apply_one's boundaries so we
        # never expand inside a longer word (the "cat" in "catalog").
        pattern = r"(?<![\w'])" + re.escape(trigger) + r"(?![\w'])"
        rx = re.compile(pattern, re.IGNORECASE)

        def _repl(m):
            token = _enclosing_token(text, m.start(), m.end())
            if _token_is_guarded(token):
                return m.group(0)            # leave URLs / code / etc. untouched
            return expansion                 # expansion is verbatim

        return rx.sub(_repl, text)
    except Exception:
        log.debug("snippets._apply_one failed for %r", snip, exc_info=True)
        return text


def apply_snippets(text, snippets):
    """Apply enabled snippets to ``text`` (longest-trigger-first so a longer
    trigger wins over a contained shorter one, e.g. "a.m." before "a").
    Deterministic, fuzzy OFF. Never raises -- returns the input on any failure."""
    try:
        if not text or not snippets:
            return text
        active = [s for s in snippets
                  if isinstance(s, dict) and s.get("enabled", True)
                  and s.get("trigger") and s.get("expansion")]
        active.sort(key=lambda s: len(str(s.get("trigger", ""))), reverse=True)
        for s in active:
            text = _apply_one(text, s)
        return text
    except Exception:
        log.debug("snippets.apply_snippets failed", exc_info=True)
        return text
