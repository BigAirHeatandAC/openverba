"""
gui.history_view - the "History" view: a list of past transcripts, an editor
with LIVE red highlighting of your edits vs. the original, "Reset to original",
and "Save" (which persists the edit + learns from it).

Owner's spec: "see a history of your transcripts, go in and edit them to change
what was wrong, and it uses that info to create words it recognizes somehow;
anything you edit shows up in RED and you can also reset it to the original."

Two modes in one view (no second window): a scrollable LIST (newest first) and
an EDITOR. The diff for the red highlight is computed on CASED tokens (so a
capitalization edit shows red and gets learned); learn.derive infers the rule's
case mode separately.
"""

from __future__ import annotations

import re
import time
import difflib

import customtkinter as ctk

from . import theme as T
from . import widgets as W

from voiceflow import history as vf_history
from voiceflow import learn as vf_learn

# How many history rows to render up front, and how many more per "Load more".
# Building each row is ~6 widgets + bindings; rendering all ~300+ records up front
# blocks the Tk thread for a noticeable beat, so we page.
INITIAL_ROWS = 40
LOAD_MORE_STEP = 40


def _page_bounds(total, shown, step):
    """Pure paging helper: given the total record count, how many are already
    shown, and the page step, return (start, end, remaining_after) for the next
    page. ``start..end`` is the slice to render next; ``remaining_after`` is how
    many are still unrendered once this page is added (0 = all shown)."""
    start = max(0, shown)
    end = min(total, start + max(0, step))
    remaining = max(0, total - end)
    return start, end, remaining


def _line_starts(s):
    """Character offsets of the start of each line in ``s`` (offset 0 plus one
    past every '\\n'). Used to turn a flat Python char offset into a Tk
    ``line.col`` index without any per-token widget round-trips."""
    starts = [0]
    for i, ch in enumerate(s):
        if ch == "\n":
            starts.append(i + 1)
    return starts


def _flat_to_index(line_starts, off):
    """Map a flat character offset into a Tk text index string ``"line.col"``
    (1-based line, 0-based col), using precomputed line-start offsets. Pure
    Python (bisect), so it makes ZERO Tcl ``.index()`` calls -- this is the fix
    for the O(n^2) recolor AND for the multi-line offset drift (the flat offset
    and the index now share the same string basis)."""
    import bisect
    line = bisect.bisect_right(line_starts, off) - 1
    if line < 0:
        line = 0
    col = off - line_starts[line]
    return "%d.%d" % (line + 1, col)


def _build_change_spans(orig, cur):
    """Compute the Tk index spans to highlight as "changed" when editing ``cur``
    against the baseline ``orig``. Returns a list of ``(start_index, end_index)``
    ``"line.col"`` string pairs. Pure (no Tk): the live editor calls this, and
    tests can exercise the offset->index mapping directly.

    Diff is on CASED whitespace tokens so capitalization edits (e.g.
    'big air' -> 'Big Air') highlight and get learned; learn.derive infers the
    case mode separately. The flat ``re.finditer`` offsets are mapped to Tk
    indices via the SAME ``cur`` string (line_starts), eliminating the
    newline/wrapped-text drift that painted red over text the user never changed.
    """
    orig_words = (orig or "").split()
    cur_tokens = [(m.group(), m.start(), m.end())
                  for m in re.finditer(r"\S+", cur or "")]
    line_starts = _line_starts(cur or "")
    sm = difflib.SequenceMatcher(
        a=orig_words, b=[t[0] for t in cur_tokens], autojunk=False)
    spans = []
    for tag, _i1, _i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        # 'replace'/'insert' -> current-side tokens j1..j2 are changed;
        # 'delete' has no current span, so nothing to color.
        for k in range(j1, j2):
            _, start, end = cur_tokens[k]
            spans.append((_flat_to_index(line_starts, start),
                          _flat_to_index(line_starts, end)))
    return spans


