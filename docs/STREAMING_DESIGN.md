# VoiceFlow Streaming / Realâ€‘Time Dictation â€” Architecture Design

*Lead architect design doc. Target: add a realâ€‘time mode where words appear into whatever app is focused, as the user speaks. Constraints: keep fasterâ€‘whisper/CTranslate2 as the single core engine, reuse the existing platform ABCs / config / customtkinter GUI, run usably on an RTX 3050 4 GB, stay fully offline + MITâ€‘clean. Dev/test box: Windows 11.*

---

## 0. Currentâ€‘facts refresh (2025â€“2026) that shaped this design

These differ in important ways from the input dossiers and drive several decisions below.

- **whisper_streaming (UFAL) is MITâ€‘licensed and uses fasterâ€‘whisper as its recommended backend.** It implements LocalAgreementâ€‘2 and reports ~3.3 s latency on longâ€‘form; with a 1.0 s chunk, average finalâ€‘emission latency â‰ˆ 2.0 s. Default `buffer_trimming` is `('segment', 15)`. ([github.com/ufal/whisper_streaming](https://github.com/ufal/whisper_streaming), [README](https://github.com/ufal/whisper_streaming/blob/main/README.md))
- **The UFAL README now explicitly says: "In 2025, WhisperStreaming is becoming outdated, replaced by SimulStreaming."** SimulStreaming uses the AlignAtt policy, is **~5Ã— faster than LocalAgreement at same/better quality**, and **was relicensed to MIT**. ([ufal/SimulStreaming](https://github.com/ufal/SimulStreaming), [WhisperLiveKit](https://github.com/QuentinFuxa/WhisperLiveKit), [IWSLT 2025](https://arxiv.org/html/2506.17077)) â†’ This is now the recommended **future** upgrade for the streaming policy, replacing "Parakeet" as the longâ€‘term bet.
- **RealtimeSTT is no longer actively maintained** (author states time constraints; PRs occasionally merged). It still uses `multiprocessing` (Windows `if __name__=='__main__'` guard required), defaults `realtime_processing_pause=0.2 s`, `post_speech_silence_duration=0.6 s`, and supports `use_microphone=False` + `feed_audio()`. ([github.com/KoljaB/RealtimeSTT](https://github.com/KoljaB/RealtimeSTT), [pypi](https://pypi.org/project/realtimestt/)) â†’ This weakens it as a dependency; it is demoted to an **optional, gated experiment**, not a shipping fallback.
- **fasterâ€‘whisper** had a release on **2025â€‘10â€‘31**; `int8_float16` runs nonâ€‘quantized layers in FP16 (good 4 GB fit); `BatchedInferencePipeline` exists but is for throughput, not streaming. ([SYSTRAN/faster-whisper](https://github.com/SYSTRAN/faster-whisper), [releases](https://github.com/SYSTRAN/faster-whisper/releases))
- **Wispr Flow** (the UX north star) is **cloudâ€‘only**, tray app, hotkey, "reads the screen", 150â€“220 wpm, inserts via OS accessibility with clipboardâ€‘paste fallback. We replicate the *feel* locally but cannot copy its cloud LLM cleanup. ([wisprflow.ai](https://wisprflow.ai/), [docs](https://docs.wisprflow.ai/articles/3941699399-keyboard-and-screen-reader-accessibility-in-wispr-flow))
- **Claude Code voice mode** is the cleanest reference for the *tentativeâ€‘vsâ€‘committed* contract: speech appears dimmed until finalized; autosubmit off by default and only â‰¥3 words. ([code.claude.com/docs/en/voice-dictation](https://code.claude.com/docs/en/voice-dictation))

---

## 1. Chosen approach + rationale (and fallbacks)

### PRIMARY â€” Embedded **LocalAgreementâ€‘2** streaming over the existing fasterâ€‘whisper model, emitting **confirmedâ€‘prefix tokens only** via **SendInput Unicode keystrokes**.

Build a new `StreamingEngine` that is a **sibling** of `DictationEngine`, selected by a new config key `mode: "batch" | "streaming"`. It reuses:

- the alreadyâ€‘loaded `WhisperModel` (same `load_model()` GPUâ†’CPU fallback + warmup, same `register_cuda_dlls()` ordering),
- the existing `sounddevice` capture pattern (`_audio_cb`),
- `clean_transcript()` / the hallucination filter,
- the `TriggerBackend` (one stable trigger API),
- and a **new platform capability** (`Typer`) for incremental Unicode typing that lives *next to* the existing clipboard `Paster` â€” the batch paste cycle is never touched.

The stability policy is **LocalAgreementâ€‘2** (â‰ˆ300 lines, embedded, fully owned). A word is emitted only after two consecutive overlapping reâ€‘transcriptions agree on it as the longest common prefix; the unstable tail is flushed on VAD endâ€‘ofâ€‘utterance. Output is therefore **strictly appendâ€‘only with no corrections** in the steady state â€” which is exactly what makes typing into a *foreign* app safe (you can't reliably unâ€‘type from someone else's text field).

**Why this one (opinionated):**

1. **Keeps fasterâ€‘whisper as the one core engine.** No second model framework, no server/client split, no new process model. Everything you already hardened (CUDA DLL order, GPUâ†’CPU fallback, hallucination filter, triggers, platform ABCs) is reused.
2. **Appendâ€‘only is the only safe contract for arbitrary apps.** Typing partials that get rewritten requires backspacing in a foreign field, which can eat the user's own text on focus/caret change. LocalAgreementâ€‘2 commits only stable words, so steadyâ€‘state corrections are unnecessary.
3. **No packaging landmines.** RealtimeSTT's `multiprocessing` would forkâ€‘bomb a frozen `VoiceFlow.exe` without a `freeze_support()` guard, and it's now unmaintained. Embedding the algorithm avoids both.
4. **Fits 4 GB.** One `base.en`/`small.en` model at `int8_float16`, reâ€‘decoding a *trimmed* buffer. No dualâ€‘model VRAM pressure.
5. **MITâ€‘clean, fully offline.** fasterâ€‘whisper (MIT), CTranslate2 (MIT), the LocalAgreement algorithm (MIT, retain the UFAL notice). No CCâ€‘BY weights, no attribution headaches.

**Expected stableâ€‘word latency:** ~1.5â€“2.5 s with a 1.0 s chunk interval (the trailing latency *is* the price of never correcting committed text). This is acceptable for dictation and matches UFAL's ~2.0 s figure.

### FALLBACK A (future policy upgrade, not v1) â€” **SimulStreaming / AlignAtt.**
Same engine shape, swap the `StreamCommitter` from LocalAgreementâ€‘2 to AlignAtt. ~5Ã— faster policy, same/better quality, now MIT. This is the **right longâ€‘term bet** and the architecture below is deliberately written so the committer is a pluggable strategy. Defer because it needs attentionâ€‘extraction hooks into the decoder (heavier than the prefix diff) and isn't needed to ship.

### FALLBACK B (lowâ€‘end / CPUâ€‘only) â€” **Vosk** (Apacheâ€‘2.0).
Truly streaming, zeroâ€‘latency partials, ~50 MB models, CPUâ€‘only, fully offline. WER is behind Whisperâ€‘small and it has no autoâ€‘punctuation/casing, but it keeps streaming usable when fasterâ€‘whisper can't hit realâ€‘time on CPU. Gate behind `realtime_model: "vosk"`.

### REJECTED for v1
- **RealtimeSTT as a dependency** â€” unmaintained + separate process + own mic; demote to an optional gated experiment only.
- **WhisperLive / whisper.cpp stream binary** â€” server/socket or C++ binary; duplicates model management; wrong weight for a selfâ€‘contained tray app.
- **NVIDIA Parakeet/Canary** â€” CCâ€‘BY weights + heavy NeMo; poor 4 GB/packaging fit. SimulStreaming is the better future path now.

---

## 2. The exact stableâ€‘output algorithm (LocalAgreementâ€‘2 + VAD commit/reset)

Two cooperating pieces: a **`HypothesisBuffer`** (commit the longest prefix two passes agree on) and the **streaming loop** (rolling buffer, periodic reâ€‘decode, VADâ€‘driven finalize + trim so perâ€‘step cost stays flat).

### 2.1 HypothesisBuffer (LocalAgreementâ€‘2) â€” pseudocode

```
state:
  committed      = []      # (start,end,word) already EMITTED into the app
  prev_tail      = []      # previous pass's unconfirmed tail
  last_committed_time = 0.0

insert(ts_words, offset):                 # ts_words from word_timestamps, +offset
  cur_tail = [ (a+offset, b+offset, w) for (a,b,w) in ts_words
               if (a+offset) > last_committed_time - 0.1 ]   # drop already-said
  return cur_tail

flush(cur_tail):                          # LocalAgreement-2 core
  newly = []
  while cur_tail and prev_tail:
    if cur_tail[0].word == prev_tail[0].word:      # AGREED -> commit
      w = cur_tail.pop(0); prev_tail.pop(0)
      newly.append(w); last_committed_time = w.end
    else:
      break                                        # stop at first disagreement
  prev_tail = cur_tail                             # this pass becomes "previous"
  committed.extend(newly)
  return newly                                      # NEW stable tokens to TYPE

finalize():                               # on VAD end-of-utterance: force-commit tail
  rest = prev_tail
  committed.extend(rest)
  prev_tail = []; last_committed_time = 0.0
  return rest
```

**Word equality** compares normalized text (case/whitespaceâ€‘insensitive) but we **emit Whisper's verbatim cased/punctuated token**. Because a word only commits once two passes agree (â‰ˆ1â€“2 words of trailing context), its casing/punctuation has already stabilized â€” so we get correct casing "for free" with intentional lag and **never speculatively emit casing we'd have to revise**.

### 2.2 Streaming loop â€” pseudocode

```
const CHUNK_INTERVAL  = cfg.stream_chunk_interval_s   # 1.0 (re-decode cadence)
const SILENCE_COMMIT  = cfg.post_speech_silence_s     # 0.6 (VAD finalize)
const MAX_BUFFER_SEC  = cfg.stream_max_buffer_s       # 12-15 (hard trim ceiling)

buf = []                        # float32 rolling audio
offset = 0.0                    # absolute time of buf[0]
hyp = HypothesisBuffer()
silence = 0.0; last_decode = 0.0

loop while streaming:
  block = audio_q.get()                       # 32ms float32 from _audio_cb
  buf += block
  speech = vad.is_speech(block)               # Silero on 512-sample window, <1ms
  silence = 0.0 if speech else silence + len(block)/sr
  now = len(buf)/sr

  # --- periodic re-transcribe of the WHOLE current buffer ---
  if now - last_decode >= CHUNK_INTERVAL and len(buf) > 0.5*sr:
    last_decode = now
    prompt = " ".join(w.word for w in hyp.committed[-40:])   # context (NOT cond_on_prev)
    segs,_ = model.transcribe(buf, language="en", beam_size=1,
                              condition_on_previous_text=False,   # MUST be False
                              word_timestamps=True, vad_filter=False,
                              no_speech_threshold=0.6,
                              initial_prompt=prompt or None)
    words = [(w.start,w.end,w.word) for s in segs for w in (s.words or [])]
    for tok in hyp.flush(hyp.insert(words, offset)):
        emit(tok.word)                          # -> Typer.type_text (append-only)

  # --- VAD end-of-utterance: finalize tail, then RESET buffer (cost bounded) ---
  if silence >= SILENCE_COMMIT and hyp.prev_tail:
    tail = hyp.finalize()
    final_text = clean_join(tok.word for tok in tail)   # hallucination filter HERE
    if final_text: emit(final_text)
    emit(" ")                                   # sentence spacer (NEVER newline)
    buf = []; offset = now                       # reset -> steady-state buf = 2-6s
    beeper.done()

  # --- hard safety trim if user never pauses ---
  elif now > MAX_BUFFER_SEC:
    cut = hyp.last_committed_time - offset
    if cut > 1.0:
      buf = buf[int(cut*sr):]; offset += cut     # drop confirmed audio, keep tail
```

**Why each rule matters** (carried from the dossier gotchas, validated against the current engine):
- `condition_on_previous_text=False` â€” already what `_transcribe()` does; turning it on makes Whisper repeat/hallucinate across overlapping windows and destabilizes the prefix. Pass last committed words as `initial_prompt` for context instead.
- `vad_filter=False` *inside* the decode â€” in streaming, VAD runs *outside* on the rolling buffer (to detect endâ€‘ofâ€‘utterance and trigger reset). Leaving it on wastes time and can drop the trailing partial word needed for agreement. (Batch keeps `vad_filter=True` â€” unchanged.)
- **Trim/reset on every VAD pause** â€” without it, perâ€‘step decode cost climbs with buffer length (UFAL issue #152). Resetting on each sentence keeps the steadyâ€‘state buffer at 2â€“6 s, well under the 1 s interval on GPU.
- **Hallucination filter at finalize, on the whole sentence** â€” not per token. A legit word can look like a blocklist hit in isolation; only the assembled sentence is judged (reuse `clean_transcript`).
- `word_timestamps=True` â€” required for alignment and the trim cut point; budget the small perâ€‘call cost.

---

## 3. New `StreamingEngine` module design

New file: `src/voiceflow/streaming.py`. It does **not** subclass `DictationEngine` (avoids inheriting the batch state machine); it shares helpers via small moduleâ€‘level functions already importable from `engine.py` (`clean_transcript`, `Beeper`, `_is_cuda_error`).

### 3.1 Construction & model reuse

```python
class StreamingEngine:
    """Real-time dictation. Reuses an ALREADY-LOADED faster-whisper model and
    emits confirmed tokens through an on_commit callback (the GUI/typer wires it
    to Typer.type_text). Mirrors DictationEngine's thread discipline."""

    def __init__(self, config, model, device, *,
                 on_commit,           # (text:str) -> None   committed words to TYPE
                 on_partial=None,     # (text:str) -> None   tentative tail (overlay)
                 on_state=None, on_log=None, on_level=None):
        self.cfg = config
        self.model = model           # <-- reuse the loaded WhisperModel
        self.device = device
        self.sr = int(config.get("sample_rate", 16000))
        self.on_commit = on_commit
        self.on_partial = on_partial
        ...
        self._triggers = _platform.make_trigger_backend()   # same trigger API
        self.beeper = Beeper(config.get("beep", True))
        self._audio_q = queue.Queue()
        self._streaming = False
        self._vad = _make_silero_vad()     # faster-whisper bundles Silero; reuse
```

**Model ownership:** the App loads the model once (existing `DictationEngine.load_model()`), then constructs whichever engine `mode` selects, **passing the same `model`/`device`**. On `mode` switch at runtime, swap the engine object without reloading the model.

### 3.2 Threads (identical discipline to the batch engine)
- **PortAudio callback** (`_audio_cb`): copy frame, push 32 ms float32 blocks into `_audio_q`, update VU. Never blocks.
- **Trigger callback** (hook thread): microscopic â€” flip `_streaming`, enqueue start/stop, return. *No transcription, no SendInput here.*
- **Stream worker thread**: runs the Â§2.2 loop (VAD + periodic `transcribe` + `emit`). This is where decode and typing happen.

### 3.3 Emit path

```python
def _emit_committed(self, text):
    clean, filtered = clean_transcript(text, self.cfg)   # reuse; allow_multiline collapses \n
    if clean and not filtered:
        self.on_commit(clean)          # GUI: committer.type(clean) via Typer
```

`on_commit` is a callback (not a direct platform call) so the **engine stays platformâ€‘agnostic** and unitâ€‘testable with a fake sink â€” matching the existing callback style (`on_transcript`, etc.).

### 3.4 Trigger / activation
- Default **hold (pushâ€‘toâ€‘talk)**: stream while held, release = endâ€‘ofâ€‘utterance + finalize.
- Also support **toggle** (a11yâ€‘friendly â€” hold is the worst model for RSI/arthritis).
- `base.HotkeyBackend` already declares `supports_hold_mode` + `register(combo, on_press, on_release)`; the abstraction anticipated this. The Windows keyboard path currently registers **pressâ€‘only** (`trigger_on_release=False`), so for true hold we wire `on_release` (mouse hooks already see buttonâ€‘up). **If hold can't be wired for a given trigger, fall back to toggle** rather than failing.
- Independently, VAD `post_speech_silence_s` autoâ€‘finalizes a sentence even in hold mode (so long holds still commit incrementally).

---

## 4. New platform capability: incremental Unicode typing (`Typer`)

### 4.1 Why a new ABC, not an extension of `Paster`
`Paster.paste()` synthesizes a **chord** (Ctrl/Cmd+V). Typing a literal string + Backspace is a different capability. Overloading `Paster` would conflate "press a key combo" with "emit a string" and muddy `set_chord`. Add a sibling **`Typer`** with a `make_typer()` factory, mirroring `make_clipboard()` / `make_trigger_backend()`. **Batch keeps `clip.paste_text()` verbatim â€” zero regression.**

### 4.2 The ABC (add to `platform/base.py`)

```python
class Typer(ABC):
    """Synthesize literal text as keystrokes (streaming/incremental insertion).
    Unlike Paster (a paste CHORD), this emits the characters themselves and never
    touches the clipboard."""

    @abstractmethod
    def type_text(self, text: str) -> bool:
        """Inject text into the focused window as Unicode keystrokes.
        Returns False if injection was blocked (caller may fall back to paste)."""

    @abstractmethod
    def backspace(self, n: int) -> None:
        """Emit n Backspace keystrokes (only for the current utterance's own
        unstable tail; never beyond the injection start offset)."""

    @property
    @abstractmethod
    def supports_incremental(self) -> bool:
        """True if type_text+backspace are reliable enough for streaming here."""
```

Factory in `platform/__init__.py`:
```python
def make_typer():
    b = _backend_module()
    fn = getattr(b, "make_typer", None)
    return fn() if fn else None        # None -> platform has no reliable typer
```

### 4.3 Windows â€” `SendInput` + `KEYEVENTF_UNICODE` (reuse the existing 40â€‘byte `_INPUT`)

The hard ctypes plumbing already exists in `platform/windows.py` (`_INPUT`, `_KEYBDINPUT`, `_u32.SendInput` with argtypes, `_ev`). We add only the Unicode flag + functions. Layoutâ€‘independent (`wVk=0`, `wScan=`code unit). Emit **one down/up pair per UTFâ€‘16 code unit** (handles emoji/astral chars via surrogate pairs).

```python
_KEYEVENTF_UNICODE = 0x0004
_VK_BACK = 0x08

def _unicode_ev(code_unit, up=False):
    flags = _KEYEVENTF_UNICODE | (_KEYEVENTF_KEYUP if up else 0)
    inp = _INPUT(); inp.type = _INPUT_KEYBOARD
    inp.u.ki = _KEYBDINPUT(0, code_unit, flags, 0, 0)   # wVk MUST be 0
    return inp

def _type_text_sendinput(text):
    if not text: return 0
    units = text.encode("utf-16-le")                     # BMP + surrogate pairs
    seq = []
    for i in range(0, len(units), 2):
        cu = units[i] | (units[i+1] << 8)
        seq += [_unicode_ev(cu), _unicode_ev(cu, up=True)]
    arr = (_INPUT * len(seq))(*seq)
    return _u32.SendInput(len(arr), arr, ctypes.sizeof(_INPUT))   # ONE batched call

def _backspace_sendinput(n):
    if n <= 0: return
    seq = []
    for _ in range(n):
        seq += [_ev(_VK_BACK), _ev(_VK_BACK, up=True)]   # reuse existing _ev (VK path)
    arr = (_INPUT * len(seq))(*seq)
    _u32.SendInput(len(arr), arr, ctypes.sizeof(_INPUT))

class WindowsTyper(Typer):
    def type_text(self, text):
        _release_stuck_modifiers()        # reuse send_paste()'s GetAsyncKeyState trick
        n = _type_text_sendinput(text)
        if n == 0:                        # blocked (e.g. elevated foreground window)
            log.warning("SendInput typed 0 (UIPI/admin window). GetLastError=%s",
                        ctypes.get_last_error())
            return False
        return True
    def backspace(self, n): _backspace_sendinput(n)
    @property
    def supports_incremental(self): return True

def make_typer(): return WindowsTyper()
```

Carry over the existing **UIPI diagnostic**: a nonâ€‘elevated VoiceFlow can't inject into an elevated/admin window â€” same limitation as the paste path; `type_text()` returning False is the signal to surface that and fall back to pasteâ€‘atâ€‘finalize.

### 4.4 macOS â€” `CGEventKeyboardSetUnicodeString` (pyobjc Quartz)
```python
def _post_unicode(text):
    for down in (True, False):
        ev = CGEventCreateKeyboardEvent(None, 0, down)
        utf16_len = len(text.encode("utf-16-le")) // 2     # code units, not chars
        CGEventKeyboardSetUnicodeString(ev, utf16_len, text)  # avoids pyobjc #162 drop
        CGEventPost(kCGHIDEventTap, ev)
```
Requires **Accessibility (TCC)** â€” already covered by the `Permissions` ABC (`check()['accessibility']`). Backspace = `kVK_Delete` (0x33).

### 4.5 Linux â€” pick by `detect_platform()`
- **Wayland**: `wtype <text>` (virtualâ€‘keyboard protocol; `-d MS` interâ€‘key delay), `wtype -k BackSpace` for backspace.
- **X11**: `xdotool type --clearmodifiers -- <text>`, `xdotool key BackSpace`.
- **Cross fallback**: `ydotool type` (needs `ydotoold` + `/dev/uinput`; slow, can't target a window) â€” surface the missing daemon via `Permissions`.
- All are external binaries: **check `shutil.which()` at startup; if absent, `supports_incremental=False` â†’ degrade to clipboardâ€‘pasteâ€‘atâ€‘finalize.**

### 4.6 Coexistence with the existing clipboard `Paster`
- **Batch mode** â†’ unchanged `clip.paste_text()` (verified verbatim cycle).
- **Streaming mode** â†’ `Typer.type_text()` for committed words (appendâ€‘only, **clipboard untouched** â€” a strict UX win: no clobber, no restore race, no Clipboardâ€‘History leak).
- **Final tail / typer blocked / `supports_incremental==False`** â†’ fall back to the existing clipboard paste for that one flush (matches Wispr Flow's accessibilityâ€‘thenâ€‘clipboard pattern).

---

## 5. Config + GUI

### 5.1 `constants.DEFAULT_CONFIG` additions (keep batch defaults intact)
```python
"mode": "batch",                  # "batch" (current) | "streaming"
"streaming_activation": "hold",   # "hold" | "toggle" (toggle = a11y-friendly)
"realtime_model": "base.en",      # model for the streaming loop on 4GB (small.en if it keeps up)
"stream_chunk_interval_s": 1.0,   # re-decode cadence; 0.7 snappier/hotter GPU
"post_speech_silence_s": 0.6,     # VAD silence that finalizes a sentence
"stream_max_buffer_s": 12.0,      # hard trim ceiling (Whisper window is 30s)
"stream_local_agreement_n": 2,    # confirmed-prefix agreement count (2 = proven)
"streaming_insert_method": "keystroke",  # "keystroke" (SendInput unicode) | "paste"
"streaming_show_overlay": True,   # live caption of tentative words
"streaming_inter_key_delay_ms": 0 # bump for RDP/Citrix/terminals that drop fast input
```

### 5.2 `config._coerce_config` additions (mirror existing style)
```python
_str("mode"); _str("streaming_activation"); _str("realtime_model")
_str("streaming_insert_method")
_num("stream_chunk_interval_s", float, minimum=0.3)
_num("post_speech_silence_s", float, minimum=0.2)
_num("stream_max_buffer_s", float, minimum=5.0)
_num("stream_local_agreement_n", int, minimum=2)
_num("streaming_inter_key_delay_ms", float, minimum=0)
for b in ("streaming_show_overlay",): _bool(b)
# Optional: validate mode/activation/insert_method against an allowed set, else default.
```

### 5.3 GUI (in `ui/settings.py`, a new "Realâ€‘time (beta)" card)
- **Mode toggle**: Batch â‡„ Streaming `CTkSegmentedButton`. Switching calls a new `app.set_mode(mode)` that swaps the engine object (no model reload). Show a oneâ€‘line "beta / appears as you speak / works in any app" hint.
- **Activation**: Hold / Toggle segmented control (default Hold for streaming).
- **Latency slider**: `stream_chunk_interval_s` 0.7â€“1.5 s, labeled "Snappier â†” Smoother (uses more GPU)".
- **Endâ€‘ofâ€‘sentence pause**: `post_speech_silence_s` 0.3â€“1.0 s ("too short cuts you off, too long feels laggy").
- **Live caption overlay** switch (`streaming_show_overlay`).
- **Insertion method**: Keystroke (default) / Clipboardâ€‘atâ€‘pause, with a hint that some terminals/RDP prefer the latter (and the `inter_key_delay_ms` advanced field).
- Reuse the existing `_toggle_row`/`_toast_msg`/`apply_behavior_change` plumbing; persist via `vf_config.save_config`.

### 5.4 Optional liveâ€‘caption overlay (`ui/overlay.py`)
A tiny **alwaysâ€‘onâ€‘top, noâ€‘activate, clickâ€‘through** window showing committed (solid) + tentative (dimmed) text near the cursor â€” the Claudeâ€‘Code "dimmed until finalized" pattern, but in VoiceFlow's own surface since we type into a foreign field.
- Windows extended styles: `WS_EX_NOACTIVATE | WS_EX_TRANSPARENT | WS_EX_LAYERED | WS_EX_TOPMOST` (must **never steal focus** or insertion breaks).
- Driven by `on_partial` (tentative tail) and `on_commit` (promote to solid). Fully optional; product must work with it disabled.
- A11y: mirror state with the existing `Beeper` tones (start/stop/done/error/filtered) so screenâ€‘reader users aren't reliant on a visual caption.

---

## 6. Safety

- **Never emit Enter/newline.** A stray `\n` in Slack/Discord/Teams/Terminal acts as **Send/Execute** midâ€‘utterance. `clean_transcript` already collapses `\r\n`â†’space when `allow_multiline` is False; the streaming emit path runs **every** committed chunk through it, and `Typer` only ever sends Unicode characters â€” **never a synthesized VK_RETURN**. Keep `allow_multiline` defaulting to False for streaming regardless of the batch setting.
- **Autoâ€‘submit OFF** (no autoâ€‘Enter). If ever added, gate behind explicit optâ€‘in **and** a â‰¥3â€‘word threshold (Claude Code's rule).
- **Bounded correction only.** Backspace is allowed **only within the current utterance's own injected character count** (tracked offset). Never backspace past the injection start â†’ can't eat the user's handâ€‘typed text or a previous utterance.
- **Focus guard.** Capture `GetForegroundWindow()` at stream start; on each commit, if the foreground window changed, **stop typing** (don't backspace into a different app). Bail to "appendâ€‘only, no correction" or finalâ€‘paste.
- **Abort/cancel (mandatory).** A dedicated Esc/abort key + overlay Cancel button: **stop the stream, discard the pending tail, freeze injection, flash overlay red, distinct error beep.** It does **not** autoâ€‘delete alreadyâ€‘committed words from a foreign app (unsafe). Offer a separate, explicit **"delete last utterance"** that backspaces exactly the tracked injected count (safe because bounded).
- **Throttle / fastâ€‘input drops.** Batch a whole chunk into **one** `SendInput` call (atomic, ordered). Expose `streaming_inter_key_delay_ms` (default 0) for RDP/Citrix/VM/Electron targets that drop fast synthetic input.
- **Stuck modifiers.** Reuse `send_paste()`'s `GetAsyncKeyState` release trick before the first `type_text` of a stream (the trigger combo may still be physically held).
- **Hookâ€‘thread discipline.** Transcription + SendInput run on the worker thread only; the trigger callback stays microscopic (<~300 ms) or Windows silently removes the hook (already documented in `engine.py`).
- **Hallucination filter at finalize**, on the assembled sentence, not per token (avoids dropping a legit stable word that looks like a blocklist hit in isolation).

---

## 7. GPUâ€‘load & latency on a 4 GB RTX 3050 â€” and how to keep it usable

**The binding constraint is VRAM + sustained decode load, not raw latency.** Streaming reâ€‘decodes a growing buffer every ~1 s, so sustained GPU load/heat is far above the current pressâ€‘once batch usage.

**Make it usable:**
- **One model only** (no dualâ€‘model). `base.en @ int8_float16` for the streaming loop is the safe default on 4 GB; promote to `small.en` only if it keeps decode time < chunk interval. (`int8_float16` runs nonâ€‘quant layers in FP16 â€” ~2â€“3 GB resident.)
- **Trim/reset on every VAD pause** so the steadyâ€‘state buffer is **2â€“6 s**, not 15 s. A 2â€“6 s buffer at greedy `beam_size=1` decodes well under a 1 s interval on the 3050.
- **Hard `stream_max_buffer_s = 12` ceiling** (below Whisper's 30 s window) for users who never pause.
- **Selfâ€‘adaptive latency rule**: if a decode takes longer than the chunk interval, run the next decode **immediately** on whatever audio accumulated (never queue/pile up â€” effective latency rises gracefully instead of starving).
- **CUDAâ†’CPU fallback honored for the streaming model too** (reuse `_transcribe_with_fallback`'s pattern). On CPU, `base.en`/`tiny.en` are fasterâ€‘thanâ€‘realâ€‘time; if even that lags, the Vosk fallback (B) keeps streaming responsive.
- **Expected latency**: ~1.5â€“2.5 s stableâ€‘word latency at 1.0 s chunks (UFAL â‰ˆ2.0 s); ~0.7 s chunks trade snappier output for higher GPU load/heat.

---

## 8. Phased implementation checklist

**Phase 0 â€” Plumbing & config (Windowsâ€‘testable)**
- [ ] Add streaming keys to `DEFAULT_CONFIG` + coercion in `config.py`. Verify batch still loads byteâ€‘identical.
- [ ] Add `Typer` ABC to `platform/base.py`; `make_typer()` to `platform/__init__.py`.

**Phase 1 â€” Windows Typer (Windowsâ€‘testable)**
- [ ] Implement `WindowsTyper` (`_type_text_sendinput`, `_backspace_sendinput`) reusing `_INPUT`/`_ev`. Unitâ€‘test typing into Notepad, a browser field, VS Code, a terminal; verify emoji (surrogate pairs) and the UIPIâ€‘blocked (admin window) path returns False.

**Phase 2 â€” Streaming core (Windowsâ€‘testable, the meat)**
- [ ] `streaming.py`: `HypothesisBuffer` (LocalAgreementâ€‘2) + `StreamingEngine` (Â§2/Â§3). Pureâ€‘function unit tests for the buffer (agreement, finalize, noâ€‘doubleâ€‘emit).
- [ ] Wire Silero VAD on 32 ms blocks; the rolling buffer + trim/reset; `on_commit`/`on_partial` callbacks.
- [ ] Reuse loaded model + GPUâ†’CPU fallback; confirm decode time < interval on the 3050 with `base.en`.

**Phase 3 â€” Insertion + safety (Windowsâ€‘testable)**
- [ ] `StreamCommitter` (bounded backspace, focus guard, newline sanitation, stuckâ€‘modifier release). Abort key + "delete last utterance".
- [ ] Finalâ€‘tail/blocked fallback to clipboard paste.

**Phase 4 â€” App + GUI (Windowsâ€‘testable)**
- [ ] `app.set_mode()` to swap engines without model reload; wire trigger hold/toggle (`on_release`).
- [ ] Settings "Realâ€‘time (beta)" card; optional clickâ€‘through overlay; beep mirroring.

**Phase 5 â€” Hardening**
- [ ] Latency/heat tuning on the 3050; selfâ€‘adaptive rule; RDP/terminal delay path; longâ€‘session soak (verify perâ€‘step latency stays flat â€” guards UFAL #152).

**Phase 6 â€” Crossâ€‘platform (needs mac/Linux hardware)**
- [ ] `MacTyper` (CGEvent unicode, TCC) â€” **needs macOS + Accessibility grant.**
- [ ] `LinuxTyper` (wtype/xdotool/ydotool) â€” **needs Wayland and X11 sessions; ydotool needs uinput.**

**Phase 7 â€” Future**
- [ ] Swap committer to **SimulStreaming/AlignAtt** (MIT, ~5Ã— faster). [ ] **Vosk** CPU fallback. [ ] Optional **RealtimeSTT** experiment behind a flag (with `freeze_support()` guard) â€” only if revived.

### Windowsâ€‘testable vs needs mac/Linux
- **Fully testable now on the Win11 / RTX 3050 box:** Phases 0â€“5 (the entire primary approach: config, `WindowsTyper`, LocalAgreementâ€‘2 core, streaming loop, safety, GUI, overlay, GPU tuning).
- **Needs other hardware:** `MacTyper` (macOS + TCC), `LinuxTyper` (Wayland + X11; ydotool/uinput). These are isolated behind `make_typer()` and don't block the Windows ship.

---

## 9. Sources
- whisper_streaming (MIT, LocalAgreementâ€‘2, fasterâ€‘whisper backend, ~3.3s/2.0s latency, buffer_trimming): https://github.com/ufal/whisper_streaming Â· https://github.com/ufal/whisper_streaming/blob/main/README.md Â· https://github.com/ufal/whisper_streaming/issues/152 Â· https://ar5iv.labs.arxiv.org/html/2307.14743
- SimulStreaming (AlignAtt, ~5Ã— faster, now MIT â€” 2025 successor): https://github.com/ufal/SimulStreaming Â· https://github.com/QuentinFuxa/WhisperLiveKit Â· https://arxiv.org/html/2506.17077
- RealtimeSTT (unmaintained note, params, feed_audio, multiprocessing): https://github.com/KoljaB/RealtimeSTT Â· https://pypi.org/project/realtimestt/
- fasterâ€‘whisper / CTranslate2 (int8_float16, 2025â€‘10â€‘31 release, batched pipeline): https://github.com/SYSTRAN/faster-whisper Â· https://github.com/SYSTRAN/faster-whisper/releases Â· https://pypi.org/project/faster-whisper/
- Vosk (Apacheâ€‘2.0 CPU streaming fallback): https://alphacephei.com/vosk/ Â· https://github.com/alphacep/vosk-api
- Silero VAD (512â€‘sample/32ms, <1ms): https://github.com/snakers4/silero-vad
- SendInput KEYEVENTF_UNICODE: https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-sendinput Â· https://github.com/boppreh/keyboard/blob/master/keyboard/_winkeyboard.py Â· https://batchloaf.wordpress.com/2014/10/02/using-sendinput-to-type-unicode-characters/
- macOS CGEventKeyboardSetUnicodeString + pyobjc emoji caveat: https://developer.apple.com/documentation/coregraphics/cgevent/keyboardsetunicodestring(stringlength:unicodestring:) Â· https://bitbucket.org/ronaldoussoren/pyobjc/issues/162
- Linux typing: https://github.com/atx/wtype Â· https://github.com/jordansissel/xdotool Â· https://gadgeteer.co.za/ydotool-is-an-alternative-to-xdotool-that-works-on-both-x11-and-wayland/
- UX references: Claude Code voice mode https://code.claude.com/docs/en/voice-dictation Â· Wispr Flow https://wisprflow.ai/ , https://docs.wisprflow.ai/articles/3941699399-keyboard-and-screen-reader-accessibility-in-wispr-flow
- VoiceFlow source: `src/voiceflow/engine.py`, `src/voiceflow/platform/base.py`, `src/voiceflow/platform/windows.py`, `src/voiceflow/platform/__init__.py`, `src/voiceflow/constants.py`, `src/voiceflow/config.py`, `src/voiceflow/ui/settings.py`, `src/voiceflow/app.py`