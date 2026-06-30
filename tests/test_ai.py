"""AI edit module: provider dispatch + response cleaning (HTTP mocked)."""

from voiceflow import ai


def test_clean_strips_wrappers():
    assert ai._clean('"hello"') == "hello"
    assert ai._clean("```\nhi\n```") == "hi"
    assert ai._clean("  plain  ") == "plain"


def test_ollama_dispatch(monkeypatch):
    captured = {}

    def fake_post(url, payload, headers, timeout):
        captured["url"] = url
        captured["model"] = payload["model"]
        return {"message": {"content": "the cat sat"}}

    monkeypatch.setattr(ai, "_post_json", fake_post)
    cfg = {"ai_provider": "ollama", "ai_model": "qwen2.5:3b"}
    out, err = ai.edit_text("remove the word dog", "the cat sat dog", cfg)
    assert err is None and out == "the cat sat"
    assert captured["url"].endswith("/api/chat")
    assert captured["model"] == "qwen2.5:3b"


def test_openai_requires_key():
    out, err = ai.edit_text("x", "y", {"ai_provider": "openai", "ai_api_key": ""})
    assert out is None and "key" in err.lower()


def test_unknown_provider():
    out, err = ai.edit_text("x", "y", {"ai_provider": "bogus"})
    assert out is None and "bogus" in err
