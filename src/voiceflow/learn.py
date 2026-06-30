"""
voiceflow.learn - turn a user's transcript EDIT into recognition improvements.

A user editing a transcript is a high-confidence, explicit correction signal. We
derive two artifacts from the (original -> edited) diff:

  * personal_vocab.json -- term -> {score, last}. Proper nouns / jargon the user
    keeps fixing. Fed to faster-whisper as ``hotwords`` + a short ``initial_prompt``
    addendum (TIER 0: SOFT biasing, never a guarantee). Capped by score/recency
    so we don't over-stuff and make Whisper hallucinate the terms.

  * corrections.json -- a list of deterministic substitution rules applied right
    before paste (TIER 1: the ONLY GUARANTEED fix). Word-boundary anchored,
    longest-phrase-first, case-aware, fuzzy OFF, and guarded so it NEVER rewrites
    inside URLs / paths / emails / code-ish / all-digit tokens.

Corrections are EXPLICIT-ONLY: a rule exists only because the user edited and
saved. That bounds overcorrection -- the blast radius of a bad rule is one exact
phrase the user themselves corrected.

stdlib only. Every public function is defensive: a learning bug must NEVER break
dictation or the paste path.
"""

from __future__ import annotations

import re
import json
import logging
import difflib

from .constants import CORRECTIONS_PATH, PERSONAL_VOCAB_PATH, ensure_data_dir

log = logging.getLogger("voiceflow.learn")

# Derivation bounds -- keep edits that look like vocabulary fixes, reject edits
# that look like sentence rewrites.
_MAX_FROM_CHARS = 60
_MAX_SPAN_TOKENS = 4

# How strongly an explicit edit bumps a vocab term's score.
_VOCAB_BUMP = 3.0

# A "term" = a contiguous run of Capitalized words (proper noun / brand), each
# word starting uppercase and length >= 2. e.g. "Big Air", "OpenVerba".
_CAP_WORD = r"[A-Z][\w'-]*"
_CAP_RUN_RE = re.compile(r"\b(%s(?:\s+%s)*)\b" % (_CAP_WORD, _CAP_WORD))


# ---------------------------------------------------------------------------
# JSON helpers (defensive: default on missing/corrupt; never raise).
# ---------------------------------------------------------------------------
def _load_json(path, default):
    try:
        import os
        if not os.path.exists(path):
            return default() if callable(default) else default
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data
    except Exception:
        log.debug("learn: load %s failed", path, exc_info=True)
        return default() if callable(default) else default


def _save_json(path, data):
    try:
        ensure_data_dir()
        import os
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except Exception:
        log.debug("learn: save %s failed", path, exc_info=True)
        return False


def load_corrections():
    """Return the list of correction rules (or [] on missing/corrupt)."""
    data = _load_json(CORRECTIONS_PATH, list)
    return data if isinstance(data, list) else []


def load_vocab():
    """Return the term->{score,last} vocab dict (or {} on missing/corrupt)."""
    data = _load_json(PERSONAL_VOCAB_PATH, dict)
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
# Diff -> rules + terms.
# ---------------------------------------------------------------------------
# Common words whose leading capital is grammar, not a name -- never vocabulary.
_STOPWORDS = {
    "a", "an", "the", "i", "it", "is", "to", "of", "and", "or", "in", "on",
    "at", "for", "this", "that", "my", "me", "we", "you", "he", "she", "they",
    "but", "so", "if", "as", "be", "do", "no", "yes", "ok",
}


def _terms_in(text):
    """Proper-noun / brand terms SAFE to learn as vocabulary. Includes: multi-word
    Capitalized runs ("Big Air"); single words with interior caps or all-caps
    ("OpenVerba", "iPhone", "HVAC"); and single Capitalized words that appear
    MID-sentence (likely real names). EXCLUDES sentence-start single Capitalized
    words and common stop-words -- where a leading capital is just grammar, not a
    name -- so an ordinary capitalization edit ("the" -> "The") never pollutes the
    hotword list."""
    if not text:
        return []
    out = []
    for m in _CAP_RUN_RE.finditer(text):
        term = m.group(1).strip()
        if len(term) < 2:
            continue
        multiword = " " in term
        interior = bool(re.search(r"(?<=.)[A-Z]", term))   # OpenVerba / HVAC
        if multiword or interior:
            out.append(term)
            continue
        if term.lower() in _STOPWORDS:
            continue
        # Single leading-cap-only word: keep ONLY if it's mid-sentence (the
        # previous non-space char isn't start-of-text or . ! ?), where a capital
        # signals a real name rather than sentence-start grammar.
        k = m.start() - 1
        while k >= 0 and text[k].isspace():
            k -= 1
        prev = text[k] if k >= 0 else ""
        if prev and prev not in ".!?":
            out.append(term)
    return out


