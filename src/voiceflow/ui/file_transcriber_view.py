"""
gui.file_transcriber_view - the "Transcribe file" view.

Pick one or more audio/video files, transcribe them with the already-loaded
faster-whisper model (read-only -- the live dictation engine state is never
touched), preview the result, and save/copy it as plain text, SRT, or WebVTT.

All heavy work runs on a daemon thread; UI updates marshal back via
``app.schedule`` (the same pattern the engine's callbacks use). Every failure
degrades to an inline toast -- the app never crashes and the dictation/paste
path is never blocked.

The GUI is intentionally thin: the formatting + transcription logic lives in the
pure, headless-testable :mod:`voiceflow.file_transcribe` module.
"""

from __future__ import annotations

import os
import threading
from tkinter import filedialog

import customtkinter as ctk

from . import theme as T
from . import widgets as W

from voiceflow import file_transcribe as FT


# Output format radio options: (key, display label).
_FORMAT_OPTIONS = [
    ("text", "Text (.txt)"),
    ("srt", "SRT subtitle (.srt)"),
    ("vtt", "WebVTT (.vtt)"),
]


class FileTranscriberView(ctk.CTkFrame):
    def __init__(self, master, app, **kw):
        kw.setdefault("fg_color", T.BG)
        super().__init__(master, **kw)
        self.app = app

        # ---- state ----
        self._files = []                 # selected file paths (order preserved)
        self._format_var = ctk.StringVar(value="text")
        self._busy = False               # a batch is running
        self._cancel = False             # cooperative cancel flag (worker polls)
        # Results: {path: {"segments": [...], "error": str|None}}.
        self._results = {}

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        self._build_shell()
        self._show_input()

    # =================================================================== shell
    def _build_shell(self):
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=24, pady=(20, 6))
        top.grid_columnconfigure(1, weight=1)
        W.ghost_button(top, "←  Back", command=self._on_back,
                       width=90).grid(row=0, column=0)
        self._title = ctk.CTkLabel(top, text="Transcribe a file",
                                   font=T.font("h1"), text_color=T.TEXT)
        self._title.grid(row=0, column=1, sticky="w", padx=12)

        self._scroll = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self._scroll.grid(row=1, column=0, sticky="nsew", padx=18, pady=(2, 8))
        self._scroll.grid_columnconfigure(0, weight=1)

        self._toast = ctk.CTkLabel(self, text="", font=T.font("small_bold"),
                                   text_color=T.ACCENT, anchor="w",
                                   wraplength=860, justify="left")
        self._toast.grid(row=2, column=0, sticky="ew", padx=26, pady=(0, 8))

    def _clear_scroll(self):
        for w in self._scroll.winfo_children():
            try:
                w.destroy()
            except Exception:
                pass

    # =============================================================== input view
    def _show_input(self):
        self._busy = False
        self._cancel = False
        self._results = {}
        self._title.configure(text="Transcribe a file")
        self._clear_scroll()

        W.hint_label(
            self._scroll,
            "Pick audio or video files and OpenVerba will transcribe them "
            "locally with your loaded model. Export as plain text or as SRT / "
            "WebVTT subtitles.",
            color=T.TEXT_MUTED, wraplength=820).grid(
            row=0, column=0, sticky="w", padx=8, pady=(2, 10))

        # ---- file picker + selected list ----
        pick_card = W.Card(self._scroll, title="Files")
        pick_card.grid(row=1, column=0, sticky="ew", padx=4, pady=(0, 12))
        pick_card.grid_columnconfigure(0, weight=1)
        W.accent_button(pick_card, "Select files…", width=150,
                        command=self._pick_files).grid(
            row=1, column=0, sticky="w", padx=18, pady=(0, 8))
        self._file_list = ctk.CTkFrame(pick_card, fg_color="transparent")
        self._file_list.grid(row=2, column=0, sticky="ew", padx=18,
                             pady=(0, 14))
        self._file_list.grid_columnconfigure(0, weight=1)
        self._render_file_list()

        # ---- output format ----
        fmt_card = W.Card(self._scroll, title="Output format")
        fmt_card.grid(row=2, column=0, sticky="ew", padx=4, pady=(0, 12))
        fmt_card.grid_columnconfigure(0, weight=1)
        frow = ctk.CTkFrame(fmt_card, fg_color="transparent")
        frow.grid(row=1, column=0, sticky="w", padx=18, pady=(0, 14))
        for i, (key, label) in enumerate(_FORMAT_OPTIONS):
            ctk.CTkRadioButton(
                frow, text=label, value=key, variable=self._format_var,
                font=T.font("body"), text_color=T.TEXT,
                fg_color=T.ACCENT, hover_color=T.ACCENT_DARK).grid(
                row=0, column=i, sticky="w", padx=(0, 22))

        # ---- transcribe button ----
        self._transcribe_btn = W.accent_button(
            self._scroll, "Transcribe", width=160, command=self._start_batch)
        self._transcribe_btn.grid(row=3, column=0, sticky="w", padx=8,
                                  pady=(2, 10))
        self._sync_transcribe_enabled()

    def _render_file_list(self):
        for w in self._file_list.winfo_children():
            try:
                w.destroy()
            except Exception:
                pass
        if not self._files:
            W.hint_label(self._file_list, "No files selected yet.",
                         color=T.TEXT_FAINT).grid(row=0, column=0, sticky="w")
            return
        for i, path in enumerate(self._files):
            row = ctk.CTkFrame(self._file_list, fg_color=T.SURFACE_2,
                               corner_radius=T.RADIUS_SM)
            row.grid(row=i, column=0, sticky="ew", pady=2)
            row.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(row, text=os.path.basename(path), font=T.font("body"),
                         text_color=T.TEXT, anchor="w").grid(
                row=0, column=0, sticky="w", padx=(12, 8), pady=6)
            W.ghost_button(row, "✕", width=34,
                           command=lambda p=path: self._remove_file(p)).grid(
                row=0, column=1, padx=(0, 8))

    # ============================================================= file actions
    def _filetypes(self):
        try:
            exts = self.app.cfg.get("file_transcribe_supported_exts") or []
            pattern = " ".join("*" + e for e in exts if isinstance(e, str))
        except Exception:
            pattern = ""
        if not pattern:
            pattern = "*.mp3 *.wav *.m4a *.flac *.webm *.ogg *.mp4 *.mkv"
        return [("Audio / video", pattern), ("All files", "*.*")]

    def _pick_files(self):
        try:
            paths = filedialog.askopenfilenames(
                title="Select audio or video files",
                filetypes=self._filetypes())
        except Exception:
            paths = ()
        if not paths:
            return
        limit = 10
        try:
            limit = int(self.app.cfg.get("file_transcribe_batch_limit", 10) or 10)
        except Exception:
            limit = 10
        added = 0
        for p in paths:
            if p not in self._files:
                if len(self._files) >= limit:
                    self._toast_msg(
                        "Batch limit is %d files; extra files were skipped."
                        % limit, T.WARN)
                    break
                self._files.append(p)
                added += 1
        if added:
            self._toast_msg("")
        self._render_file_list()
        self._sync_transcribe_enabled()

    def _remove_file(self, path):
        self._files = [p for p in self._files if p != path]
        self._render_file_list()
        self._sync_transcribe_enabled()

    def _sync_transcribe_enabled(self):
        try:
            state = "normal" if (self._files and not self._busy) else "disabled"
            self._transcribe_btn.configure(state=state)
        except Exception:
            pass

    # =============================================================== run batch
    def _start_batch(self):
        if self._busy or not self._files:
            return
        engine = getattr(self.app, "engine", None)
        model = getattr(engine, "model", None) if engine is not None else None
        if model is None:
            self._toast_msg(
                "The speech model is still loading. Try again in a moment.",
                T.WARN)
            # Nudge the load along (best-effort).
            try:
                self.app._ensure_engine_started()
            except Exception:
                pass
            return

        self._busy = True
        self._cancel = False
        fmt = self._format_var.get()
        files = list(self._files)
        self._show_progress(len(files))
        threading.Thread(
            target=self._worker, args=(files, fmt, engine), daemon=True,
            name="file-transcribe").start()

    def _worker(self, files, fmt, engine):
        """Background thread: transcribe each file in turn, FAIL-OPEN per file."""
        results = {}
        total = len(files)
        # Belt-and-braces: disarm the live dictation trigger for the whole batch
        # so the user can't fire dictation into the (now busy) shared model while
        # we transcribe. The shared model lock (passed below) is the real safety
        # net; this just avoids contention. Best-effort -- never block the batch.
        try:
            engine.pause()
        except Exception:
            pass
        # The SAME lock the live engine uses around model.transcribe() -- the
        # shared model is not safe for concurrent inference / mid-swap (C1).
        lock = getattr(engine, "_model_lock", None)

        def cuda_fallback(_exc):
            # Reuse the engine's own CPU reload + retry policy. (Called by
            # transcribe_file while it holds the shared lock, so the swap is
            # serialized against any other inference.)
            try:
                cpu_model = engine._load_cpu_model()
                engine.model = cpu_model
                engine.device = "cpu"
                return cpu_model
            except Exception:
                return None

        try:
            for idx, path in enumerate(files, start=1):
                if self._cancel:
                    break
                name = os.path.basename(path)
                self.app.schedule(self._on_progress,
                                  "Transcribing %d/%d: %s" % (idx, total, name),
                                  (idx - 1) / max(1, total))
                model = getattr(engine, "model", None)
                segments, err = FT.transcribe_file(
                    path, model, self.app.cfg,
                    on_progress=lambda m, n=name: self.app.schedule(
                        self._on_progress, "%s — %s" % (n, m), None),
                    should_cancel=lambda: self._cancel,
                    on_cuda_error=cuda_fallback,
                    lock=lock)
                results[path] = {"segments": segments, "error": err}
                self.app.schedule(self._on_progress, None, idx / max(1, total))
        finally:
            # Always re-arm the live dictation trigger when the batch finishes
            # (success, error, or cancel). Best-effort -- never raise.
            try:
                engine.resume()
            except Exception:
                pass
        self.app.schedule(self._on_batch_done, results, fmt)

    # =============================================================== progress
    def _show_progress(self, total):
        self._title.configure(text="Transcribing…")
        self._clear_scroll()
        card = W.Card(self._scroll, title="Working")
        card.grid(row=0, column=0, sticky="ew", padx=4, pady=(0, 12))
        card.grid_columnconfigure(0, weight=1)
        self._progress = ctk.CTkProgressBar(card, height=14,
                                            progress_color=T.ACCENT)
        self._progress.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 8))
        self._progress.set(0.0)
        self._progress_lbl = W.hint_label(
            card, "Starting…", color=T.TEXT_MUTED, wraplength=780)
        self._progress_lbl.grid(row=2, column=0, sticky="w", padx=18,
                                pady=(0, 14))
        W.secondary_button(self._scroll, "Cancel", width=120,
                           command=self._request_cancel).grid(
            row=1, column=0, sticky="w", padx=8, pady=(2, 8))

    def _request_cancel(self):
        self._cancel = True
        self._toast_msg("Canceling after the current file…", T.WARN)

    def _on_progress(self, msg, fraction):
        try:
            if fraction is not None and hasattr(self, "_progress"):
                self._progress.set(max(0.0, min(1.0, float(fraction))))
            if msg is not None and hasattr(self, "_progress_lbl"):
                self._progress_lbl.configure(text=msg)
        except Exception:
            pass

    # =============================================================== results
    def _on_batch_done(self, results, fmt):
        self._busy = False
        self._results = results
        canceled = self._cancel
        self._show_results(results, fmt, canceled)

    def _show_results(self, results, fmt, canceled):
        self._title.configure(text="Transcription results")
        self._clear_scroll()

        ok = [p for p, r in results.items() if not r.get("error")]
        errored = [(p, r.get("error")) for p, r in results.items()
                   if r.get("error") and r.get("error") != "canceled"]

        if canceled:
            self._toast_msg("Canceled. Showing what finished.", T.WARN)
        elif errored and not ok:
            self._toast_msg("Could not transcribe the selected file(s).",
                            T.DANGER)
        else:
            self._toast_msg("Done.", T.OK)

        row = 0
        if not results:
            W.hint_label(self._scroll, "Nothing was transcribed.",
                         color=T.TEXT_MUTED).grid(row=row, column=0, sticky="w",
                                                  padx=8, pady=12)
        for path, r in results.items():
            self._result_card(path, r, fmt).grid(
                row=row, column=0, sticky="ew", padx=4, pady=(0, 12))
            row += 1

        new_btn = W.accent_button(self._scroll, "New batch", width=140,
                                  command=self._reset_to_input)
        new_btn.grid(row=row, column=0, sticky="w", padx=8, pady=(2, 10))

    def _result_card(self, path, r, fmt):
        name = os.path.basename(path)
        card = W.Card(self._scroll, title=name)
        card.grid_columnconfigure(0, weight=1)
        err = r.get("error")
        segments = r.get("segments") or []

        if err and err != "canceled" and not segments:
            W.hint_label(card, "Failed: %s" % err, color=T.DANGER,
                         wraplength=780).grid(row=1, column=0, sticky="w",
                                              padx=18, pady=(0, 14))
            return card

        body = FT.format_segments(segments, fmt)
        if not body.strip() or (fmt == "vtt" and body.strip() == "WEBVTT"):
            W.hint_label(card, "No speech detected in this file.",
                         color=T.TEXT_MUTED).grid(row=1, column=0, sticky="w",
                                                  padx=18, pady=(0, 14))
            return card

        if err == "canceled":
            W.hint_label(card, "Partial (canceled before finishing).",
                         color=T.WARN).grid(row=1, column=0, sticky="w",
                                            padx=18, pady=(0, 4))

        box = ctk.CTkTextbox(card, font=T.font("mono"), fg_color=T.SURFACE_2,
                             text_color=T.TEXT, border_width=1,
                             border_color=T.BORDER, corner_radius=T.RADIUS_SM,
                             wrap="word", height=160)
        box.grid(row=2, column=0, sticky="ew", padx=18, pady=(4, 8))
        box.insert("1.0", body)
        box.configure(state="disabled")

        btns = ctk.CTkFrame(card, fg_color="transparent")
        btns.grid(row=3, column=0, sticky="w", padx=18, pady=(0, 14))
        W.secondary_button(
            btns, "Save file", width=120,
            command=lambda p=path, b=body, f=fmt: self._save(p, b, f)).grid(
            row=0, column=0, padx=(0, 8))
        W.ghost_button(
            btns, "Copy", width=90,
            command=lambda b=body: self._copy(b)).grid(row=0, column=1)
        return card

    # =============================================================== save/copy
    def _save(self, src_path, body, fmt):
        ext = FT.extension_for(fmt)
        base = os.path.splitext(os.path.basename(src_path))[0]
        default_name = "%s_transcript%s" % (base, ext)
        try:
            init_dir = os.path.dirname(src_path) or None
            out = filedialog.asksaveasfilename(
                title="Save transcript",
                defaultextension=ext,
                initialfile=default_name,
                initialdir=init_dir,
                filetypes=[("Transcript", "*" + ext), ("All files", "*.*")])
        except Exception:
            out = ""
        if not out:
            return   # user canceled -> silent
        try:
            with open(out, "w", encoding="utf-8") as f:
                f.write(body)
            self._toast_msg("Saved: %s" % os.path.basename(out), T.OK)
        except Exception as exc:
            self._toast_msg("Could not write file: %s" % exc, T.DANGER)

    def _copy(self, body):
        try:
            self.clipboard_clear()
            self.clipboard_append(body)
            self._toast_msg("Copied to clipboard.", T.OK)
        except Exception:
            self._toast_msg("Could not copy to clipboard.", T.WARN)

    # =============================================================== nav
    def _reset_to_input(self):
        self._files = []
        self._show_input()

    def _on_back(self):
        if self._busy:
            self._cancel = True
        else:
            # Belt-and-braces: if no batch is running, make sure the live
            # dictation trigger is armed (a running batch re-arms in its
            # worker's finally after the current file). resume() is idempotent.
            try:
                engine = getattr(self.app, "engine", None)
                if engine is not None:
                    engine.resume()
            except Exception:
                pass
        try:
            self.app.show_dashboard()
        except Exception:
            pass

    # =============================================================== helpers
    def _toast_msg(self, text, color=T.ACCENT):
        try:
            self._toast.configure(text=text or "", text_color=color)
        except Exception:
            pass
