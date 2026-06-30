"""
voiceflow.bugreport - "Report a bug" delivery with no backend server.

The user types a short description in the GUI; we collect a SANITIZED set of
app/system diagnostics (version, OS, python, active model/device/compute, and a
short tail of the log) and deliver the report to the developer's email. Two
delivery paths, tried in order of preference:

  1. ``send_via_form`` - POST JSON to formsubmit.co's AJAX endpoint, which
     forwards it as an email to ``cfg["bug_report_email"]`` with no server of
     our own. Fully silent (no email client needed).
  2. ``open_mailto`` - fall back to the user's email client via a mailto: URL
     pre-filled with the report (they just hit send).

NOTHING here ever raises: every public function is fail-open so a bug report can
never crash the app. We deliberately collect NO audio, NO transcripts, NO
clipboard contents, and NO file contents - only app/system diagnostics plus a
truncated log tail.
"""

from __future__ import annotations

import os
import sys
import json
import logging
import platform
import urllib.parse
import urllib.request
import webbrowser

log = logging.getLogger("voiceflow.bugreport")

# How many trailing lines of the log to include (kept small for privacy + size).
_LOG_TAIL_LINES = 60
# Cap the mailto: body so the URL stays usable across email clients.
_MAILTO_BODY_MAX = 1500
# Network timeout for the form POST (seconds).
_FORM_TIMEOUT = 10


def _app_version():
    """Best-effort app version string ("?" if it can't be determined)."""
    try:
        from voiceflow import __version__
        return str(__version__)
    except Exception:
        return "?"


def _log_tail(n=_LOG_TAIL_LINES):
    """Return the last ``n`` lines of the app log as a single string, or "" if
    the log is missing/unreadable. Best-effort; never raises."""
    try:
        from .constants import LOG_PATH
        if not LOG_PATH or not os.path.exists(LOG_PATH):
            return ""
        with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
        tail = lines[-n:] if len(lines) > n else lines
        return "".join(tail).strip()
    except Exception:
        return ""


def get_diagnostics(cfg):
    """Collect a SANITIZED dict of app + system diagnostics.

    Includes (each guarded individually so a missing piece is simply omitted):
    app version, OS, python version, the active model/device/compute_type, and a
    short tail of the log. NEVER includes audio, transcripts, clipboard, or file
    contents. Never raises - returns whatever it could gather."""
    diag = {}
    cfg = cfg or {}
    try:
        diag["version"] = _app_version()
    except Exception:
        pass
    try:
        diag["os"] = platform.platform()
    except Exception:
        pass
    try:
        diag["python"] = sys.version.split()[0]
    except Exception:
        pass
    try:
        if cfg.get("model"):
            diag["model"] = str(cfg.get("model"))
    except Exception:
        pass
    try:
        if cfg.get("device"):
            diag["device"] = str(cfg.get("device"))
    except Exception:
        pass
    try:
        if cfg.get("compute_type"):
            diag["compute_type"] = str(cfg.get("compute_type"))
    except Exception:
        pass
    try:
        tail = _log_tail()
        if tail:
            diag["log_tail"] = tail
    except Exception:
        pass
    return diag


def format_body(message, diag, reporter_email):
    """Render a readable plain-text report: the user's message, then a
    "--- Diagnostics ---" section listing the diag dict, then the reporter email
    if one was given. Never raises."""
    try:
        parts = []
        parts.append((message or "").strip() or "(no message provided)")
        parts.append("")
        parts.append("--- Diagnostics ---")
        diag = diag or {}
        for key in sorted(diag.keys()):
            val = diag[key]
            if key == "log_tail":
                parts.append("log_tail:")
                parts.append(str(val))
            else:
                parts.append("%s: %s" % (key, val))
        if reporter_email:
            parts.append("")
            parts.append("Reporter email: %s" % reporter_email)
        return "\n".join(parts)
    except Exception as exc:
        # Absolute last resort: still return something deliverable.
        return "%s\n\n(diagnostics unavailable: %s)" % (
            (message or "").strip(), exc)