def _denylist_phrase(frm):
    """Should we REFUSE to derive a rule whose 'from' is ``frm``? True for
    URL/path/email/code-ish/all-digit phrases -- the same guard family applied at
    apply-time, but here it stops a bad rule from ever being created."""
    if not frm:
        return True
    return any(_token_is_guarded(tok) for tok in frm.split())


def _infer_case_mode(frm, to):
    """How a derived rule should treat case:
      'force'    -> output ``to`` verbatim everywhere. ONLY for genuine
                    brands/proper nouns/acronyms: interior or non-leading caps
                    ("OpenVerba", "iPhone", "HVAC") or a multi-word Capitalized
                    run ("big air" -> "Big Air").
      'preserve' -> case-insensitive match, mirror the matched token's casing.
                    For lowercase typo fixes ("recieve" -> "Receive"/"receive").
      'skip'     -> do NOT derive a rule. A single word that merely gained a
                    LEADING capital is a sentence-start artifact, not a fix that
                    should fire everywhere ("the" -> "The", "a" -> "A"); forcing
                    it would corrupt every later transcript.
    """
    f, t = (frm or "").strip(), (to or "").strip()
    if not f or not t:
        return "skip"
    # Interior / non-leading uppercase -> brand or acronym (OpenVerba, HVAC).
    if re.search(r"(?<=.)[A-Z]", t):
        return "force"
    # Multi-word Capitalized run -> proper noun ("Big Air").
    if " " in t and _CAP_RUN_RE.fullmatch(t):
        return "force"
    # `t` is now a single word whose only uppercase (if any) is the leading char.
    if t.lower() == f.lower() and t != f:
        return "skip"          # pure recapitalization of one word -> contextual
    # A spelling fix that also got sentence-start-capitalized: fix the spelling,
    # but DON'T force the capital everywhere -> preserve (mirror context casing).
    return "preserve"


def derive(original, edited):
    """Diff two transcripts at the WORD level and return ``(rules, terms)``.

    Only bounded, confident single-block REPLACEMENTS become rules (inserts /
    deletes are editing, not recognition fixes, so they're ignored -- they still
    correctly bound the replace blocks via SequenceMatcher). ``terms`` are the
    proper-noun vocabulary to bias toward.

    Comparison is on CASED tokens: lowercasing the keys would make a pure
    recapitalization (big air -> Big Air) collapse to an 'equal' block and never
    produce a rule. Diffing cased tokens surfaces both recapitalizations and
    lowercase typos as 'replace' blocks; _infer_case_mode then distinguishes a
    proper-noun fix (force) from a typo fix (preserve)."""
    rules = []
    terms = []
    try:
        o = (original or "").split()
        e = (edited or "").split()
        if not e:
            return rules, terms
        sm = difflib.SequenceMatcher(a=o, b=e, autojunk=False)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag != "replace":
                continue
            frm = " ".join(o[i1:i2]).strip()
            to = " ".join(e[j1:j2]).strip()
            if not frm or not to:
                continue
            if (i2 - i1) > _MAX_SPAN_TOKENS or (j2 - j1) > _MAX_SPAN_TOKENS:
                continue  # long rewrite -> a sentence edit, not vocabulary
            if len(frm) > _MAX_FROM_CHARS:
                continue
            # Reject changes that are IDENTICAL once trailing punctuation is
            # stripped (cleanup already handles those) AND differ only by that
            # punctuation -- i.e. same letters AND same case, just punctuation.
            # (A pure recapitalization like big air -> Big Air is NOT skipped.)
            if frm != to and \
                    re.sub(r"[^\w]+$", "", frm) == re.sub(r"[^\w]+$", "", to):
                continue
            if _denylist_phrase(frm):
                continue
            # Never derive a rule from a 1-char 'from' (e.g. "a"->"A"): it would
            # rewrite that letter everywhere.
            if len(frm.strip()) <= 1:
                continue
            case = _infer_case_mode(frm, to)
            if case == "skip":
                # A bare sentence-start capitalization -> contextual, not a fix
                # that should fire on every future transcript.
                continue
            rules.append({
                "from": frm,
                "to": to,
                "case": case,
                "whole_word": True,
                "enabled": True,
                "hits": 1,
            })
            if case == "force":
                terms.extend(_terms_in(to))
        # Any NEW capitalized phrase in the edit (not already in the original)
        # is vocabulary too, even if it wasn't a clean 1:1 replace.
        orig_terms = {t.lower() for t in _terms_in(original or "")}
        for t in _terms_in(edited or ""):
            if t.lower() not in orig_terms:
                terms.append(t)
    except Exception:
        log.debug("learn.derive failed", exc_info=True)
        return [], []
    # De-dup terms (case-insensitive, keep first casing).
    seen = set()
    uniq_terms = []
    for t in terms:
        k = t.lower()
        if k not in seen:
            seen.add(k)
            uniq_terms.append(t)
    return rules, uniq_terms


