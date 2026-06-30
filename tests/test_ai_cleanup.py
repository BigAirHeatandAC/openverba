"""AI cleanup: local Ollama-powered dictation polish (HTTP mocked)."""

from voiceflow import ai


def test_cleanup_text_light_is_rulebased(monkeypatch):
    """Light cleanup is now rule-based + offline: it removes filler, fixes caps,
    and makes NO network call (A.1)."""
    def boom(*args, **kwargs):
        raise AssertionError("light must not call _post_json")

    monkeypatch.setattr(ai, "_post_json", boom)
    cfg = {"ai_provider": "ollama", "ai_model": "qwen2.5:3b", "ai_timeout": 90}

    text, err = ai.cleanup_text("hello world um", "light", cfg)
    assert err is None and text == "Hello world"


def test_cleanup_text_medium_mocked(monkeypatch):
    """Medium cleanup: + false-starts, light grammar."""
    captured = {}

    def fake_post(url, payload, headers, timeout):
        captured["payload"] = payload
        return {"message": {"content": "Meet at 3pm"}}

    monkeypatch.setattr(ai, "_post_json", fake_post)
    cfg = {"ai_provider": "ollama", "ai_model": "qwen2.5:3b"}

    text, err = ai.cleanup_text("meet at 4 actually 3 o'clock", "medium", cfg)
    assert err is None and text == "Meet at 3pm"
    sys_prompt = captured["payload"]["messages"][0]["content"].lower()
    assert "false-start" in sys_prompt
    # S2: medium prompt must also forbid answering the text.
    assert "do not answer" in sys_prompt


def test_cleanup_text_high_mocked(monkeypatch):
    """High cleanup: + rephrase for clarity."""
    captured = {}

    def fake_post(url, payload, headers, timeout):
        captured["payload"] = payload
        return {"message": {"content": "The quick brown fox."}}

    monkeypatch.setattr(ai, "_post_json", fake_post)
    cfg = {"ai_provider": "ollama", "ai_model": "qwen2.5:3b"}

    text, err = ai.cleanup_text("quick uh brown um fox", "high", cfg)
    assert err is None and text == "The quick brown fox."
    sys_prompt = captured["payload"]["messages"][0]["content"].lower()
    assert "rephrase" in sys_prompt
    # S2: high prompt forbids answering (it already did; keep the guarantee).
    assert "answer it" in sys_prompt or "do not answer" in sys_prompt


def test_all_cleanup_prompts_forbid_answering():
    """S2: every cleanup level (light/medium/high) must instruct the model NOT
    to answer/respond to the text -- only clean it."""
    for level in ("light", "medium", "high"):
        prompt = ai._CLEANUP_PROMPTS[level].lower()
        assert ("do not answer" in prompt or "answer it" in prompt), (
            "level %r prompt does not forbid answering" % level)


# ===========================================================================
# S1: cleanup uses the short cleanup_timeout (not the 90s ai_timeout), and
# is_available() is cached within the TTL.
# ===========================================================================
def test_cleanup_uses_cleanup_timeout_not_ai_timeout(monkeypatch):
    """Inline auto-cleanup (medium/high) must use cleanup_timeout (short) for the
    HTTP call, NOT ai_timeout (90s) -- a slow Ollama can't stall the paste path.
    (light is rule-based now and makes no HTTP call.)"""
    captured = {}

    def fake_post(url, payload, headers, timeout):
        captured["timeout"] = timeout
        return {"message": {"content": "Clean"}}

    monkeypatch.setattr(ai, "_post_json", fake_post)
    cfg = {"ai_provider": "ollama", "ai_model": "qwen2.5:3b",
           "ai_timeout": 90, "cleanup_timeout": 8.0}

    text, err = ai.cleanup_text("dirty text", "medium", cfg)
    assert err is None and text == "Clean"
    # The cleanup HTTP call used cleanup_timeout, not ai_timeout.
    assert captured["timeout"] == 8.0
    assert captured["timeout"] != 90


def test_cleanup_timeout_defaults_when_absent(monkeypatch):
    """When cleanup_timeout is unset, default to 10.0 (still not 90)."""
    captured = {}

    def fake_post(url, payload, headers, timeout):
        captured["timeout"] = timeout
        return {"message": {"content": "Clean"}}

    monkeypatch.setattr(ai, "_post_json", fake_post)
    cfg = {"ai_provider": "ollama", "ai_model": "qwen2.5:3b", "ai_timeout": 90}

    ai.cleanup_text("dirty", "medium", cfg)
    assert captured["timeout"] == 10.0


