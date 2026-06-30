"""
gui.widgets - small reusable, styled customtkinter building blocks used across
the onboarding / dashboard / settings views, plus the app icon image.

Everything here is presentation-only; no engine/hardware imports.
"""

from __future__ import annotations

import customtkinter as ctk

from . import theme as T


# ---------------------------------------------------------------------------
# App icon (drawn in-code with PIL so there is no external asset dependency).
# Returns a PIL.Image; callers convert to PhotoImage / ICO as needed.
# ---------------------------------------------------------------------------
def make_icon_image(size: int = 256):
    """The OpenVerba brand mark (the green-gradient microphone from the website),
    decoded from the embedded base64 logo so it needs no asset file at runtime
    (works in both the source tree and the frozen build). Falls back to a drawn
    mic if decoding ever fails."""
    try:
        import io
        import base64
        from PIL import Image
        from ._logo import LOGO_PNG_B64
        img = Image.open(io.BytesIO(base64.b64decode(LOGO_PNG_B64))).convert("RGBA")
        if img.size != (size, size):
            img = img.resize((size, size), Image.LANCZOS)
        return img
    except Exception:
        return _draw_icon_fallback(size)


def _draw_icon_fallback(size: int = 256):
    """Pure-PIL mic fallback (teal rounded square + white mic glyph) used only if
    the embedded brand logo can't be decoded. No asset/embedded dependency."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = int(size * 0.06)
    radius = int(size * 0.22)

    # Rounded-square background (accent gradient-ish via two stacked rects).
    d.rounded_rectangle([pad, pad, size - pad, size - pad],
                        radius=radius, fill=(20, 184, 166, 255))
    d.rounded_rectangle([pad, pad, size - pad, int(size * 0.62)],
                        radius=radius, fill=(45, 212, 191, 255))

    # Microphone capsule.
    cx = size / 2
    cap_w = size * 0.20
    cap_top = size * 0.22
    cap_bot = size * 0.55
    white = (255, 255, 255, 255)
    d.rounded_rectangle([cx - cap_w / 2, cap_top, cx + cap_w / 2, cap_bot],
                        radius=int(cap_w / 2), fill=white)
    # Mic arc (the U cradle).
    arc_w = size * 0.34
    line = max(3, int(size * 0.035))
    d.arc([cx - arc_w / 2, cap_top + size * 0.04,
           cx + arc_w / 2, cap_bot + size * 0.06],
          start=20, end=160, fill=white, width=line)
    # Stand + base.
    d.line([cx, cap_bot + size * 0.06, cx, size * 0.74], fill=white, width=line)
    d.line([cx - size * 0.10, size * 0.74, cx + size * 0.10, size * 0.74],
           fill=white, width=line)
    return img


def make_ctk_icon(size: int = 64):
    """A CTkImage of the app mark, for in-window headers."""
    img = make_icon_image(size * 2)
    return ctk.CTkImage(light_image=img, dark_image=img, size=(size, size))


# ---------------------------------------------------------------------------
# Card: a rounded surface container with optional title.
# ---------------------------------------------------------------------------
class Card(ctk.CTkFrame):
    def __init__(self, master, title: str | None = None, **kw):
        kw.setdefault("fg_color", T.SURFACE)
        kw.setdefault("corner_radius", T.RADIUS)
        kw.setdefault("border_width", 1)
        kw.setdefault("border_color", T.BORDER)
        super().__init__(master, **kw)
        self._row = 0
        if title:
            lbl = ctk.CTkLabel(self, text=title, font=T.font("h2"),
                               text_color=T.TEXT, anchor="w")
            lbl.grid(row=self._row, column=0, sticky="w",
                     padx=18, pady=(16, 8))
            self.grid_columnconfigure(0, weight=1)
            self._row += 1


# ---------------------------------------------------------------------------
# Badge: a small rounded pill (used for tiers, "installed", device, etc).
# ---------------------------------------------------------------------------
class Badge(ctk.CTkLabel):
    def __init__(self, master, text: str, color: str = T.ACCENT,
                 text_color: str = "#06201d", **kw):
        kw.setdefault("font", T.font("small_bold"))
        kw.setdefault("corner_radius", 10)
        kw.setdefault("fg_color", color)
        kw.setdefault("text_color", text_color)
        kw.setdefault("padx", 10)
        kw.setdefault("pady", 3)
        super().__init__(master, text=text, **kw)


# ---------------------------------------------------------------------------
# Accent / secondary / ghost / danger buttons (consistent styling).
# ---------------------------------------------------------------------------
def accent_button(master, text, command=None, **kw):
    kw.setdefault("font", T.font("body_bold"))
    kw.setdefault("corner_radius", T.RADIUS_SM)
    kw.setdefault("fg_color", T.ACCENT)
    kw.setdefault("hover_color", T.ACCENT_DARK)
    kw.setdefault("text_color", "#06201d")
    kw.setdefault("height", 40)
    return ctk.CTkButton(master, text=text, command=command, **kw)


def secondary_button(master, text, command=None, **kw):
    kw.setdefault("font", T.font("body_bold"))
    kw.setdefault("corner_radius", T.RADIUS_SM)
    kw.setdefault("fg_color", T.SURFACE_2)
    kw.setdefault("hover_color", T.SURFACE_3)
    kw.setdefault("text_color", T.TEXT)
    kw.setdefault("border_width", 1)
    kw.setdefault("border_color", T.BORDER)
    kw.setdefault("height", 40)
    return ctk.CTkButton(master, text=text, command=command, **kw)


def ghost_button(master, text, command=None, **kw):
    kw.setdefault("font", T.font("body"))
    kw.setdefault("corner_radius", T.RADIUS_SM)
    kw.setdefault("fg_color", "transparent")
    kw.setdefault("hover_color", T.SURFACE_2)
    kw.setdefault("text_color", T.TEXT_MUTED)
    kw.setdefault("height", 32)
    return ctk.CTkButton(master, text=text, command=command, **kw)


def danger_button(master, text, command=None, **kw):
    kw.setdefault("font", T.font("body_bold"))
    kw.setdefault("corner_radius", T.RADIUS_SM)
    kw.setdefault("fg_color", "transparent")
    kw.setdefault("hover_color", "#3a1f23")
    kw.setdefault("text_color", T.DANGER)
    kw.setdefault("border_width", 1)
    kw.setdefault("border_color", "#5a2a2e")
    kw.setdefault("height", 36)
    return ctk.CTkButton(master, text=text, command=command, **kw)


# ---------------------------------------------------------------------------
# Star/dot rating row (quality & speed). Filled vs empty dots.
# ---------------------------------------------------------------------------
class DotRating(ctk.CTkFrame):
    def __init__(self, master, value: int, total: int = 5,
                 color: str = T.ACCENT, **kw):
        kw.setdefault("fg_color", "transparent")
        super().__init__(master, **kw)
        value = max(0, min(total, int(value or 0)))
        for i in range(total):
            on = i < value
            ctk.CTkLabel(
                self, text="●",  # filled circle
                font=T.font("small"),
                text_color=color if on else T.SURFACE_3,
            ).grid(row=0, column=i, padx=1)


# ---------------------------------------------------------------------------
# Labeled stat (e.g. "GPU:  RTX 3050 (4 GB)") used in the hardware card.
# ---------------------------------------------------------------------------
class StatRow(ctk.CTkFrame):
    def __init__(self, master, label: str, value: str,
                 value_color: str = T.TEXT, icon: str | None = None, **kw):
        kw.setdefault("fg_color", "transparent")
        super().__init__(master, **kw)
        self.grid_columnconfigure(1, weight=1)
        head = label if not icon else f"{icon}  {label}"
        ctk.CTkLabel(self, text=head, font=T.font("body"),
                     text_color=T.TEXT_MUTED, anchor="w", width=120).grid(
            row=0, column=0, sticky="w", padx=(0, 12))
        self.value_lbl = ctk.CTkLabel(self, text=value, font=T.font("body_bold"),
                                      text_color=value_color, anchor="w",
                                      justify="left")
        self.value_lbl.grid(row=0, column=1, sticky="w")

    def set_value(self, value, color=None):
        self.value_lbl.configure(text=value)
        if color:
            self.value_lbl.configure(text_color=color)


# ---------------------------------------------------------------------------
# A horizontal mic VU level meter (segmented bar).
# ---------------------------------------------------------------------------
class LevelMeter(ctk.CTkFrame):
    def __init__(self, master, segments: int = 24, **kw):
        kw.setdefault("fg_color", "transparent")
        super().__init__(master, **kw)
        self.segments = segments
        self._cells = []
        for i in range(segments):
            c = ctk.CTkFrame(self, width=8, height=20, corner_radius=2,
                             fg_color=T.SURFACE_3)
            c.grid(row=0, column=i, padx=1)
            c.grid_propagate(False)
            self._cells.append(c)

    def set_level(self, level: float):
        """level 0..1 -> light up that fraction of segments with a green->amber
        ->red gradient near the top."""
        level = max(0.0, min(1.0, float(level or 0.0)))
        lit = int(round(level * self.segments))
        for i, c in enumerate(self._cells):
            if i < lit:
                frac = i / max(1, self.segments - 1)
                if frac < 0.6:
                    col = T.OK
                elif frac < 0.85:
                    col = T.WARN
                else:
                    col = T.DANGER
                c.configure(fg_color=col)
            else:
                c.configure(fg_color=T.SURFACE_3)


# ---------------------------------------------------------------------------
# Section heading (used in scrollable settings).
# ---------------------------------------------------------------------------
def section_label(master, text):
    return ctk.CTkLabel(master, text=text, font=T.font("h2"),
                        text_color=T.TEXT, anchor="w")


def hint_label(master, text, color=T.TEXT_FAINT, **kw):
    kw.setdefault("font", T.font("small"))
    kw.setdefault("text_color", color)
    kw.setdefault("anchor", "w")
    kw.setdefault("justify", "left")
    return ctk.CTkLabel(master, text=text, **kw)
