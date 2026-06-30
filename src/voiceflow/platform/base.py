"""
voiceflow.platform.base - the platform-abstraction interfaces (ABCs).

A thin runtime-selected abstraction is the architectural backbone of the
cross-platform port. Wayland vs X11 (different hotkey/paste mechanisms) and
macOS TCC permissions make a single cross-platform library impossible, so each
OS supplies a module implementing these ABCs. The engine talks ONLY to these
interfaces (obtained from :func:`voiceflow.platform.make_backends`), never to an
OS-specific module directly.

The signatures here are fixed (see docs/PRODUCTION_PLAN.md section 2.2).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable


class HotkeyBackend(ABC):
    """Global keyboard-combo trigger registration."""

    @abstractmethod
    def register(self, combo: str, on_press: Callable[[], None],
                 on_release: Callable[[], None] | None = None) -> None:
        ...

    @abstractmethod
    def start(self) -> None:
        ...

    @abstractmethod
    def stop(self) -> None:
        ...

    @property
    @abstractmethod
    def supports_hold_mode(self) -> bool:  # push-to-talk reliable?
        ...


class MouseBackend(ABC):
    """Global mouse-button trigger registration (incl. side buttons / chords)."""

    supports_side_buttons: bool = False

    @abstractmethod
    def register(self, button: str, on_press: Callable[[], None],
                 on_release: Callable[[], None] | None = None) -> None:
        ...

    @abstractmethod
    def start(self) -> None:
        ...

    @abstractmethod
    def stop(self) -> None:
        ...


class ClipboardBackend(ABC):
    """Format-aware clipboard snapshot/restore + set-text."""

    @abstractmethod
    def snapshot(self) -> dict[str, bytes | str]:
        """{fmt/MIME: data} best-effort, eager formats only."""
        ...

    @abstractmethod
    def restore(self, snap: dict[str, bytes | str]) -> None:
        ...

    @abstractmethod
    def set_text(self, text: str) -> None:
        ...


class Paster(ABC):
    """Synthesizes the paste keystroke (Ctrl/Cmd+V, configurable chord)."""

    @abstractmethod
    def paste(self) -> None:
        ...

    @abstractmethod
    def set_chord(self, chord: str) -> None:  # e.g. "ctrl+v", "cmd+v", "shift+insert"
        ...


class Typer(ABC):
    """Incremental text insertion: types characters directly into the focused
    window (used by STREAMING mode, where confirmed words are appended live).

    Unlike :class:`Paster` (which sets the clipboard and synthesizes a paste
    chord), a Typer synthesizes the characters themselves and never touches the
    clipboard -- so streaming mode leaves the user's clipboard untouched. It must
    only ever emit printable characters/spaces, NEVER Enter/newline (a stray
    newline would submit a chat box or run a terminal command)."""

    @abstractmethod
    def type_text(self, text: str) -> None:
        """Type ``text`` into the currently focused window (no trailing Enter)."""
        ...

    @abstractmethod
    def press_keys(self, spec: str, count: int = 1) -> None:
        """Press a key or chord ``count`` times -- e.g. "backspace", "enter",
        "delete", "ctrl+a", "ctrl+backspace", "shift+end". Used by VOICE
        COMMANDS (this is the one place Enter is allowed, on explicit request)."""
        ...

    @property
    @abstractmethod
    def supports_incremental(self) -> bool:
        """True if this backend can type small chunks live with low latency."""
        ...


class Permissions(ABC):
    """OS permission state (macOS TCC, Linux /dev/input, etc.)."""

    @abstractmethod
    def check(self) -> dict[str, bool]:
        """{"accessibility": bool, "input_monitoring": bool, "mic": bool}"""
        ...

    @abstractmethod
    def request(self, name: str) -> None:
        """Open the right OS settings pane / trigger the prompt."""
        ...

    @abstractmethod
    def all_ok(self) -> bool:
        ...


# ---------------------------------------------------------------------------
# TriggerBackend (VoiceFlow extension to the plan).
#
# VoiceFlow's real trigger model is richer than a plain hotkey: it accepts
# keyboard combos AND single mouse buttons (middle/x1/x2) AND the left+right
# mouse chord, each with platform-specific suppression/hold-and-forward
# behaviour. Rather than force the engine to juggle a HotkeyBackend AND a
# MouseBackend AND classify which one a trigger string needs, each platform
# also exposes a single TriggerBackend that registers ANY trigger string and
# returns a stoppable handle. The engine uses ONLY this (one stable trigger
# API), as required by the task. HotkeyBackend / MouseBackend remain available
# (and the Windows backend implements them) for callers that want the
# narrower interfaces.
# ---------------------------------------------------------------------------
class TriggerHandle(ABC):
    """A uniform 'stop me' wrapper over a registered trigger."""

    kind: str  # "keyboard" | "mouse" | "chord"

    @abstractmethod
    def stop(self) -> None:
        ...


class TriggerBackend(ABC):
    """Register any VoiceFlow trigger string (keyboard combo, mouse button, or
    the left+right chord) behind one stable API."""

    @abstractmethod
    def register(self, trigger: str,
                 callback: Callable[[], None]) -> TriggerHandle | None:
        """Install the global trigger; ``callback()`` fires on each activation.
        Returns a handle whose ``.stop()`` removes it, or None on failure."""
        ...

    @abstractmethod
    def classify(self, trigger: str) -> dict:
        """{"trigger","label","clean","warning"} describing a trigger string."""
        ...

    @property
    @abstractmethod
    def presets(self) -> list[dict]:
        """Clean, conflict-free suggested triggers for the GUI picker."""
        ...