def _relative_time(ts):
    """A short 'just now' / '2m ago' / '3h ago' / '5d ago' string from epoch."""
    try:
        delta = max(0.0, time.time() - float(ts))
    except Exception:
        return ""
    if delta < 45:
        return "just now"
    if delta < 3600:
        return "%dm ago" % int(delta // 60)
    if delta < 86400:
        return "%dh ago" % int(delta // 3600)
    if delta < 7 * 86400:
        return "%dd ago" % int(delta // 86400)
    try:
        return time.strftime("%b %d", time.localtime(float(ts)))
    except Exception:
        return ""


class HistoryView(ctk.CTkFrame):
    def __init__(self, master, app, **kw):
        kw.setdefault("fg_color", T.BG)
        super().__init__(master, **kw)
        self.app = app

        # Editor state.
        self._rec = None        # the record being edited
        self._orig = ""         # immutable diff baseline (record["original"])
        self._editor = None     # the CTkTextbox in editor mode
        self._recolor_job = None  # pending debounced recolor (after id)
        # List paging state.
        self._all_records = []  # full loaded record list (newest first)
        self._shown = 0         # how many list rows are currently rendered
        self._more_row = None   # the "Load more" footer frame (if any)

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        self._build_shell()
        self._show_list()

    # ------------------------------------------------------------- shell
    def _build_shell(self):
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=24, pady=(20, 6))
        top.grid_columnconfigure(1, weight=1)
        W.ghost_button(top, "←  Back", command=self.app.show_dashboard,
                       width=90).grid(row=0, column=0)
        self._title = ctk.CTkLabel(top, text="History", font=T.font("h1"),
                                   text_color=T.TEXT)
        self._title.grid(row=0, column=1, sticky="w", padx=12)

        self._scroll = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self._scroll.grid(row=1, column=0, sticky="nsew", padx=18, pady=(2, 16))
        self._scroll.grid_columnconfigure(0, weight=1)

        self._toast = ctk.CTkLabel(self, text="", font=T.font("small_bold"),
                                   text_color=T.ACCENT, anchor="w")
        self._toast.grid(row=2, column=0, sticky="ew", padx=26, pady=(0, 8))

    def _clear_scroll(self):
        for w in self._scroll.winfo_children():
            try:
                w.destroy()
            except Exception:
                pass

    # ------------------------------------------------------------- list mode
    def _show_list(self):
        self._rec = None
        self._orig = ""
        self._editor = None
        self._recolor_job = None
        self._title.configure(text="History")
        self._clear_scroll()
        self._more_row = None
        self._shown = 0
        try:
            self._all_records = vf_history.load(limit=500)
        except Exception:
            self._all_records = []
        if not self._all_records:
            W.hint_label(
                self._scroll,
                "No transcripts yet. Dictate something and it'll show up here.",
                color=T.TEXT_MUTED, wraplength=620).grid(
                row=0, column=0, sticky="w", padx=8, pady=16)
            return
        # Render only the newest INITIAL_ROWS; the rest load on demand. Loading
        # the JSONL is cheap -- it's building the row widgets that's slow.
        self._render_more(INITIAL_ROWS)

    def _render_more(self, step):
        """Render the next ``step`` list rows (append; never rebuild existing
        rows) and refresh the 'Load more' footer."""
        total = len(self._all_records)
        start, end, remaining = _page_bounds(total, self._shown, step)
        for i in range(start, end):
            self._list_row(self._all_records[i]).grid(
                row=i, column=0, sticky="ew", padx=4, pady=4)
        self._shown = end
        self._update_more_row(total, remaining)

    def _update_more_row(self, total, remaining):
        # Drop any existing footer first so it always sits below the last row.
        if self._more_row is not None:
            try:
                self._more_row.destroy()
            except Exception:
                pass
            self._more_row = None
        if remaining <= 0:
            return
        foot = ctk.CTkFrame(self._scroll, fg_color="transparent")
        foot.grid(row=self._shown, column=0, sticky="ew", padx=4, pady=(6, 4))
        foot.grid_columnconfigure(1, weight=1)
        W.secondary_button(
            foot, "Load more", width=130,
            command=lambda: self._render_more(LOAD_MORE_STEP)).grid(
            row=0, column=0, sticky="w")
        ctk.CTkLabel(foot, text="Showing %d of %d" % (self._shown, total),
                     font=T.font("small"), text_color=T.TEXT_MUTED,
                     anchor="w").grid(row=0, column=1, sticky="w", padx=(12, 0))
        self._more_row = foot

    def _list_row(self, rec):
        row = ctk.CTkFrame(self._scroll, fg_color=T.SURFACE_2,
                           corner_radius=T.RADIUS_SM, border_width=1,
                           border_color=T.BORDER)
        row.grid_columnconfigure(0, weight=1)

        head = ctk.CTkFrame(row, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=14, pady=(8, 0))
        head.grid_columnconfigure(2, weight=1)
        ctk.CTkLabel(head, text=_relative_time(rec.get("ts")),
                     font=T.font("small"), text_color=T.TEXT_FAINT,
                     anchor="w").grid(row=0, column=0, sticky="w")
        W.Badge(head, str(rec.get("mode") or "dictation"),
                color=T.SURFACE_3, text_color=T.TEXT_MUTED).grid(
            row=0, column=1, padx=(8, 0))
        if rec.get("edited") is not None:
            W.Badge(head, "Edited", color=T.WARN,
                    text_color="#231a00").grid(row=0, column=2, sticky="w",
                                               padx=(8, 0))

        preview = (rec.get("edited") or rec.get("original") or "").strip()
        if len(preview) > 90:
            preview = preview[:90] + "…"
        W.hint_label(row, preview or "(empty)", color=T.TEXT,
                     wraplength=640).grid(row=1, column=0, sticky="w",
                                          padx=14, pady=(2, 10))

        # Whole row clickable -> open editor.
        def _open(_e=None, r=rec):
            self._open_editor(r)
        for widget in (row, head, *head.winfo_children()):
            try:
                widget.bind("<Button-1>", _open)
            except Exception:
                pass
        for child in row.winfo_children():
            try:
                child.bind("<Button-1>", _open)
            except Exception:
                pass
        return row

    # ----------------------------------------------------------- editor mode
    def _open_editor(self, rec):
        self._rec = rec
        self._orig = str(rec.get("original") or "")
        self._title.configure(text="Edit transcript")
        self._clear_scroll()

        header = ctk.CTkFrame(self._scroll, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=8, pady=(2, 6))
        header.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(header, text=_relative_time(rec.get("ts")),
                     font=T.font("small"), text_color=T.TEXT_FAINT).grid(
            row=0, column=0, sticky="w")
        W.Badge(header, str(rec.get("mode") or "dictation"),
                color=T.SURFACE_3, text_color=T.TEXT_MUTED).grid(
            row=0, column=1, sticky="w", padx=(8, 0))

        W.hint_label(
            self._scroll,
            "Fix anything that was misheard. Your changes show in red. "
            "Save to teach OpenVerba; Reset restores the original.",
            color=T.TEXT_MUTED, wraplength=640).grid(
            row=1, column=0, sticky="w", padx=8, pady=(0, 8))

        self._editor = ctk.CTkTextbox(
            self._scroll, font=T.font("body"), fg_color=T.SURFACE_2,
            text_color=T.TEXT, border_width=1, border_color=T.BORDER,
            corner_radius=T.RADIUS_SM, wrap="word", height=200)
        self._editor.grid(row=2, column=0, sticky="ew", padx=8, pady=(0, 10))
        self._editor.insert("1.0", str(rec.get("edited") or rec.get("original")
                                       or ""))
        try:
            self._editor.tag_config("changed", foreground=T.DANGER)
        except Exception:
            pass
        try:
            self._editor.bind("<KeyRelease>", self._recolor)
            self._editor.bind("<<Paste>>", self._recolor)
        except Exception:
            pass
        self._do_recolor()

        btns = ctk.CTkFrame(self._scroll, fg_color="transparent")
        btns.grid(row=3, column=0, sticky="ew", padx=8, pady=(2, 8))
        W.secondary_button(btns, "Reset to original", width=170,
                           command=self._reset).grid(row=0, column=0, padx=(0, 8))
        W.accent_button(btns, "Save", width=120,
                        command=self._save).grid(row=0, column=1, padx=(0, 8))
        W.ghost_button(btns, "←  Back to list", width=130,
                       command=self._show_list).grid(row=0, column=2)

    # ----------------------------------------------------- live red diff
    def _recolor(self, event=None):
        """KeyRelease/Paste handler: DEBOUNCE the recolor. A burst of keystrokes
        collapses into a single _do_recolor() ~120ms after typing stops, instead
        of running the diff+tagging on every key (which stuttered on long text)."""
        if self._editor is None:
            return
        try:
            if self._recolor_job is not None:
                self._editor.after_cancel(self._recolor_job)
        except Exception:
            pass
        try:
            self._recolor_job = self._editor.after(120, self._do_recolor)
        except Exception:
            # If scheduling fails, just recolor inline (still correct).
            self._do_recolor()

    def _do_recolor(self):
        """Recompute and apply the red "changed" tags. Clears all existing tags
        first (no accumulation), then tags the correct spans computed purely from
        the editor's current text -- no per-token Tk .index() round-trips, and
        newline-correct (fixes the phantom-highlight glitch on multi-line text)."""
        self._recolor_job = None
        if self._editor is None:
            return
        try:
            self._editor.tag_remove("changed", "1.0", "end")
            cur = self._editor.get("1.0", "end-1c")
            for start_idx, end_idx in _build_change_spans(self._orig, cur):
                self._editor.tag_add("changed", start_idx, end_idx)
        except Exception:
            pass

    # --------------------------------------------------------- reset / save
    def _reset(self):
        if self._editor is None:
            return
        try:
            self._editor.delete("1.0", "end")
            self._editor.insert("1.0", self._orig)
            self._do_recolor()
            self._toast_msg("Reset to the original transcript.", T.TEXT_MUTED)
        except Exception:
            pass

    def _save(self):
        if self._editor is None or self._rec is None:
            return
        try:
            edited = self._editor.get("1.0", "end-1c").strip()
        except Exception:
            return
        try:
            vf_history.update_edit(self._rec.get("id"), edited)
        except Exception:
            pass
        if edited and edited != self._orig:
            try:
                result = vf_learn.learn(self._orig, edited)
            except Exception:
                result = {"corrections": [], "terms": []}
            try:
                self.app.on_learned()   # engine reloads vocab + rules live
            except Exception:
                pass
            self._toast_msg(self._learn_summary(result), T.OK)
        else:
            self._toast_msg("Saved (no changes to learn).", T.OK)
        self._show_list()

    def _learn_summary(self, result):
        rules = (result or {}).get("corrections") or []
        terms = (result or {}).get("terms") or []
        if not rules and not terms:
            return "Saved your edit. (Nothing specific to learn from this one.)"
        if len(rules) == 1:
            r = rules[0]
            if r.get("case") == "force":
                return ("Learned: '%s' — OpenVerba will spell it right next time."
                        % r.get("to"))
            return "Learned: '%s' → '%s'." % (r.get("from"), r.get("to"))
        if rules:
            return "Learned %d corrections." % len(rules)
        if len(terms) == 1:
            return ("Learned: '%s' — OpenVerba will recognize it better."
                    % terms[0])
        return "Learned %d new words." % len(terms)

    # --------------------------------------------------------------- helpers
    def _toast_msg(self, text, color=T.ACCENT):
        self._toast.configure(text=text, text_color=color)
