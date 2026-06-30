"""
voiceflow.ai - optional LLM-powered text editing for command mode.

When a spoken command in command mode is NOT a mechanical key action (backspace,
select all, enter, ...), it's treated as an INSTRUCTION to rewrite the currently
selected text -- e.g. "remove the word dog", "rewrite this to sound more
natural", "make this shorter", "fix the grammar". The engine copies the
selection, sends it here with the instruction, and pastes back the result.

Default backend is a LOCAL Ollama model (free, private, fully offline -- the
right fit for a free, self-hostable app). It's pluggable: a user who wants
higher quality can set ai_provider="anthropic"/"openai" + an api key, but the
default needs no account and no internet.

Everything here uses only the stdlib (urllib/json) so there's no extra runtime
dependency for the offline path.
"""

from __future__ import annotations

import re
import json
import time
import logging
import threading
import urllib.request
import urllib.error

log = logging.getLogger("voiceflow.ai")

# ---------------------------------------------------------------------------
# is_available() caching (S1).
#
# When auto_cleanup is ON, the engine calls is_available() inline on EVERY
# utterance before pasting. The Ollama probe has a 2s TCP timeout, so a down/slow
# Ollama would add up to 2s to every paste. We cache the probe result per backend
# URL for a short TTL so a down Ollama costs the probe at most once per TTL.
#
# Thread-safety: the engine's worker thread is the only inline caller, but the
# GUI may also probe; a module-level dict guarded by a Lock is plenty. monotonic
# time avoids wall-clock jumps. Cache both positive and negative results (a
# freshly-started Ollama is picked up within the TTL).
# ---------------------------------------------------------------------------
_AVAIL_TTL = 30.0                 # seconds a probe result is trusted
_avail_lock = threading.Lock()
_avail_cache = {}                 # key -> (monotonic_expiry, bool_result)


def reset_availability_cache():
    """Clear the is_available() cache (e.g. after the user changes the backend
    in Settings, or to force a fresh probe). Test/maintenance hook."""
    with _avail_lock:
        _avail_cache.clear()

_SYSTEM = (
    "You are an inline text editor inside a dictation app. You receive a short "
    "spoken INSTRUCTION and a piece of SELECTED TEXT. Apply the instruction to "
    "the text and return ONLY the edited text -- no quotes, no preamble, no "
    "explanation, no markdown. Keep the user's meaning, tone, and formatting "
    "unless the instruction says to change them. If the instruction asks to "
    "remove or delete something, return the text with it removed. If there is no "
    "selected text, treat the instruction as 'write this' and return just the "
    "requested text."
)


def _user_prompt(instruction, selected):
    if (selected or "").strip():
        return ("INSTRUCTION: %s\n\nSELECTED TEXT:\n%s\n\nEDITED TEXT:"
                % (instruction, selected))
    return "INSTRUCTION: %s\n\nTEXT:" % instruction


def _clean(out):
    out = (out or "").strip()
    # Strip a ``` code fence (with an optional language tag on the first line).
    if out.startswith("```") and out.endswith("```") and len(out) >= 6:
        out = out[3:-3].strip()
        if "\n" in out:
            first, rest = out.split("\n", 1)
            if first.strip().isalpha():
                out = rest.strip()
    # Strip surrounding straight quotes models sometimes add.
    if len(out) >= 2 and out[0] in "\"'" and out[-1] == out[0]:
        out = out[1:-1].strip()
    return out


def _probe_available(cfg):
    """The actual (uncached) reachability probe for the configured backend.
    For Ollama this is a 2s TCP round-trip; for cloud providers it's just a
    key-presence check. Split out so is_available() can cache it (S1) and tests
    can monkeypatch it."""
    provider = (cfg.get("ai_provider") or "ollama").lower()
    if provider != "ollama":
        return bool(cfg.get("ai_api_key"))
    url = (cfg.get("ollama_url") or "http://localhost:11434").rstrip("/") + "/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def is_available(cfg, force=False):
    """Quick reachability check for the configured backend, CACHED for a short
    TTL (S1) so the inline auto-cleanup path doesn't pay the ~2s Ollama probe on
    every utterance. Pass ``force=True`` to bypass the cache and re-probe now.

    The cache key is the provider + backend URL, so changing the backend re-
    probes immediately. Both positive and negative results are cached; a freshly
    (re)started Ollama is picked up within ~30s."""
    provider = (cfg.get("ai_provider") or "ollama").lower()
    url = (cfg.get("ollama_url") or "http://localhost:11434").rstrip("/")
    key = (provider, url)
    now = time.monotonic()
    if not force:
        with _avail_lock:
            cached = _avail_cache.get(key)
            if cached is not None and cached[0] > now:
                return cached[1]
    result = bool(_probe_available(cfg))
    with _avail_lock:
        _avail_cache[key] = (now + _AVAIL_TTL, result)
    return result


