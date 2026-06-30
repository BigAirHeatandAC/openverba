"""
gui.onboarding - the first-run flow shown when config["first_run_done"] is False.

Steps:
  1. Welcome
  2. "Scanning your hardware..."  -> hardware.detect_hardware() (bg thread)
  3. Hardware card + recommended models (comparison + "Recommended for your PC"
     badge) -> user picks a model
  4. Download the picked model with a real progress bar (models.download_model)
  5. If an NVIDIA GPU is present, offer "Enable GPU acceleration" -> ensures the
     CUDA runtime wheels are present (cuda.install_gpu_runtime)
  6. Done -> hands control back to the main window (dashboard)

All long work runs on background threads; UI updates are marshalled via .after.
The OnboardingView is a frame the App swaps into its content area. When finished
it calls on_done(model_id, enabled_gpu: bool).
"""

from __future__ import annotations

import threading
import traceback

import customtkinter as ctk

from . import theme as T
from . import widgets as W

from voiceflow import hardware
from voiceflow import models as vf_models
from voiceflow import cuda
from voiceflow import config as vf_config


class OnboardingView(ctk.CTkFrame):
    def __init__(self, master, cfg: dict, on_done, **kw):
        kw.setdefault("fg_color", T.BG)
        super().__init__(master, **kw)
        self.cfg = cfg
        self.on_done = on_done
        self.download_root = vf_config.resolve_download_root(cfg)

        self._hw = None
        self._recs = []
        self._selected = None
        self._gpu_present = False

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._page = None
        self._show_welcome()

    # ------------------------------------------------------------------ utils
    def _swap(self, builder):
        if self._page is not None:
            self._page.destroy()
        self._page = ctk.CTkFrame(self, fg_color="transparent")
        self._page.grid(row=0, column=0, sticky="nsew")
        self._page.grid_columnconfigure(0, weight=1)
        self._page.grid_rowconfigure(0, weight=1)
        builder(self._page)

    def _centered(self, parent):
        """A centered column inside parent (so content doesn't stretch full-width)."""
        wrap = ctk.CTkFrame(parent, fg_color="transparent")
        wrap.grid(row=0, column=0)
        return wrap

    # ------------------------------------------------------------- 1. welcome
    def _show_welcome(self):
        def build(p):
            c = self._centered(p)
            ctk.CTkLabel(c, image=W.make_ctk_icon(96), text="").grid(
                row=0, column=0, pady=(10, 14))
            ctk.CTkLabel(c, text="Welcome to OpenVerba", font=T.font("title"),
                         text_color=T.TEXT).grid(row=1, column=0)
            ctk.CTkLabel(
                c, text="Free, private, offline voice typing for Windows.",
                font=T.font("h3"), text_color=T.ACCENT).grid(
                row=2, column=0, pady=(4, 18))
            steps = ctk.CTkFrame(c, fg_color="transparent")
            steps.grid(row=3, column=0, pady=(0, 22))
            for i, (em, head, sub) in enumerate([
                ("1", "Press your trigger", "A key combo or mouse button."),
                ("2", "Speak naturally", "OpenVerba records your microphone."),
                ("3", "It types for you", "Transcribed locally, pasted in place."),
            ]):
                self._step_chip(steps, em, head, sub).grid(
                    row=0, column=i, padx=10)
            W.hint_label(
                c, "Everything runs on your PC. Your voice never leaves the "
                   "machine. We'll scan your hardware and pick the best model "
                   "for you next.", color=T.TEXT_MUTED, wraplength=520,
                justify="center").grid(row=4, column=0, pady=(0, 22))
            W.accent_button(c, "Get started  →", command=self._start_scan,
                            width=220, height=46).grid(row=5, column=0)
        self._swap(build)

    def _step_chip(self, master, num, head, sub):
        f = ctk.CTkFrame(master, fg_color=T.SURFACE, corner_radius=T.RADIUS,
                         border_width=1, border_color=T.BORDER, width=180,
                         height=130)
        f.grid_propagate(False)
        f.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(f, text=num, font=T.font("h1"), text_color=T.ACCENT).grid(
            row=0, column=0, pady=(16, 2))
        ctk.CTkLabel(f, text=head, font=T.font("body_bold"),
                     text_color=T.TEXT).grid(row=1, column=0)
        W.hint_label(f, sub, color=T.TEXT_MUTED, wraplength=150,
                     justify="center").grid(row=2, column=0, padx=10, pady=(4, 0))
        return f

    # -------------------------------------------------------- 2. scanning
    def _start_scan(self):
        def build(p):
            c = self._centered(p)
            self._spinner = ctk.CTkLabel(c, text="◓", font=T.font("huge"),
                                         text_color=T.ACCENT)
            self._spinner.grid(row=0, column=0, pady=(20, 10))
            ctk.CTkLabel(c, text="Scanning your hardware…",
                         font=T.font("h1"), text_color=T.TEXT).grid(
                row=1, column=0)
            W.hint_label(c, "Detecting your GPU, CPU, and memory to recommend "
                            "the best speech model.", color=T.TEXT_MUTED,
                         wraplength=420, justify="center").grid(
                row=2, column=0, pady=(8, 0))
        self._swap(build)
        self._animate_spinner()
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _animate_spinner(self):
        frames = ["◐", "◓", "◑", "◒"]
        if getattr(self, "_spinner", None) is None:
            return
        try:
            idx = (getattr(self, "_spin_i", 0) + 1) % len(frames)
            self._spin_i = idx
            self._spinner.configure(text=frames[idx])
            self._spin_job = self.after(180, self._animate_spinner)
        except Exception:
            pass

    def _scan_worker(self):
        try:
            hw = hardware.detect_hardware()
            recs = hardware.recommend_models(hw)
        except Exception:
            hw = {"gpu": {"present": False}, "cpu": {}, "ram_gb": None,
                  "os": "Windows"}
            recs = hardware.recommend_models(hw)
        self.after(0, lambda: self._show_results(hw, recs))

    # -------------------------------------------------- 3. hardware + models
    def _show_results(self, hw, recs):
        self._hw = hw
        self._recs = recs
        self._gpu_present = bool((hw.get("gpu") or {}).get("present"))
        # Default selection = the recommended-tier model (first rec).
        self._selected = recs[0]["model_id"] if recs else self.cfg.get("model")

        def build(p):
            # Rows: 0 hardware header/card, 1 section label, 2 scroll (grows),
            # 3 footer. Each lives in its OWN row so nothing overlaps.
            p.grid_rowconfigure(0, weight=0)
            p.grid_rowconfigure(1, weight=0)
            p.grid_rowconfigure(2, weight=1)
            p.grid_rowconfigure(3, weight=0)
            p.grid_columnconfigure(0, weight=1)

            head = ctk.CTkFrame(p, fg_color="transparent")
            head.grid(row=0, column=0, sticky="ew", padx=8, pady=(4, 8))
            head.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(head, text="Here's your PC", font=T.font("h1"),
                         text_color=T.TEXT, anchor="w").grid(
                row=0, column=0, sticky="w")
            self._hw_card(head).grid(row=1, column=0, sticky="ew", pady=(8, 0))

            ctk.CTkLabel(p, text="Choose your speech model",
                         font=T.font("h2"), text_color=T.TEXT, anchor="w").grid(
                row=1, column=0, sticky="nw", padx=8, pady=(12, 2))

            scroll = ctk.CTkScrollableFrame(p, fg_color="transparent")
            scroll.grid(row=2, column=0, sticky="nsew", padx=2, pady=(2, 4))
            scroll.grid_columnconfigure(0, weight=1)
            self._model_rows = {}
            for i, rec in enumerate(recs):
                row = self._model_row(scroll, rec)
                row.grid(row=i, column=0, sticky="ew", padx=4, pady=5)

            footer = ctk.CTkFrame(p, fg_color="transparent")
            footer.grid(row=3, column=0, sticky="ew", padx=8, pady=(8, 2))
            footer.grid_columnconfigure(0, weight=1)
            W.hint_label(footer, "You can change or add models later in "
                                 "Settings.", color=T.TEXT_FAINT).grid(
                row=0, column=0, sticky="w")
            W.accent_button(footer, "Download & continue  →",
                            command=self._start_download, width=230,
                            height=46).grid(row=0, column=1)
        self._swap(build)
        self._refresh_model_selection()

    def _hw_card(self, master):
        card = W.Card(master)
        card.grid_columnconfigure(0, weight=1)
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.grid(row=0, column=0, sticky="ew", padx=18, pady=16)
        inner.grid_columnconfigure(0, weight=1)

        gpu = self._hw.get("gpu") or {}
        cpu = self._hw.get("cpu") or {}
        ram = self._hw.get("ram_gb")

        if gpu.get("present"):
            vram = gpu.get("vram_mb")
            vram_s = (" · %.1f GB VRAM" % (vram / 1024.0)) if vram else ""
            cuda_s = (" · CUDA %s" % gpu["cuda"]) if gpu.get("cuda") else ""
            gpu_val = "%s%s%s" % (gpu.get("name") or "NVIDIA GPU", vram_s, cuda_s)
            gpu_color = T.OK
        else:
            gpu_val = "No NVIDIA GPU detected — transcription runs on CPU"
            gpu_color = T.TEXT_MUTED

        threads = cpu.get("threads")
        cores = cpu.get("cores")
        cpu_extra = ""
        if cores or threads:
            cpu_extra = "  (%s cores / %s threads)" % (cores or "?", threads or "?")
        cpu_val = (cpu.get("name") or "Unknown CPU") + cpu_extra
        ram_val = ("%.1f GB" % ram) if ram else "Unknown"

        W.StatRow(inner, "GPU", gpu_val, value_color=gpu_color, icon="🎮").grid(
            row=0, column=0, sticky="ew", pady=4)
        W.StatRow(inner, "CPU", cpu_val, icon="🧠").grid(
            row=1, column=0, sticky="ew", pady=4)
        W.StatRow(inner, "Memory", ram_val, icon="💾").grid(
            row=2, column=0, sticky="ew", pady=4)
        W.StatRow(inner, "System", self._hw.get("os") or "Windows",
                  icon="🪟").grid(row=3, column=0, sticky="ew", pady=4)
        return card

    def _model_row(self, master, rec):
        mid = rec["model_id"]
        meta = vf_models.get_model(mid) or {}
        tier = rec.get("tier", "")
        installed = vf_models.is_downloaded(mid, self.download_root)

        row = ctk.CTkFrame(master, fg_color=T.SURFACE, corner_radius=T.RADIUS,
                           border_width=2, border_color=T.BORDER)
        row.grid_columnconfigure(1, weight=1)
        # Click anywhere to select.
        row.bind("<Button-1>", lambda e, m=mid: self._select_model(m))

        # Selection radio dot.
        dot = ctk.CTkLabel(row, text="○", font=T.font("h2"),
                           text_color=T.TEXT_FAINT, width=24)
        dot.grid(row=0, column=0, rowspan=3, padx=(16, 0), pady=14)
        dot.bind("<Button-1>", lambda e, m=mid: self._select_model(m))

        # Title + tier badge.
        titlebar = ctk.CTkFrame(row, fg_color="transparent")
        titlebar.grid(row=0, column=1, sticky="ew", padx=10, pady=(14, 0))
        titlebar.grid_columnconfigure(2, weight=1)
        ctk.CTkLabel(titlebar, text=meta.get("label", mid),
                     font=T.font("h3"), text_color=T.TEXT, anchor="w").grid(
            row=0, column=0, sticky="w")
        W.Badge(titlebar, T.tier_label(tier), color=T.tier_color(tier),
                text_color="#06201d" if tier == "recommended" else "#04222e"
                ).grid(row=0, column=1, padx=8)
        if installed:
            W.Badge(titlebar, "Installed", color=T.OK,
                    text_color="#06231a").grid(row=0, column=3, sticky="e")

        # Reason.
        W.hint_label(row, rec.get("reason", ""), color=T.TEXT_MUTED,
                     wraplength=520).grid(row=1, column=1, sticky="ew",
                                          padx=10, pady=(2, 2))

        # Spec strip: quality / speed dots + size + vram + language.
        spec = ctk.CTkFrame(row, fg_color="transparent")
        spec.grid(row=2, column=1, sticky="w", padx=10, pady=(2, 14))
        ctk.CTkLabel(spec, text="Accuracy", font=T.font("small"),
                     text_color=T.TEXT_FAINT).grid(row=0, column=0, padx=(0, 4))
        W.DotRating(spec, meta.get("quality", 0), color=T.ACCENT).grid(
            row=0, column=1, padx=(0, 14))
        ctk.CTkLabel(spec, text="Speed", font=T.font("small"),
                     text_color=T.TEXT_FAINT).grid(row=0, column=2, padx=(0, 4))
        W.DotRating(spec, meta.get("speed", 0), color=T.INFO).grid(
            row=0, column=3, padx=(0, 14))
        disk = meta.get("disk_mb")
        size_s = ("%.1f GB" % (disk / 1024.0)) if disk and disk >= 1024 \
            else ("%s MB" % disk if disk else "?")
        lang = "English" if meta.get("languages") == "en" else "Multilingual"
        ctk.CTkLabel(
            spec, text="·  %s download  ·  %s" % (size_s, lang),
            font=T.font("small"), text_color=T.TEXT_MUTED).grid(
            row=0, column=4)

        self._model_rows[mid] = (row, dot)
        return row

    def _select_model(self, mid):
        self._selected = mid
        self._refresh_model_selection()

    def _refresh_model_selection(self):
        for mid, (row, dot) in getattr(self, "_model_rows", {}).items():
            if mid == self._selected:
                row.configure(border_color=T.ACCENT)
                dot.configure(text="◉", text_color=T.ACCENT)
            else:
                row.configure(border_color=T.BORDER)
                dot.configure(text="○", text_color=T.TEXT_FAINT)

    # ----------------------------------------------------- 4. download model
    def _start_download(self):
        mid = self._selected
        meta = vf_models.get_model(mid) or {}

        def build(p):
            c = self._centered(p)
            ctk.CTkLabel(c, text="Downloading %s" % meta.get("label", mid),
                         font=T.font("h1"), text_color=T.TEXT).grid(
                row=0, column=0, pady=(20, 4))
            W.hint_label(c, "This is a one-time download. It stays on your PC "
                            "for fully offline use afterward.",
                         color=T.TEXT_MUTED, wraplength=440,
                         justify="center").grid(row=1, column=0, pady=(0, 18))
            self._dl_bar = ctk.CTkProgressBar(c, width=440, height=16,
                                              corner_radius=8,
                                              progress_color=T.ACCENT,
                                              fg_color=T.SURFACE_2)
            self._dl_bar.grid(row=2, column=0, pady=(0, 8))
            self._dl_bar.set(0)
            self._dl_pct = ctk.CTkLabel(c, text="Starting…",
                                        font=T.font("body_bold"),
                                        text_color=T.ACCENT)
            self._dl_pct.grid(row=3, column=0)
            self._dl_detail = ctk.CTkLabel(c, text="", font=T.font("small"),
                                           text_color=T.TEXT_FAINT)
            self._dl_detail.grid(row=4, column=0, pady=(2, 0))
        self._swap(build)
        threading.Thread(target=self._download_worker, args=(mid,),
                         daemon=True).start()

    def _download_worker(self, mid):
        def progress(frac, done, total, desc):
            self.after(0, lambda: self._update_download(frac, done, total, desc))
        try:
            if vf_models.is_downloaded(mid, self.download_root):
                self.after(0, lambda: self._update_download(1.0, 0, 0, "ready"))
            else:
                vf_models.download_model(mid, progress_cb=progress,
                                         download_root=self.download_root)
            self.after(400, self._after_download)
        except Exception as exc:
            tb = traceback.format_exc()
            self.after(0, lambda: self._download_failed(exc, tb))

    def _update_download(self, frac, done, total, desc):
        try:
            self._dl_bar.set(max(0.0, min(1.0, frac)))
            self._dl_pct.configure(text="%d%%" % int(frac * 100))
            if total:
                self._dl_detail.configure(
                    text="%.0f MB / %.0f MB  ·  %s"
                         % (done / 1e6, total / 1e6, desc or ""))
            elif desc:
                self._dl_detail.configure(text=str(desc))
        except Exception:
            pass

    def _download_failed(self, exc, tb):
        def build(p):
            c = self._centered(p)
            ctk.CTkLabel(c, text="Download failed", font=T.font("h1"),
                         text_color=T.DANGER).grid(row=0, column=0, pady=(20, 6))
            W.hint_label(
                c, "OpenVerba couldn't finish downloading the model:\n\n%s\n\n"
                   "Check your internet connection and try again." % exc,
                color=T.TEXT_MUTED, wraplength=460, justify="center").grid(
                row=1, column=0, pady=(0, 20))
            btns = ctk.CTkFrame(c, fg_color="transparent")
            btns.grid(row=2, column=0)
            W.secondary_button(btns, "Back", width=120,
                               command=lambda: self._show_results(
                                   self._hw, self._recs)).grid(
                row=0, column=0, padx=6)
            W.accent_button(btns, "Retry", width=120,
                            command=self._start_download).grid(
                row=0, column=1, padx=6)
        self._swap(build)

    def _after_download(self):
        # Persist the chosen model now so a crash mid-onboarding keeps it.
        self.cfg["model"] = self._selected
        vf_config.save_config(self.cfg)
        if self._gpu_present and not cuda.gpu_runtime_present():
            self._show_gpu_offer()
        else:
            self._finish()

    # ----------------------------------------------------- 5. enable GPU
    def _show_gpu_offer(self):
        gpu = self._hw.get("gpu") or {}
        name = gpu.get("name") or "your NVIDIA GPU"

        def build(p):
            c = self._centered(p)
            ctk.CTkLabel(c, image=W.make_ctk_icon(64), text="").grid(
                row=0, column=0, pady=(8, 8))
            ctk.CTkLabel(c, text="Enable GPU acceleration?",
                         font=T.font("h1"), text_color=T.TEXT).grid(
                row=1, column=0)
            W.hint_label(
                c, "We detected %s. Installing the NVIDIA runtime lets OpenVerba "
                   "transcribe much faster on your GPU. This downloads a few "
                   "hundred MB of CUDA libraries (one time). You can skip this "
                   "and run on CPU instead." % name,
                color=T.TEXT_MUTED, wraplength=480, justify="center").grid(
                row=2, column=0, pady=(8, 18))
            btns = ctk.CTkFrame(c, fg_color="transparent")
            btns.grid(row=3, column=0)
            W.secondary_button(btns, "Skip (use CPU)", width=150,
                               command=self._finish).grid(
                row=0, column=0, padx=6)
            W.accent_button(btns, "Enable GPU  ⚡", width=180,
                            command=self._install_gpu).grid(
                row=0, column=1, padx=6)
        self._swap(build)

    def _install_gpu(self):
        def build(p):
            c = self._centered(p)
            ctk.CTkLabel(c, text="Installing GPU runtime…",
                         font=T.font("h1"), text_color=T.TEXT).grid(
                row=0, column=0, pady=(16, 4))
            W.hint_label(c, "Downloading the NVIDIA CUDA libraries. This can "
                            "take a couple of minutes.", color=T.TEXT_MUTED,
                         wraplength=440, justify="center").grid(
                row=1, column=0, pady=(0, 14))
            self._gpu_bar = ctk.CTkProgressBar(c, width=440, height=14,
                                               mode="indeterminate",
                                               progress_color=T.ACCENT,
                                               fg_color=T.SURFACE_2)
            self._gpu_bar.grid(row=2, column=0, pady=(0, 12))
            self._gpu_bar.start()
            self._gpu_log = ctk.CTkTextbox(c, width=520, height=160,
                                           font=T.font("mono"),
                                           fg_color=T.SURFACE_2,
                                           text_color=T.TEXT_MUTED,
                                           border_width=1, border_color=T.BORDER)
            self._gpu_log.grid(row=3, column=0)
            self._gpu_log.configure(state="disabled")
        self._swap(build)
        threading.Thread(target=self._gpu_worker, daemon=True).start()

    def _gpu_worker(self):
        def emit(line):
            self.after(0, lambda: self._gpu_log_append(line))
        try:
            ok, msg = cuda.install_gpu_runtime(progress_cb=emit)
        except Exception as exc:
            ok, msg = False, "GPU runtime install failed: %s" % exc
        self.after(0, lambda: self._gpu_done(ok, msg))

    def _gpu_log_append(self, line):
        try:
            self._gpu_log.configure(state="normal")
            self._gpu_log.insert("end", line + "\n")
            self._gpu_log.see("end")
            self._gpu_log.configure(state="disabled")
        except Exception:
            pass

    def _gpu_done(self, ok, msg):
        try:
            self._gpu_bar.stop()
        except Exception:
            pass
        if ok:
            self.cfg["device"] = "auto"
            vf_config.save_config(self.cfg)

        def build(p):
            c = self._centered(p)
            ctk.CTkLabel(
                c, text="GPU enabled ✓" if ok else "GPU not enabled",
                font=T.font("h1"),
                text_color=T.OK if ok else T.WARN).grid(
                row=0, column=0, pady=(20, 6))
            W.hint_label(c, msg, color=T.TEXT_MUTED, wraplength=480,
                         justify="center").grid(row=1, column=0, pady=(0, 20))
            W.accent_button(c, "Finish setup  →", width=200,
                            command=lambda: self._finish(gpu_enabled=ok)).grid(
                row=2, column=0)
        self._swap(build)

    # --------------------------------------------------------------- 6. done
    def _finish(self, gpu_enabled=False):
        self.cfg["model"] = self._selected
        self.cfg["first_run_done"] = True
        vf_config.save_config(self.cfg)
        try:
            self.on_done(self._selected, gpu_enabled)
        except Exception:
            pass
