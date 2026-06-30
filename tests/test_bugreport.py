"""
Hermetic tests for voiceflow.bugreport - NO network, NO real email client.

We exercise:
  * get_diagnostics returns a dict with version/os keys and never raises, even
    when the log file is missing.
  * format_body includes the user's message, the diagnostics, and the reporter
    email.
  * send_via_form returns (False, ...) when urlopen raises, and (True, "sent")
    on a fake 200 + {"success": true} response (urlopen monkeypatched).
  * mailto_url is a valid mailto: with url-encoded subject/body to the
    configured email.
  * report() falls back to mailto when send_via_form fails, and returns "failed"
    when mailto also fails (both monkeypatched).
  * config coercion: a bad bug_report_method resets to "form"; the
    bug_report_email default is present.
"""

from __future__ import annotations

import io
import json
import urllib.parse

import pytest

from voiceflow import bugreport
from voiceflow import config
from voiceflow.constants import DEFAULT_CONFIG


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class _FakeResp:
    """Minimal context-manager stand-in for the object urlopen returns."""

    def __init__(self, body: bytes, status: int = 200):
        self._buf = io.BytesIO(body)
        self.status = status

    def read(self, n=-1):
        return self._buf.read(n)

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _cfg(**over):
    cfg = {
        "bug_report_email": "dev@example.com",
        "bug_report_method": "form",
        "model": "small.en",
        "device": "auto",
        "compute_type": "int8_float16",
    }
    cfg.update(over)
    return cfg


# ---------------------------------------------------------------------------
# get_diagnostics
# ---------------------------------------------------------------------------
def test_get_diagnostics_has_version_and_os(monkeypatch):
    # Force the log tail to "missing" so we also prove it doesn't blow up.
    monkeypatch.setattr(bugreport, "_log_tail", lambda *a, **k: "")
    diag = bugreport.get_diagnostics(_cfg())
    assert isinstance(diag, dict)
    assert "version" in diag
    assert "os" in diag
    assert "python" in diag
    assert diag["model"] == "small.en"
    assert diag["device"] == "auto"
    assert diag["compute_type"] == "int8_float16"


def test_get_diagnostics_never_raises_with_broken_inputs(monkeypatch):
    # _log_tail itself raising must be swallowed.
    def boom(*a, **k):
        raise RuntimeError("log read exploded")
    monkeypatch.setattr(bugreport, "_log_tail", boom)
    diag = bugreport.get_diagnostics(None)        # cfg=None too
    assert isinstance(diag, dict)
    assert "version" in diag and "os" in diag
    assert "log_tail" not in diag                 # omitted, not crashed


def test_get_diagnostics_includes_log_tail_when_present(monkeypatch):
    monkeypatch.setattr(bugreport, "_log_tail", lambda *a, **k: "line A\nline B")
    diag = bugreport.get_diagnostics(_cfg())
    assert diag.get("log_tail") == "line A\nline B"


# ---------------------------------------------------------------------------
# format_body
# ---------------------------------------------------------------------------
def test_format_body_includes_message_diag_and_email():
    diag = {"version": "1.0.0", "os": "Windows-11", "model": "small.en"}
    body = bugreport.format_body("It crashed on paste", diag, "me@example.com")
    assert "It crashed on paste" in body
    assert "--- Diagnostics ---" in body
    assert "version: 1.0.0" in body
    assert "os: Windows-11" in body
    assert "model: small.en" in body
    assert "me@example.com" in body


def test_format_body_without_reporter_email():
    body = bugreport.format_body("hi", {"version": "1.0.0"}, "")
    assert "hi" in body
    assert "Reporter email" not in body


def test_format_body_empty_message_placeholder():
    body = bugreport.format_body("", {}, None)
    assert "(no message provided)" in body


# ---------------------------------------------------------------------------
# send_via_form
# ---------------------------------------------------------------------------
def test_send_via_form_returns_false_when_urlopen_raises(monkeypatch):
    def boom(req, timeout=None):
        raise OSError("no network")
    monkeypatch.setattr(bugreport.urllib.request, "urlopen", boom)
    ok, detail = bugreport.send_via_form("msg", {"version": "1.0.0"}, "", _cfg())
    assert ok is False
    assert "no network" in detail