# ---------------------------------------------------------------------------
# Persist (merge into corrections.json + personal_vocab.json).
# ---------------------------------------------------------------------------
def _merge_corrections(new_rules):
    if not new_rules:
        return
    existing = load_corrections()
    index = {}
    for r in existing:
        if isinstance(r, dict) and "from" in r and "to" in r:
            index[(str(r["from"]).lower(), str(r["to"]))] = r
    for nr in new_rules:
        key = (str(nr["from"]).lower(), str(nr["to"]))
        if key in index:
            cur = index[key]
            cur["hits"] = int(cur.get("hits", 1)) + 1
        else:
            existing.append(dict(nr))
            index[key] = nr
    _save_json(CORRECTIONS_PATH, existing)


def _bump_vocab(terms):
    if not terms:
        return
    import time as _time
    vocab = load_vocab()
    now = _time.time()
    for t in terms:
        if not t:
            continue
        cur = vocab.get(t)
        if isinstance(cur, dict):
            cur["score"] = float(cur.get("score", 0.0)) + _VOCAB_BUMP
            cur["last"] = now
        else:
            vocab[t] = {"score": _VOCAB_BUMP, "last": now}
    _save_json(PERSONAL_VOCAB_PATH, vocab)


def learn(original, edited):
    """Derive + persist corrections/vocab from an explicit edit. Returns
    ``{"corrections": [...], "terms": [...]}`` (the freshly-derived ones, for a
    user-facing toast). Never raises."""
    try:
        rules, terms = derive(original, edited)
        _merge_corrections(rules)
        _bump_vocab(terms)
        return {"corrections": rules, "terms": terms}
    except Exception:
        log.debug("learn.learn failed", exc_info=True)
        return {"corrections": [], "terms": []}


# ---------------------------------------------------------------------------
# Runtime apply (TIER 1: the guaranteed, deterministic fix). Batch only.
# ---------------------------------------------------------------------------
_GUARD_URLISH_RE = re.compile(r"://|@|\\|\.[A-Za-z]{2,}(?:/|$)")
_GUARD_CODEISH_RE = re.compile(r"[_<>(){}=]|[A-Za-z]\d|\d[A-Za-z]")
_ALL_DIGITS_RE = re.compile(r"^\d+$")


def _token_is_guarded(token):
    """Should a match inside this whitespace-delimited token be SKIPPED?
    True for URLs / paths / emails, all-digit tokens, and code-ish/mixed-symbol
    tokens (so we never rewrite inside http://big-air.com or a variable name)."""
    if not token:
        return False
    if _ALL_DIGITS_RE.match(token):
        return True
    if _GUARD_URLISH_RE.search(token):
        return True
    return bool(_GUARD_CODEISH_RE.search(token))


