"""
Tests for voiceflow.learn -- diff -> correction rules + vocabulary, the runtime
apply_corrections (with all its guards), and the hotword/prompt builders.

Hermetic: the corrections.json + personal_vocab.json paths are redirected into a
tmp dir, so the real learned data is never touched. No model / GUI / OS.
"""

from __future__ import annotations

import os

import pytest

import voiceflow.learn as L


@pytest.fixture
def learn_paths(tmp_path, monkeypatch):
    corr = os.path.join(str(tmp_path), "corrections.json")
    vocab = os.path.join(str(tmp_path), "personal_vocab.json")
    monkeypatch.setattr(L, "CORRECTIONS_PATH", corr)
    monkeypatch.setattr(L, "PERSONAL_VOCAB_PATH", vocab)
    monkeypatch.setattr(L, "ensure_data_dir", lambda: str(tmp_path))
    return corr, vocab


# ===========================================================================
# derive(): diff -> rules + terms
# ===========================================================================
def test_derive_brand_recapitalization():
    rules, terms = L.derive("meet me at big air", "meet me at Big Air")
    assert len(rules) == 1
    r = rules[0]
    assert r["from"] == "big air"
    assert r["to"] == "Big Air"
    assert r["case"] == "force"          # pure recapitalization -> proper noun
    assert r["whole_word"] is True
    assert "Big Air" in terms


def test_derive_proper_noun_word_swap():
    rules, terms = L.derive("i use openverba daily", "i use OpenVerba daily")
    assert len(rules) == 1
    assert rules[0]["to"] == "OpenVerba"
    assert rules[0]["case"] == "force"
    assert "OpenVerba" in terms


def test_derive_lowercase_typo_is_preserve():
    rules, _terms = L.derive("please recieve it", "please receive it")
    assert len(rules) == 1
    assert rules[0]["from"] == "recieve"
    assert rules[0]["to"] == "receive"
    assert rules[0]["case"] == "preserve"   # not a proper noun


def test_derive_rejects_long_rewrite():
    # A >4-token replaced span is a sentence edit, not a vocabulary fix.
    orig = "the quick brown fox jumps over"
    edited = "a completely different sentence entirely written now"
    rules, _terms = L.derive(orig, edited)
    assert rules == []


def test_derive_ignores_pure_insert_and_delete():
    # Inserting words (no replacement) yields no substitution rule.
    rules, _ = L.derive("hello world", "hello there world")
    assert rules == []
    # Deleting words yields no rule.
    rules2, _ = L.derive("hello there world", "hello world")
    assert rules2 == []


def test_derive_new_capitalized_phrase_is_a_term():
    # Even without a clean 1:1 replace, a new proper noun is vocabulary.
    _rules, terms = L.derive("call me", "call me at Big Air today")
    assert "Big Air" in terms


def test_derive_empty_edit():
    rules, terms = L.derive("something", "")
    assert rules == [] and terms == []


# ===========================================================================
# learn(): persistence + merge/hits + vocab bump
# ===========================================================================
def test_learn_persists_and_merges(learn_paths):
    corr, vocab = learn_paths
    res = L.learn("meet me at big air", "meet me at Big Air")
    assert res["corrections"]
    rules = L.load_corrections()
    assert len(rules) == 1
    assert rules[0]["hits"] == 1
    v = L.load_vocab()
    assert "Big Air" in v
    score1 = v["Big Air"]["score"]

    # Same correction again -> hits bumps, vocab score grows, no duplicate rule.
    L.learn("big air rocks", "Big Air rocks")
    rules2 = L.load_corrections()
    assert len(rules2) == 1
    assert rules2[0]["hits"] == 2
    assert L.load_vocab()["Big Air"]["score"] > score1


def test_learn_no_change_learns_nothing(learn_paths):
    res = L.learn("same text", "same text")
    assert res["corrections"] == []
    assert L.load_corrections() == []


# ===========================================================================
# apply_corrections(): the guaranteed deterministic fix + guards
# ===========================================================================
def _rule(frm, to, case="force", whole_word=True):
    return {"from": frm, "to": to, "case": case, "whole_word": whole_word,
            "enabled": True, "hits": 1}


def test_apply_force_case_insensitive():
    rules = [_rule("big air", "Big Air")]
    assert L.apply_corrections("call big air now", rules) == "call Big Air now"
    assert L.apply_corrections("CALL BIG AIR", rules) == "CALL Big Air"


def test_apply_whole_word_only():
    rules = [_rule("air", "AIR")]
    # 'airplane' / 'fairground' must NOT be touched.
    assert L.apply_corrections("airplane fairground air", rules) == \
        "airplane fairground AIR"


def test_apply_longest_phrase_first():
    rules = [
        _rule("big air", "Big Air"),
        _rule("big air conditioning", "Big Air Conditioning"),
    ]
    out = L.apply_corrections("call big air conditioning today", rules)
    assert out == "call Big Air Conditioning today"


def test_apply_preserve_case():
    rules = [_rule("recieve", "receive", case="preserve")]
    assert L.apply_corrections("i recieve mail", rules) == "i receive mail"
    # Sentence-start capitalization is mirrored.
    assert L.apply_corrections("Recieve mail", rules) == "Receive mail"
    # ALL-CAPS mirrored.
    assert L.apply_corrections("RECIEVE", rules) == "RECEIVE"


def test_apply_exact_case_sensitive():
    rules = [_rule("Foo", "Bar", case="exact")]
    assert L.apply_corrections("Foo and foo", rules) == "Bar and foo"