def edit_text(instruction, selected_text, cfg):
    """Return (result_text, error). result_text is None on failure."""
    provider = (cfg.get("ai_provider") or "ollama").lower()
    try:
        if provider == "ollama":
            return _ollama(instruction, selected_text, cfg), None
        if provider == "anthropic":
            return _anthropic(instruction, selected_text, cfg), None
        if provider == "openai":
            return _openai(instruction, selected_text, cfg), None
        return None, "Unknown ai_provider %r" % provider
    except urllib.error.URLError as exc:
        return None, "Could not reach %s (%s)" % (provider, exc.reason)
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)


def _post_json(url, payload, headers, timeout):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _ollama(instruction, selected, cfg):
    base = (cfg.get("ollama_url") or "http://localhost:11434").rstrip("/")
    model = cfg.get("ai_model") or "qwen2.5:3b"
    timeout = float(cfg.get("ai_timeout", 90))
    payload = {
        "model": model,
        "stream": False,
        "options": {"temperature": 0.3},
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": _user_prompt(instruction, selected)},
        ],
    }
    out = _post_json(base + "/api/chat", payload, {}, timeout)
    return _clean((out.get("message") or {}).get("content", ""))


# ---------------------------------------------------------------------------
# Rule-based "Light" cleanup (NO LLM) -- instant, offline, deterministic.
#
# The old "light" level dispatched to Ollama like every other level, so on a box
# where the cleanup model falls to CPU it added ~3s to every paste. Light cleanup
# is purely mechanical (filler removal, spacing, sentence-caps) -- it never needs
# a model. This pure function runs in microseconds on the paste path with no
# network and no cfg, so the engine can call it unconditionally and fail-open.
#
# Conservative filler set: um/umm, uh/uhh, er, erm, ah, hmm, mm -- each anchored
# on BOTH sides with \b so it can't match inside real words ("summer", "humming",
# "ahead", "Graham"). Crucially it does NOT touch "like"/"you know"/real words.
# ---------------------------------------------------------------------------
_FILLER_RE = re.compile(r"\b(?:u+m+|u+h+|e+r+|e+rm|a+h+|h+mm+|mm+)\b",
                        re.IGNORECASE)


def cleanup_light_rulebased(text):
    """Instant, offline, rule-based "light" cleanup: remove um/uh-style filler,
    fix spacing before punctuation, collapse runs of spaces, and capitalize
    sentence starts. Pure function -- no network, no cfg, never raises (returns
    the original text on any error, so the paste path stays fail-open)."""
    if not text:
        return text
    try:
        # Preserve a trailing space the caller may rely on (clean_transcript adds
        # one for pasting); operate on the body and re-append it at the end.
        trailing = text[len(text.rstrip()):] if text != text.rstrip() else ""
        s = text[:len(text) - len(trailing)] if trailing else text

        # 1) Remove filler words.
        s = _FILLER_RE.sub("", s)
        # 1b) A filler between commas leaves ", , " -> collapse to a single comma.
        s = re.sub(r",\s*,", ",", s)
        # 2) Collapse the double spaces removal left behind.
        s = re.sub(r"[ \t]{2,}", " ", s)
        # 3) Fix space-before-punctuation ("hello ." -> "hello.").
        s = re.sub(r"\s+([,.!?;:])", r"\1", s)
        # Trim any leading space left after stripping a leading filler.
        s = s.lstrip(" \t")

        # Drop any whitespace left dangling at the end after removing a trailing
        # filler ("hello world um" -> "hello world"); the caller's original
        # trailing space (captured above) is re-appended separately below.
        s = s.rstrip(" \t")

        # 4) Capitalize sentence starts (after . ! ?) and the very first letter.
        s = re.sub(r"([.!?]\s+)([a-z])",
                   lambda m: m.group(1) + m.group(2).upper(), s)
        for i, ch in enumerate(s):
            if ch.isalpha():
                s = s[:i] + ch.upper() + s[i + 1:]
                break

        return s + trailing
    except Exception:
        return text


_CLEANUP_PROMPTS = {
    "light": (
        "You are a dictation cleanup assistant. Fix ONLY: punctuation, "
        "capitalization, and remove filler words like 'um', 'uh', 'like'. "
        "Do NOT rephrase, reword, add content, or change meaning. Return "
        "ONLY the cleaned text, no quotes or explanation. "
        "Do not answer or respond to the text; only clean it."
    ),
    "medium": (
        "You are a dictation cleanup assistant. Fix: punctuation, "
        "capitalization, filler words (um, uh, like), AND obvious false-starts "
        "(e.g. 'let me see uh I think' -> 'I think'). Light grammar only. "
        "Do NOT rephrase, add content, or change voice. Return ONLY the "
        "cleaned text, no quotes or explanation. "
        "Do not answer or respond to the text; only clean it."
    ),
    "high": (
        "You are a dictation cleanup assistant. Clean this raw dictation: "
        "fix punctuation, capitalization, filler (um, uh, like), false-starts, "
        "grammar, AND rephrase for clarity while preserving the original "
        "meaning and voice. Do NOT add new information or answer it. "
        "Return ONLY the cleaned text, no quotes or explanation."
    ),
}