def send_via_form(message, diag, reporter_email, cfg):
    """POST the report as JSON to formsubmit.co's AJAX endpoint, which emails it
    to ``cfg["bug_report_email"]`` (no backend of our own).

    Returns (True, "sent") only if the HTTP status is 2xx AND the JSON response
    has a truthy ``success``; otherwise (False, <reason>). NEVER raises."""
    try:
        cfg = cfg or {}
        email = (cfg.get("bug_report_email") or "").strip()
        if not email:
            return (False, "no bug_report_email configured")
        endpoint = "https://formsubmit.co/ajax/%s" % urllib.parse.quote(email)
        payload = {
            "_subject": "OpenVerba bug report",
            "message": (message or "").strip(),
            "diagnostics": format_body(message, diag, reporter_email),
            "reporter_email": reporter_email or "(not provided)",
            "app": "OpenVerba",
            "version": _app_version(),
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            endpoint, data=data, method="POST",
            headers={"Content-Type": "application/json",
                     "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=_FORM_TIMEOUT) as resp:
            status = getattr(resp, "status", None)
            if status is None:
                # Older Python: fall back to getcode().
                try:
                    status = resp.getcode()
                except Exception:
                    status = 0
            body = resp.read()
        if not (200 <= int(status) < 300):
            return (False, "HTTP %s" % status)
        try:
            parsed = json.loads(body.decode("utf-8", errors="replace"))
        except Exception:
            return (False, "non-JSON response")
        if isinstance(parsed, dict) and parsed.get("success"):
            return (True, "sent")
        return (False, "form did not confirm success")
    except Exception as exc:
        return (False, str(exc))


def mailto_url(message, diag, reporter_email, cfg):
    """Build a mailto: URL to the configured bug-report email with a url-encoded
    subject + body (the body is the formatted report, capped in length so the URL
    stays usable). Never raises."""
    try:
        cfg = cfg or {}
        email = (cfg.get("bug_report_email") or "").strip()
        subject = "OpenVerba bug report"
        body = format_body(message, diag, reporter_email)
        if len(body) > _MAILTO_BODY_MAX:
            body = body[:_MAILTO_BODY_MAX] + "\n... (truncated)"
        query = urllib.parse.urlencode(
            {"subject": subject, "body": body}, quote_via=urllib.parse.quote)
        return "mailto:%s?%s" % (email, query)
    except Exception:
        # A minimal but valid mailto so the user can still reach us.
        try:
            return "mailto:%s" % ((cfg or {}).get("bug_report_email") or "")
        except Exception:
            return "mailto:"


def open_mailto(message, diag, reporter_email, cfg):
    """Open the user's email client at a pre-filled mailto: URL. Returns True if
    the open succeeded, else False. Never raises."""
    try:
        url = mailto_url(message, diag, reporter_email, cfg)
        return bool(webbrowser.open(url))
    except Exception:
        return False


def report(message, include_diag, reporter_email, cfg):
    """High-level entry point used by the GUI.

    Returns (status, detail) where status is one of:
      "sent"   - delivered silently via the form.
      "mailto" - the form failed (or method is mailto); the email client opened.
      "failed" - nothing worked.

    Honors ``cfg["bug_report_method"]`` ("form" | "mailto"). Always defensive -
    never raises."""
    try:
        cfg = cfg or {}
        diag = get_diagnostics(cfg) if include_diag else {}
        method = cfg.get("bug_report_method", "form")
        if method == "mailto":
            if open_mailto(message, diag, reporter_email, cfg):
                return ("mailto", "opened email client")
            return ("failed", "could not open email client")
        # Default: try the form, fall back to mailto.
        ok, detail = send_via_form(message, diag, reporter_email, cfg)
        if ok:
            return ("sent", detail)
        if open_mailto(message, diag, reporter_email, cfg):
            return ("mailto", "form failed (%s); opened email client" % detail)
        return ("failed", "form failed (%s) and email client did not open"
                % detail)
    except Exception as exc:
        return ("failed", str(exc))
