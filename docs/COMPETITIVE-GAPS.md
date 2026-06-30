# OpenVerba — Competitive Feature-Gap Report (2026-06-15)

The research notes plus the detailed "ALREADY HAS" list give me everything I need. The codebase confirmation isn't load-bearing for this competitive-gap deliverable — I have the feature inventory. Let me produce the report.

---

# OpenVerba — Competitive Gap Report

*Functions competitors have (or users loudly ask for) that we DON'T have yet.* Deduped across 5 research sweeps. Items we already ship (local transcription, AI editing of selected text, custom vocab via history-learning, voice commands, clipboard preservation, auto-update, model picker, dictation history, hold-vs-tap modes) are **excluded**.

Feasibility legend for a **free / local / Windows** app: **[Easy]** · **[Medium]** · **[Hard]** · **[Against-our-ethos]** (cloud-only, paid-only, or platform-locked).

---

## 1. GAP LIST

### A. Capture & Activation

| Gap | What it is | Who has it | Why users like it | Feasibility |
|---|---|---|---|---|
| **AI auto-cleanup during plain dictation** | Strip "um/uh", fix stumbles, auto-punctuate/capitalize *automatically on normal dictation* — not only when you select text and ask. The #1 most-praised feature in every source. | Wispr, superwhisper, Aqua, Willow, MacWhisper | "Rambled thoughts become clear, perfectly formatted text"; "never type again." We have AI editing of *selected* text, but not auto-cleanup of *fresh* dictation. | **[Medium]** — reuse our Ollama path on the just-transcribed buffer before paste |
| **Cleanup-level control (None/Light/Medium/High)** | A dial for how aggressively cleanup rewrites, to prevent over-editorializing. | Wispr (4 levels) | Top complaint about cleanup is it "over-editorializes / rewrites instead of transcribes" — the dial is the fix. | **[Easy]** — prompt/temperature presets |
| **Self-correction ("meet at 4… actually 3" → "3")** | Detects spoken false-starts/"I mean"/"wait" and outputs only the final intent. | Wispr, Willow, Aqua | "Feels magical"; #2 most-loved. | **[Medium]** — part of the cleanup LLM prompt |
| **Whisper Mode (recognize near-silent/whispered speech)** | Tuned to transcribe quiet whispering for open offices/libraries (~92–95%). | Wispr, Willow | "Removes the main social barrier to dictating at work." | **[Hard]** — needs VAD/model tuning, hard to match accuracy |
| **Hands-free / "No-Hands" toggle (double-tap → keeps listening)** | A continuous-listen mode for long-form, separate from tap/hold. | Willow, Wispr | Long-form dictation + accessibility (tremor/Parkinson's/dyslexia users cite it). | **[Easy]** — a third activation state |
| **Faster cold-start / instant-on** | Sub-second readiness; competitors complained-about 8–10s init disrupts "quick bursts." | (anti-pattern users punish all tools for) | "Instant-on" is an explicit wish; a local app can win here. | **[Medium]** — keep model warm/resident |

### B. Formatting & AI

| Gap | What it is | Who has it | Why users like it | Feasibility |
|---|---|---|---|---|
| **Per-app / context-aware tone & formatting** | Detect the foreground app and auto-adjust tone+format: formal in Gmail, casual in Slack, code-aware in VS Code/Cursor. Top differentiator across the board. | Wispr, superwhisper (Super Mode), Aqua, Willow, VoiceInk (Power Mode) | "Removes the mental overhead of switching modes manually"; repeatedly called "unique"/"game-changer." | **[Medium]** — Win32 foreground-window detection → per-app profile + prompt |
| **Custom "Modes" = saved prompt + model + format presets** | User-defined presets (Email / Note / Code / Commit-msg), each with its own system prompt, model, and hotkey; the signature superwhisper power feature. | superwhisper, VoiceInk, MacWhisper, Aqua | "Most flexible system in the category… nothing comes close on configurability." | **[Medium]** — we already have an LLM layer; this is presets + UI |
| **Tone presets (Very Casual → Casual → Excited → Formal)** | A quick per-style tone selector independent of app. | Wispr ("Personalized Style") | "Emails sound like emails, Slack sounds like Slack." | **[Easy]** — prompt presets |
| **Live intent-aware editing with on-screen status** | While dictating, show "Deleting…/Adding to list…/Fixing spelling…" and act on plain-language feedback live. | Aqua Voice | "By far the most convenient STT they've ever used." | **[Hard]** — tight real-time LLM-fusion loop |
| **Code-aware dictation** | Keep code as code, recognize dev terms (Supabase/Vercel), don't let the formatter mangle snippets. | Wispr, Aqua | Lets devs dictate prompts/code cleanly. | **[Medium]** — a code-mode prompt + lighter punctuation |

### C. Vocabulary, Commands & Snippets

| Gap | What it is | Who has it | Why users like it | Feasibility |
|---|---|---|---|---|
| **Voice snippets / text expansion** | Speak a short cue → expand a saved block (email signature, scheduling link, FAQ, boilerplate; up to ~4k chars). | Wispr, superwhisper, VoiceInk, Dragon | "Stop typing the same things over and over"; big repetitive-typing saver. | **[Easy]** — phrase→template map, fires before paste |
| **Natural-language inline editing with NO memorized syntax** | "Make this a list", "rephrase that", "redo the second sentence" — plain language, no command vocabulary to learn. (We have *fixed-keyword* commands + selected-text AI edit; this is the free-form, no-selection-needed variant.) | Aqua, Willow, Wispr (Command Mode) | "No command memorization"; "feels like magic." | **[Medium]** — route free-form utterances through the LLM intent layer |
| **Pronunciation training for custom terms** | Train *how a word sounds* AND what gets typed (medical/legal/jargon) — the Dragon gold standard. | Dragon | The deepest dictionary; still the benchmark users cite. | **[Hard]** — beyond Whisper's bias-prompt approach |
| **Macro / shell-command / automation triggers** | After transcription, run a script / Apple-Shortcut / open a URL — "voice-controlled workflows." | superwhisper (shell triggers), Macrowhisper | "A power-user feature no other dictation app offers." | **[Medium]** — run a user command with transcript as arg (sandbox it) |

### D. Workflow & UX

| Gap | What it is | Who has it | Why users like it | Feasibility |
|---|---|---|---|---|
| **Audio/video FILE transcription (drag-drop, batch, watch-folder)** | Transcribe pre-recorded MP3/MP4/YouTube; batch + auto-transcribe a folder. MacWhisper "owns" this; live-only tools lack it. | MacWhisper, superwhisper, VoiceInk | The main reason file-workflow users pick those tools; we already run Whisper, so it's low-marginal-cost. | **[Easy]** — feed files through the existing faster-whisper pipeline |
| **Speaker diarization + subtitle (SRT/VTT) export** | Label speakers; export captions. | MacWhisper, superwhisper | Big for meetings/interviews/video. | **[Medium]** — diarization needs an extra model (e.g. pyannote); SRT export is trivial |
| **Meeting recorder + auto-digest to notes** | Capture system+mic audio of Zoom/Teams/Meet, output labeled notes — on-device. | superwhisper, MacWhisper | "Audio never leaves your device — good for sensitive/legal meetings." | **[Medium]** — system-audio loopback capture + summarize prompt |
| **Re-paste / re-insert last transcript hotkey** | One key to paste the previous result again without re-dictating. (We have a history *screen*; this is a one-shot re-insert.) | Wispr (Alt+Shift+Z), Alter | "Never lose your voice again"; avoids re-dictating. | **[Easy]** |
| **Preview/edit transcript before it commits** | Show text in an editable overlay to review/fix before it pastes/sends. | (loudly requested; ChatGPT-voice removal called "major regression") | Users want to catch errors before they land in the target app. | **[Medium]** — optional confirm-overlay before paste |
| **Searchable history** | Search past transcripts. (We have a history screen — confirm search exists; if not, it's a small add.) | VoiceInk, Wispr (local SQLite) | Find/re-use prior dictations. | **[Easy]** |
| **Global hotkey conflict detection / remap UI** | Detect clashes and let users remap push-to-talk cleanly. | (frequent r/macapps wish) | Removes a common setup frustration. | **[Easy]** |
| **Lower idle RAM/CPU footprint** | Stay light when idle (rivals get hammered for ~800MB/8% idle, freezing VS Code). | (anti-pattern users punish) | "Real concern on 8GB machines." | **[Medium]** — engineering, not a feature |

### E. Languages & Insights

| Gap | What it is | Who has it | Why users like it | Feasibility |
|---|---|---|---|---|
| **Multi-language + auto-detect + code-switching** | 100+ languages, detect at session start, handle mixed-language sentences (Hinglish, English-in-Czech). We're English-only today. | Wispr, superwhisper, VoiceInk, Willow | Huge for bilingual users; "keeps English words in English mid-sentence." | **[Medium]** — Whisper is multilingual; mostly config/UI + model size |
| **Translate-on-dictate (speak X → English out)** | Foreign speech → English text, with an explicit toggle. | superwhisper, Willow, Wispr (via command) | Sought by non-native English writers. *Caveat: must be a clear toggle — users hate it firing unwanted.* | **[Medium]** — Whisper translate task |
| **Usage analytics (words dictated, time saved, WPM, profile card)** | A personal dashboard + shareable stat cards; gamified. | Typeless, Monologue, Wispr (Voice Profile/Insights) | Drives habit/retention; shareable cards = organic marketing. | **[Easy]** — we already log; just surface stats |

### Off-ethos / structural (listed for completeness, not recommended)

| Gap | Who | Why it's off-ethos for us |
|---|---|---|
| Cross-device sync of dictionary/snippets/settings | Wispr, superwhisper | Requires accounts/cloud infra — against free/local/no-account ethos. |
| BYOK cloud LLM/STT models (OpenAI/Claude/Deepgram/ElevenLabs) | superwhisper, VoiceInk | Cloud-dependent + paid; we're local-first. (Optional opt-in only.) |
| Team/shared dictionaries & snippets, admin dashboards | Wispr, superwhisper Teams | SaaS/seat-based, needs a backend. |
| SOC2/HIPAA compliance posture | Wispr, superwhisper | Enterprise sales motion, not relevant to a free local tool (we're *more* private by default). |
| iOS/Android voice keyboard | Wispr, superwhisper, Willow | Platform-locked; we're Windows-first (mac/linux already planned). |
| Eye-tracking + noise (pop/hiss) click input, full mouse/OS grid control | Talon, Win Voice Access | Different product category (hands-free OS control); Windows Voice Access already gives this free. |

---

## 2. TOP 5 RECOMMENDATIONS (highest value-per-effort)

1. **AI auto-cleanup on plain dictation + a None/Light/Medium/High dial.** [Medium] — The single most-praised feature in *all five* sweeps, and we already have the Ollama pipeline; the dial directly neutralizes the #1 complaint (over-editorializing). This is the biggest "loved feature" gap to close.
2. **Per-app context profiles (tone/format auto-switch by foreground app) + saved "Modes."** [Medium] — Repeatedly called the top differentiator and "game-changer"; Win32 foreground detection + our existing LLM layer makes it cheap relative to its marketing weight.
3. **Voice snippets / text expansion.** [Easy] — Low effort, universally shipped, concrete daily time-saver (signatures, links, canned replies).
4. **Audio/video file transcription (drag-drop + batch + watch-folder) with SRT export.** [Easy] — We already run faster-whisper; this opens a whole second use-case (meetings, interviews, video) at near-zero marginal cost and is a category MacWhisper "owns."
5. **Multi-language + auto-detect (and translate-on-dictate as a toggle).** [Medium] — Whisper is already multilingual; flipping this on unlocks the large bilingual segment and a feature every paid rival charges for, while we give it free + local.

*Honorable mentions (all [Easy], grab while in the code):* re-paste-last-transcript hotkey, hands-free/No-Hands toggle, usage-stats screen, tone presets, hotkey-conflict remap UI.

---

## 3. SKIP — not worth it for a free/local/Windows app

- **Cross-device sync** — requires accounts + cloud backend; breaks the no-account/local ethos. (If ever wanted: local export/import file instead.)
- **BYOK cloud LLM/STT models** — cloud + paid dependency; only justifiable as a clearly-labeled optional opt-in, never default.
- **Team/shared dictionaries, admin dashboards, SOC2/HIPAA posture** — SaaS/enterprise motion with a backend and compliance overhead; irrelevant to a free local tool that is already more private by default.
- **iOS/Android voice keyboard** — platform-locked; out of scope for Windows-first (mac/linux already on the roadmap).
- **Eye-tracking / noise-click / full mouse-and-OS grid control (Talon-style)** — different product category; Windows 11 Voice Access already provides OS-level hands-free control for free, so building it duplicates a free OS feature.
- **Pronunciation-training dictionary (Dragon-depth)** — [Hard]; Whisper's bias-prompt + our correction-map already covers most of the value at a fraction of the effort.
- **Whisper Mode (near-silent speech) and live intent-aware on-screen editing** — both [Hard] to match incumbents' accuracy/latency; revisit only after the high-ROI items ship.

**Bottom line:** Our structural moat (free, local, private-by-default, no account, low-footprint potential) is exactly what cloud incumbents get punished for lacking. The clearest wins are *post-processing layers on top of the local Whisper pipeline we already have* — auto-cleanup, per-app modes, snippets, file transcription, multi-language — none of which require giving up the local/free ethos.