def test_send_via_form_success_on_200_and_success_true(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["data"] = req.data
        captured["headers"] = {k.lower(): v for k, v in req.header_items()}
        return _FakeResp(json.dumps({"success": "true",
                                     "message": "ok"}).encode("utf-8"), 200)
    monkeypatch.setattr(bugreport.urllib.request, "urlopen", fake_urlopen)

    ok, detail = bugreport.send_via_form(
        "my message", {"version": "1.0.0"}, "me@example.com", _cfg())
    assert ok is True
    assert detail == "sent"
    # POSTed to the configured email's formsubmit ajax endpoint.
    assert captured["url"] == "https://formsubmit.co/ajax/dev%40example.com"
    # JSON body carries the expected fields.
    payload = json.loads(captured["data"].decode("utf-8"))
    assert payload["_subject"] == "OpenVerba bug report"
    assert payload["message"] == "my message"
    assert payload["reporter_email"] == "me@example.com"
    assert payload["app"] == "OpenVerba"
    assert "diagnostics" in payload
    assert captured["headers"].get("content-type") == "application/json"
    assert captured["headers"].get("accept") == "application/json"


def test_send_via_form_false_when_success_falsey(monkeypatch):
    def fake_urlopen(req, timeout=None):
        return _FakeResp(json.dumps({"success": False}).encode("utf-8"), 200)
    monkeypatch.setattr(bugreport.urllib.request, "urlopen", fake_urlopen)
    ok, detail = bugreport.send_via_form("m", {}, "", _cfg())
    assert ok is False


def test_send_via_form_false_on_non_2xx(monkeypatch):
    def fake_urlopen(req, timeout=None):
        return _FakeResp(json.dumps({"success": True}).encode("utf-8"), 500)
    monkeypatch.setattr(bugreport.urllib.request, "urlopen", fake_urlopen)
    ok, detail = bugreport.send_via_form("m", {}, "", _cfg())
    assert ok is False
    assert "500" in detail


def test_send_via_form_false_when_no_email_configured(monkeypatch):
    def boom(req, timeout=None):
        raise AssertionError("urlopen must not be called without an email")
    monkeypatch.setattr(bugreport.urllib.request, "urlopen", boom)
    ok, detail = bugreport.send_via_form("m", {}, "", _cfg(bug_report_email=""))
    assert ok is False


# ---------------------------------------------------------------------------
# mailto_url
# ---------------------------------------------------------------------------
def test_mailto_url_is_valid_and_encoded():
    url = bugreport.mailto_url("hello world", {"version": "1.0.0"},
                              "me@example.com", _cfg())
    assert url.startswith("mailto:dev@example.com?")
    parsed = urllib.parse.urlparse(url)
    assert parsed.scheme == "mailto"
    assert parsed.path == "dev@example.com"
    qs = urllib.parse.parse_qs(parsed.query)
    assert qs["subject"] == ["OpenVerba bug report"]
    # The body is url-encoded; decoded it contains the message + diagnostics.
    body = qs["body"][0]
    assert "hello world" in body
    assert "--- Diagnostics ---" in body


def test_mailto_url_caps_body_length():
    long_msg = "x" * 5000
    url = bugreport.mailto_url(long_msg, {}, "", _cfg())
    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(parsed.query)
    body = qs["body"][0]
    assert "(truncated)" in body
    assert len(body) < 2000


# ---------------------------------------------------------------------------
# report (high level)
# ---------------------------------------------------------------------------
def test_report_sent_when_form_succeeds(monkeypatch):
    monkeypatch.setattr(bugreport, "send_via_form",
                        lambda *a, **k: (True, "sent"))
    monkeypatch.setattr(bugreport, "open_mailto",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("must not fall back when form ok")))
    status, detail = bugreport.report("m", True, "", _cfg())
    assert status == "sent"


def test_report_falls_back_to_mailto_when_form_fails(monkeypatch):
    monkeypatch.setattr(bugreport, "send_via_form",
                        lambda *a, **k: (False, "boom"))
    monkeypatch.setattr(bugreport, "open_mailto", lambda *a, **k: True)
    status, detail = bugreport.report("m", True, "", _cfg())
    assert status == "mailto"


def test_report_failed_when_form_and_mailto_both_fail(monkeypatch):
    monkeypatch.setattr(bugreport, "send_via_form",
                        lambda *a, **k: (False, "boom"))
    monkeypatch.setattr(bugreport, "open_mailto", lambda *a, **k: False)
    status, detail = bugreport.report("m", True, "", _cfg())
    assert status == "failed"


def test_report_mailto_method_skips_form(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("form must not be used in mailto method")
    monkeypatch.setattr(bugreport, "send_via_form", boom)
    monkeypatch.setattr(bugreport, "open_mailto", lambda *a, **k: True)
    status, detail = bugreport.report("m", True, "", _cfg(bug_report_method="mailto"))
    assert status == "mailto"


def test_report_never_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("explode")
    monkeypatch.setattr(bugreport, "get_diagnostics", boom)
    status, detail = bugreport.report("m", True, "", _cfg())
    assert status == "failed"


# ---------------------------------------------------------------------------
# config coercion
# ---------------------------------------------------------------------------
def test_bug_report_email_default_present():
    assert "bug_report_email" in DEFAULT_CONFIG
    assert DEFAULT_CONFIG["bug_report_email"]
    assert DEFAULT_CONFIG["bug_report_method"] == "form"


def test_coerce_bad_bug_report_method_resets_to_form():
    cfg = dict(DEFAULT_CONFIG)
    cfg["bug_report_method"] = "carrier-pigeon"
    config._coerce_config(cfg)
    assert cfg["bug_report_method"] == "form"


def test_coerce_mailto_method_preserved():
    cfg = dict(DEFAULT_CONFIG)
    cfg["bug_report_method"] = "mailto"
    config._coerce_config(cfg)
    assert cfg["bug_report_method"] == "mailto"


def test_coerce_empty_bug_report_email_resets_to_default():
    cfg = dict(DEFAULT_CONFIG)
    cfg["bug_report_email"] = ""
    config._coerce_config(cfg)
    assert cfg["bug_report_email"] == DEFAULT_CONFIG["bug_report_email"]