def test_apply_disabled_rule_skipped():
    r = _rule("big air", "Big Air")
    r["enabled"] = False
    assert L.apply_corrections("big air", [r]) == "big air"


def test_apply_fuzzy_off():
    # A near-miss ("bigair") must NOT be corrected -- exact (modulo case) only.
    rules = [_rule("big air", "Big Air")]
    assert L.apply_corrections("bigair", rules) == "bigair"


def test_apply_guard_url():
    rules = [_rule("air", "AIR")]
    # The standalone word is fixed; the one inside a URL token is left alone.
    assert L.apply_corrections("visit air-site.com for air", rules) == \
        "visit air-site.com for AIR"
    assert "AIR" not in L.apply_corrections("http://air.example", rules)


def test_apply_guard_email():
    rules = [_rule("air", "AIR")]
    out = L.apply_corrections("mail air@air.com about air", rules)
    assert out == "mail air@air.com about AIR"


def test_apply_guard_all_digits_and_codeish():
    rules = [_rule("100", "ONE-HUNDRED")]
    # all-digit token guarded.
    assert L.apply_corrections("value 100 here", rules) == "value 100 here"
    rules2 = [_rule("var", "VARIABLE")]
    # code-ish token (underscore / mixed alnum) guarded.
    assert L.apply_corrections("my_var = var2 plus var", rules2) == \
        "my_var = var2 plus VARIABLE"


def test_apply_empty_inputs():
    assert L.apply_corrections("", [_rule("a", "b")]) == ""
    assert L.apply_corrections("hello", []) == "hello"


# ===========================================================================
# build_hotwords / prompt
# ===========================================================================
def test_build_hotwords_cap():
    vocab = {("term%02d" % i): {"score": float(i), "last": float(i)}
             for i in range(50)}
    hw = L.build_hotwords(vocab, cap=10)
    parts = hw.split()
    assert len(parts) == 10
    # Highest score wins (term49 has the top score).
    assert "term49" in parts
    assert "term00" not in parts


def test_build_hotwords_empty():
    assert L.build_hotwords({}) == ""


def test_top_terms_orders_by_score_then_recency():
    vocab = {
        "A": {"score": 5.0, "last": 1.0},
        "B": {"score": 5.0, "last": 9.0},   # same score, newer
        "C": {"score": 9.0, "last": 1.0},   # highest score
    }
    assert L.top_terms(vocab, cap=3) == ["C", "B", "A"]


def test_augmented_prompt_appends_terms():
    vocab = {"Big Air": {"score": 7.0, "last": 1.0}}
    out = L.augmented_prompt("Base prompt.", vocab)
    assert out.startswith("Base prompt.")
    assert "Big Air" in out
    assert "Vocabulary:" in out


def test_augmented_prompt_no_terms_returns_base():
    assert L.augmented_prompt("Base.", {}) == "Base."


# ===========================================================================
# Defensiveness: never raise
# ===========================================================================
def test_load_corrupt_files_default(tmp_path, monkeypatch):
    bad = os.path.join(str(tmp_path), "corrections.json")
    with open(bad, "w", encoding="utf-8") as f:
        f.write("{not valid json")
    monkeypatch.setattr(L, "CORRECTIONS_PATH", bad)
    assert L.load_corrections() == []
    badv = os.path.join(str(tmp_path), "vocab.json")
    with open(badv, "w", encoding="utf-8") as f:
        f.write("nope")
    monkeypatch.setattr(L, "PERSONAL_VOCAB_PATH", badv)
    assert L.load_vocab() == {}


def test_apply_corrections_never_raises():
    # A malformed rule must not crash apply.
    assert L.apply_corrections("text", [{"garbage": True}]) == "text"
    assert L.apply_corrections("text", [None]) == "text"


# ===========================================================================
# C1/C2 regression: a sentence-start capitalization must NEVER become a global
# force rule (which would corrupt every later transcript) or pollute vocab.
# ===========================================================================
def test_derive_sentence_start_article_makes_no_rule():
    # "a"->"A" / "the"->"The" are grammar, not fixes -> no rule, no vocab.
    for o, e in (("a cat", "A cat"), ("the dog", "The dog"), ("it works", "It works")):
        rules, terms = L.derive(o, e)
        assert rules == [], "%r->%r wrongly made a rule: %r" % (o, e, rules)
        assert terms == [], "%r->%r wrongly learned vocab: %r" % (o, e, terms)


def test_derive_sentence_start_typo_is_preserve_not_force():
    # A typo fixed at a sentence start ("recieve"->"Receive") fixes the spelling
    # but must NOT force the capital everywhere, nor add "Receive" to vocab.
    rules, terms = L.derive("recieve mail", "Receive mail")
    assert len(rules) == 1
    assert rules[0]["from"] == "recieve" and rules[0]["to"] == "Receive"
    assert rules[0]["case"] == "preserve"
    assert "Receive" not in terms
    # Applied mid-sentence, it mirrors lowercase context (no stray capital).
    assert L.apply_corrections("i recieve mail", rules) == "i receive mail"


def test_derive_does_not_corrupt_articles_after_learning():
    # The whole point: learning from "a cat"->"A cat" must not uppercase every "a".
    rules, _ = L.derive("a cat", "A cat")
    assert L.apply_corrections("i saw a cat and a dog", rules) \
        == "i saw a cat and a dog"


def test_derive_midsentence_name_still_learned_as_vocab():
    # A capitalized name MID-sentence is a real proper noun -> still biased for.
    _rules, terms = L.derive("call joseph today", "call Joseph today")
    assert "Joseph" in terms