def _enclosing_token(text, start, end):
    """The whitespace-delimited token enclosing [start, end) in ``text``."""
    ls = start
    while ls > 0 and not text[ls - 1].isspace():
        ls -= 1
    le = end
    n = len(text)
    while le < n and not text[le].isspace():
        le += 1
    return text[ls:le]


def _mirror_case(matched, to):
    """For case='preserve': mirror the MATCHED token's casing onto ``to``.
    Handles the common cases: all-lower, ALL-UPPER, and Titlecase (sentence
    start). Anything else falls back to ``to`` verbatim."""
    if not matched:
        return to
    if matched.isupper() and len(matched) > 1:
        return to.upper()
    if matched[:1].isupper() and matched[1:].islower():
        return to[:1].upper() + to[1:]
    if matched.islower():
        return to.lower()
    return to


def _apply_one(text, rule):
    try:
        frm = str(rule.get("from", ""))
        to = str(rule.get("to", ""))
        if not frm:
            return text
        case = rule.get("case", "force")
        whole = rule.get("whole_word", True)
        if whole:
            pattern = r"(?<![\w'])" + re.escape(frm) + r"(?![\w'])"
        else:
            pattern = re.escape(frm)
        flags = 0 if case == "exact" else re.IGNORECASE
        rx = re.compile(pattern, flags)

        def _repl(m):
            token = _enclosing_token(text, m.start(), m.end())
            if _token_is_guarded(token):
                return m.group(0)            # leave guarded spans untouched
            if case == "preserve":
                return _mirror_case(m.group(0), to)
            return to                        # force / exact -> verbatim

        return rx.sub(_repl, text)
    except Exception:
        log.debug("learn._apply_one failed for %r", rule, exc_info=True)
        return text


def apply_corrections(text, rules):
    """Apply enabled correction rules to ``text`` (longest-phrase-first so a
    longer phrase wins over a contained shorter one). Deterministic, fuzzy OFF.
    Never raises -- returns the input on any failure."""
    try:
        if not text or not rules:
            return text
        active = [r for r in rules
                  if isinstance(r, dict) and r.get("enabled", True)
                  and r.get("from")]
        active.sort(key=lambda r: len(str(r.get("from", ""))), reverse=True)
        for r in active:
            text = _apply_one(text, r)
        return text
    except Exception:
        log.debug("learn.apply_corrections failed", exc_info=True)
        return text


# ---------------------------------------------------------------------------
# Biasing (TIER 0): build the hotwords string + the prompt term list.
# ---------------------------------------------------------------------------
def top_terms(vocab, cap=24):
    """The top ``cap`` terms by (score, recency)."""
    try:
        items = sorted(
            vocab.items(),
            key=lambda kv: (float((kv[1] or {}).get("score", 0) if isinstance(kv[1], dict) else 0),
                            float((kv[1] or {}).get("last", 0) if isinstance(kv[1], dict) else 0)),
            reverse=True)
        return [t for t, _ in items[:cap]]
    except Exception:
        log.debug("learn.top_terms failed", exc_info=True)
        return []


def build_hotwords(vocab, cap=32):
    """faster-whisper's ``hotwords`` is a single space-joined string. Capped to
    avoid over-stuffing (which makes Whisper hallucinate the terms)."""
    try:
        return " ".join(top_terms(vocab, cap=cap)) or ""
    except Exception:
        return ""


def build_prompt_terms(vocab, cap=12):
    """A short comma-list of the strongest terms, to APPEND to the existing
    initial_prompt (prompt + hotwords share Whisper's ~224-token budget, so the
    prompt gets a smaller slice)."""
    try:
        return top_terms(vocab, cap=cap)
    except Exception:
        return []


def augmented_prompt(base_prompt, vocab, cap=12):
    """``base_prompt`` with a short vocabulary list appended (or the base prompt
    unchanged if there are no terms)."""
    try:
        terms = build_prompt_terms(vocab, cap=cap)
        base = base_prompt or ""
        if not terms:
            return base or None
        suffix = " Vocabulary: " + ", ".join(terms) + "."
        return (base + suffix).strip()
    except Exception:
        return base_prompt or None