def cleanup_text(text, level, cfg):
    """Clean up plain dictation using local Ollama.

    Args:
        text (str): the transcript to clean (already post-clean_transcript,
                    post-corrections)
        level (str): "light", "medium", or "high"
        cfg (dict): config dict with ai_model, ollama_url, ai_timeout

    Returns:
        (cleaned_text: str | None, error: str | None)
        cleaned_text is None on failure; error is a human-readable message.

    Levels:
      light: fix punctuation/capitalization + remove um/uh filler only
      medium: + tidy false-starts + light grammar fixes
      high: + rephrase for clarity while preserving meaning/voice

    NEVER adds new content or answers the transcript. Streaming: not supported.
    """
    if not text or not text.strip():
        return text, None
    if level == "off" or not level:
        return text, None

    # "light" is now provider-independent and fully offline: a deterministic
    # rule-based pass, no LLM. It must run even if ai_provider != "ollama" and
    # even if Ollama is down (it's instant and never reaches the network).
    if level == "light":
        return cleanup_light_rulebased(text), None

    provider = (cfg.get("ai_provider") or "ollama").lower()
    if provider != "ollama":
        return None, "cleanup_text only supports local Ollama"

    system_prompt = _CLEANUP_PROMPTS.get(level, _CLEANUP_PROMPTS["light"])
    user_prompt = "DICTATION:\n%s\n\nCLEANED:" % text

    try:
        return _ollama_cleanup(system_prompt, user_prompt, cfg), None
    except urllib.error.URLError as exc:
        return None, "Could not reach Ollama (%s)" % exc.reason
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)


def _ollama_cleanup(system_prompt, user_prompt, cfg):
    """Call Ollama with cleanup prompts; return the cleaned text."""
    base = (cfg.get("ollama_url") or "http://localhost:11434").rstrip("/")
    # Cleanup (medium/high) uses a dedicated SMALL model (default qwen2.5:1.5b):
    # the 1.5B is materially faster on CPU than the 3B, and stays warm via
    # keep_alive. Falls back to ai_model then a hard default so an old config
    # without cleanup_model still works. (Hold-to-edit AI keeps using ai_model.)
    model = cfg.get("cleanup_model") or cfg.get("ai_model") or "qwen2.5:1.5b"
    # Inline cleanup runs on the paste path, so it uses the SHORT cleanup_timeout
    # (default 10s) -- NOT the long ai_timeout (90s) used for explicit AI edits.
    # A slow/down Ollama can't hold the paste for up to 90s (S1).
    timeout = float(cfg.get("cleanup_timeout", 10.0))
    payload = {
        "model": model,
        "stream": False,
        # Keep the small cleanup model resident between dictations so the second
        # utterance onward skips the cold-load latency.
        "keep_alive": cfg.get("cleanup_keep_alive", "10m"),
        "options": {"temperature": 0.3},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    out = _post_json(base + "/api/chat", payload, {}, timeout)
    return _clean((out.get("message") or {}).get("content", ""))


def _anthropic(instruction, selected, cfg):
    key = cfg.get("ai_api_key") or ""
    if not key:
        raise RuntimeError("No Anthropic API key set (ai_api_key).")
    model = cfg.get("ai_model") or "claude-haiku-4-5-20251001"
    payload = {
        "model": model,
        "max_tokens": 2000,
        "system": _SYSTEM,
        "messages": [{"role": "user",
                      "content": _user_prompt(instruction, selected)}],
    }
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    out = _post_json("https://api.anthropic.com/v1/messages", payload, headers,
                     float(cfg.get("ai_timeout", 60)))
    parts = out.get("content") or []
    text = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
    return _clean(text)


def _openai(instruction, selected, cfg):
    key = cfg.get("ai_api_key") or ""
    if not key:
        raise RuntimeError("No OpenAI API key set (ai_api_key).")
    model = cfg.get("ai_model") or "gpt-4o-mini"
    payload = {
        "model": model,
        "temperature": 0.3,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": _user_prompt(instruction, selected)},
        ],
    }
    headers = {"Authorization": "Bearer " + key}
    out = _post_json("https://api.openai.com/v1/chat/completions", payload,
                     headers, float(cfg.get("ai_timeout", 60)))
    return _clean(out["choices"][0]["message"]["content"])