def test_is_available_is_cached_within_ttl(monkeypatch):
    """S1: is_available() caches its probe for a TTL, so two back-to-back calls
    only probe the backend once (a down Ollama can't cost ~2s per utterance)."""
    ai.reset_availability_cache()
    probes = {"n": 0}

    def fake_probe(cfg):
        probes["n"] += 1
        return False        # simulate a down Ollama

    monkeypatch.setattr(ai, "_probe_available", fake_probe)
    cfg = {"ai_provider": "ollama", "ollama_url": "http://localhost:11434"}

    r1 = ai.is_available(cfg)
    r2 = ai.is_available(cfg)
    assert r1 is False and r2 is False
    # The underlying probe ran only ONCE despite two is_available() calls.
    assert probes["n"] == 1

    # force=True bypasses the cache and re-probes.
    ai.is_available(cfg, force=True)
    assert probes["n"] == 2
    ai.reset_availability_cache()


def test_is_available_cache_keyed_by_backend(monkeypatch):
    """Changing the backend URL re-probes (cache is keyed by provider+url)."""
    ai.reset_availability_cache()
    probes = {"n": 0}
    monkeypatch.setattr(ai, "_probe_available",
                        lambda cfg: probes.__setitem__("n", probes["n"] + 1) or True)

    ai.is_available({"ai_provider": "ollama", "ollama_url": "http://a:11434"})
    ai.is_available({"ai_provider": "ollama", "ollama_url": "http://b:11434"})
    assert probes["n"] == 2     # different URLs -> two probes
    ai.reset_availability_cache()


def test_cleanup_text_off_no_call(monkeypatch):
    """cleanup_level='off' returns text unchanged, no API call."""
    called = []

    def fake_post(*args, **kwargs):
        called.append(True)
        raise RuntimeError("Should not be called")

    monkeypatch.setattr(ai, "_post_json", fake_post)
    cfg = {"ai_provider": "ollama", "ai_model": "qwen2.5:3b"}

    text, err = ai.cleanup_text("hello", "off", cfg)
    assert text == "hello" and err is None
    assert not called


def test_cleanup_text_empty_returns_unchanged():
    """Empty/blank text returns unchanged, no API call."""
    cfg = {"ai_provider": "ollama"}

    text, err = ai.cleanup_text("", "light", cfg)
    assert text == "" and err is None

    text, err = ai.cleanup_text("   ", "light", cfg)
    assert text == "   " and err is None


def test_cleanup_text_non_ollama_fails():
    """LLM cleanup (medium/high) only supports local Ollama. (light is rule-based
    and provider-independent -- covered separately.)"""
    cfg = {"ai_provider": "anthropic", "ai_api_key": "fake"}

    text, err = ai.cleanup_text("hello", "medium", cfg)
    assert text is None and "ollama" in err.lower()


def test_cleanup_text_url_error_handled(monkeypatch):
    """URL error (Ollama not running) returns graceful error."""
    import urllib.error

    def fake_post(*args, **kwargs):
        raise urllib.error.URLError("Connection refused")

    monkeypatch.setattr(ai, "_post_json", fake_post)
    cfg = {"ai_provider": "ollama", "ai_model": "qwen2.5:3b"}

    text, err = ai.cleanup_text("hello", "medium", cfg)
    assert text is None and "Could not reach" in err


def test_cleanup_text_generic_exception_handled(monkeypatch):
    """Generic exception returns graceful error -- for MEDIUM (light is now
    rule-based and never reaches _post_json, so it can't surface this error)."""
    def fake_post(*args, **kwargs):
        raise RuntimeError("Model crashed")

    monkeypatch.setattr(ai, "_post_json", fake_post)
    cfg = {"ai_provider": "ollama", "ai_model": "qwen2.5:3b"}

    text, err = ai.cleanup_text("hello", "medium", cfg)
    assert text is None and "Model crashed" in err


# ===========================================================================
# A.1: rule-based "Light" cleanup (NO LLM) -- instant, offline, deterministic.
# ===========================================================================
def test_light_rulebased_removes_filler_and_caps():
    assert ai.cleanup_light_rulebased("so um I uh think") == "So I think"


def test_light_rulebased_keeps_like_and_you_know():
    # "like" / "you know" are NOT filler here; only capitalization changes.
    assert ai.cleanup_light_rulebased("I like it, you know") == "I like it, you know"


def test_light_rulebased_no_false_positives_inside_words():
    # filler patterns must not fire inside real words.
    for word in ("summer", "humming", "ahead", "Graham", "mummy"):
        assert ai.cleanup_light_rulebased(word) == word[0].upper() + word[1:]


