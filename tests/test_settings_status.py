"""Settings status-decision helpers (B.1): pure functions that turn the async
probe results (recommend_ai / is_available / gpu_runtime_present) into the
text/color/button each card shows -- testable without a Tk display."""

from voiceflow.ui import settings as S


def test_ai_status_ready():
    text, color, show = S._ai_status({"capable": True}, True, "qwen2.5:3b")
    assert "Ready" in text and "qwen2.5:3b" in text
    assert color == "ok" and show is False


def test_ai_status_capable_not_setup_shows_enable():
    text, color, show = S._ai_status({"capable": True}, False, "m")
    assert text == "Not set up yet"
    assert color == "muted" and show is True


def test_ai_status_not_capable_no_enable():
    text, color, show = S._ai_status({"capable": False}, False, "m")
    assert "Not recommended" in text
    assert color == "muted" and show is False


def test_cleanup_status_ready_and_not():
    assert S._cleanup_status(True) == ("Ready", "ok")
    assert S._cleanup_status(False) == ("Not set up", "muted")


def test_gpu_status_installed():
    assert S._gpu_status(True) == ("Installed", "ok", False)


def test_gpu_status_not_installed_shows_enable():
    assert S._gpu_status(False) == ("Not installed", "muted", True)
