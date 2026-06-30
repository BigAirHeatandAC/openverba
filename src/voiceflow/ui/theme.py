"""
gui.theme - the VoiceFlow look: dark palette, one accent color, fonts, and a few
shared layout constants. Importing this module configures customtkinter's global
appearance (dark mode) so every window/widget is consistent.

The palette is intentional (not default grey): a deep slate background with a
single calm teal/blue accent, rounded corners, and soft surface cards.
"""

from __future__ import annotations

import customtkinter as ctk

# ---------------------------------------------------------------------------
# Global appearance. Set once at import.
# ---------------------------------------------------------------------------
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")

# ---------------------------------------------------------------------------
# Color palette (hex). One accent color (teal/blue), everything else neutral.
# ---------------------------------------------------------------------------
BG          = "#0f1419"   # window background (deep slate)
SURFACE     = "#171d26"   # card / panel surface
SURFACE_2   = "#1f2731"   # raised surface (rows, inputs)
SURFACE_3   = "#2a3340"   # hover / selected surface
BORDER      = "#2b333f"   # subtle separators / card borders

ACCENT      = "#2dd4bf"   # primary accent (teal)
ACCENT_DARK = "#14b8a6"   # accent hover/pressed
ACCENT_SOFT = "#0e3b38"   # accent-tinted surface (badges, selected)

TEXT        = "#e8edf2"   # primary text
TEXT_MUTED  = "#9aa6b2"   # secondary text
TEXT_FAINT  = "#5f6b78"   # tertiary / hints

OK          = "#34d399"   # success / idle-ready green
WARN        = "#f59e0b"   # warning amber
DANGER      = "#ef4444"   # error / recording red
INFO        = "#38bdf8"   # informational blue

# State indicator colors (dashboard status dot).
STATE_COLORS = {
    "idle":          ("#39424f", "Idle"),
    "recording":     (DANGER,    "Recording"),
    "transcribing":  (WARN,      "Transcribing"),
}

# Rounded-corner radius used across the app.
RADIUS = 12
RADIUS_SM = 8

# ---------------------------------------------------------------------------
# Fonts. Built lazily (a Tk root must exist before CTkFont is created).
# ---------------------------------------------------------------------------
_FONTS: dict = {}


def font(role: str = "body"):
    """Return a cached CTkFont for a named role. Roles:
    title / h1 / h2 / h3 / body / body_bold / small / small_bold / mono / huge."""
    if role in _FONTS:
        return _FONTS[role]
    family = "Segoe UI"
    spec = {
        "huge":       (family, 40, "bold"),
        "title":      (family, 26, "bold"),
        "h1":         (family, 22, "bold"),
        "h2":         (family, 17, "bold"),
        "h3":         (family, 14, "bold"),
        "body":       (family, 13, "normal"),
        "body_bold":  (family, 13, "bold"),
        "small":      (family, 11, "normal"),
        "small_bold": (family, 11, "bold"),
        "mono":       ("Consolas", 12, "normal"),
    }.get(role, (family, 13, "normal"))
    f = ctk.CTkFont(family=spec[0], size=spec[1],
                    weight="bold" if spec[2] == "bold" else "normal")
    _FONTS[role] = f
    return f


def tier_color(tier: str) -> str:
    """Accent color for a recommendation tier badge."""
    return {
        "recommended": ACCENT,
        "max":         INFO,
        "light":       TEXT_MUTED,
    }.get(tier, TEXT_MUTED)


def tier_label(tier: str) -> str:
    return {
        "recommended": "Recommended for your PC",
        "max":         "Max accuracy",
        "light":       "Lightest / fastest",
    }.get(tier, tier)