def test_light_rulebased_space_before_punct_and_sentence_caps():
    assert ai.cleanup_light_rulebased(
        "hello . how are you") == "Hello. How are you"


def test_light_rulebased_filler_between_commas_collapses():
    # "so, um, yeah" -> drop "um" -> the dangling ", ," collapses to one comma.
    assert ai.cleanup_light_rulebased("so, um, yeah") == "So, yeah"


def test_light_rulebased_preserves_trailing_space():
    # clean_transcript adds a trailing space for pasting; light must not eat it.
    assert ai.cleanup_light_rulebased("um hello ") == "Hello "


def test_light_rulebased_empty_and_blank():
    assert ai.cleanup_light_rulebased("") == ""
    assert ai.cleanup_light_rulebased("   ") == "   "


def test_cleanup_text_light_makes_no_network_call(monkeypatch):
    """A.1: light is rule-based -- it must NOT touch _post_json (no network),
    even if Ollama is configured. Monkeypatch _post_json to fail if called."""
    def boom(*args, **kwargs):
        raise AssertionError("light cleanup must not call _post_json")

    monkeypatch.setattr(ai, "_post_json", boom)
    cfg = {"ai_provider": "ollama", "ai_model": "qwen2.5:3b"}
    text, err = ai.cleanup_text("so um I uh think", "light", cfg)
    assert err is None and text == "So I think"


def test_cleanup_text_light_works_for_non_ollama_provider(monkeypatch):
    """A.1: light is provider-independent -- it works even when ai_provider is a
    cloud provider (it never reaches the provider check)."""
    def boom(*args, **kwargs):
        raise AssertionError("light cleanup must not call _post_json")

    monkeypatch.setattr(ai, "_post_json", boom)
    cfg = {"ai_provider": "anthropic", "ai_api_key": "k"}
    text, err = ai.cleanup_text("um hi", "light", cfg)
    assert err is None and text == "Hi"


# ===========================================================================
# A.2: medium/high use a dedicated SMALL cleanup_model + keep_alive.
# ===========================================================================
def test_cleanup_uses_cleanup_model(monkeypatch):
    captured = {}

    def fake_post(url, payload, headers, timeout):
        captured["payload"] = payload
        return {"message": {"content": "Clean"}}

    monkeypatch.setattr(ai, "_post_json", fake_post)
    cfg = {"ai_provider": "ollama", "cleanup_model": "qwen2.5:1.5b",
           "ai_model": "qwen2.5:3b"}
    ai.cleanup_text("dirty text here", "medium", cfg)
    assert captured["payload"]["model"] == "qwen2.5:1.5b"


def test_cleanup_falls_back_to_ai_model_when_no_cleanup_model(monkeypatch):
    captured = {}

    def fake_post(url, payload, headers, timeout):
        captured["payload"] = payload
        return {"message": {"content": "Clean"}}

    monkeypatch.setattr(ai, "_post_json", fake_post)
    cfg = {"ai_provider": "ollama", "ai_model": "qwen2.5:3b"}  # no cleanup_model
    ai.cleanup_text("dirty text here", "medium", cfg)
    assert captured["payload"]["model"] == "qwen2.5:3b"


def test_cleanup_sends_keep_alive(monkeypatch):
    captured = {}

    def fake_post(url, payload, headers, timeout):
        captured["payload"] = payload
        return {"message": {"content": "Clean"}}

    monkeypatch.setattr(ai, "_post_json", fake_post)
    # default
    ai.cleanup_text("dirty text", "medium",
                    {"ai_provider": "ollama", "ai_model": "m"})
    assert captured["payload"]["keep_alive"] == "10m"
    # override
    ai.cleanup_text("dirty text", "high",
                    {"ai_provider": "ollama", "ai_model": "m",
                     "cleanup_keep_alive": "30m"})
    assert captured["payload"]["keep_alive"] == "30m"


def test_ai_edit_still_uses_ai_model_not_cleanup_model(monkeypatch):
    """The hold-to-edit AI path (_ollama via edit_text) must keep using ai_model,
    NOT cleanup_model -- guards against cross-wiring."""
    captured = {}

    def fake_post(url, payload, headers, timeout):
        captured["payload"] = payload
        return {"message": {"content": "edited"}}

    monkeypatch.setattr(ai, "_post_json", fake_post)
    cfg = {"ai_provider": "ollama", "ai_model": "qwen2.5:3b",
           "cleanup_model": "qwen2.5:1.5b"}
    out, err = ai.edit_text("make it shorter", "some selected text", cfg)
    assert err is None
    assert captured["payload"]["model"] == "qwen2.5:3b"
