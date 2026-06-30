"""
voiceflow.constants - paths, state names, and default configuration.

Per-user data dir (NOT the install dir) holds config.json + voiceflow.log so the
app needs no admin rights and survives reinstalls/upgrades. On Windows this is
%LOCALAPPDATA%\\VoiceFlow.
"""

import os

APP_NAME = "VoiceFlow"

# User-facing display name shown in the GUI, tray, notifications, and logs.
# APP_NAME stays "VoiceFlow" as the data-dir name (display != dir) pending a
# release-time data migration; only APP_DISPLAY_NAME is the public brand.
APP_DISPLAY_NAME = "OpenVerba"

# ---------------------------------------------------------------------------
# Per-user data directory (config + logs). NEVER write these into the install
# dir (e.g. %LOCALAPPDATA%\\Programs\\VoiceFlow): that may be read-only and is
# wiped on upgrade.
# ---------------------------------------------------------------------------
def _user_data_dir():
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(
            os.path.expanduser("~"), "AppData", "Local")
        return os.path.join(base, APP_NAME)
    # Cross-platform fallback (dev on non-Windows).
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, APP_NAME)


DATA_DIR = _user_data_dir()
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
LOG_PATH = os.path.join(DATA_DIR, "voiceflow.log")

# Default location for downloaded models (faster-whisper / HF snapshots). The
# config "download_root" may override this; None there means "use this dir".
MODELS_DIR = os.path.join(DATA_DIR, "models")

# Debug captures (saved audio + paired transcripts) when save_recordings is on.
RECORDINGS_DIR = os.path.join(DATA_DIR, "recordings")

# Transcript history + learning (text-only, local-only; survives upgrades since
# they live in DATA_DIR alongside config.json). Independent of save_recordings.
HISTORY_PATH = os.path.join(DATA_DIR, "history.jsonl")
CORRECTIONS_PATH = os.path.join(DATA_DIR, "corrections.json")
PERSONAL_VOCAB_PATH = os.path.join(DATA_DIR, "personal_vocab.json")
# User-defined text-expansion snippets (trigger -> expansion), applied before
# paste in batch mode. Local-only; survives upgrades (lives in DATA_DIR).
SNIPPETS_PATH = os.path.join(DATA_DIR, "snippets.json")
# Per-app context profiles ("modes"): each maps a set of exe basenames to a
# cleanup/biasing prompt + tone. Local-only; survives upgrades (lives in
# DATA_DIR). Seeded with builtin modes on first use.
MODES_PATH = os.path.join(DATA_DIR, "modes.json")


def ensure_data_dir():
    """Create the per-user data dir (and models subdir). Safe to call often."""
    for d in (DATA_DIR, MODELS_DIR):
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass
    return DATA_DIR


def ensure_recordings_dir():
    """Create the recordings dir (for debug audio + transcripts)."""
    try:
        os.makedirs(RECORDINGS_DIR, exist_ok=True)
    except Exception:
        pass
    return RECORDINGS_DIR


# ---------------------------------------------------------------------------
# Engine state machine names. Public string values double as the values handed
# to the GUI via the on_state callback ("idle"/"recording"/"transcribing").
# ---------------------------------------------------------------------------
IDLE = "IDLE"
RECORDING = "RECORDING"
TRANSCRIBING = "TRANSCRIBING"

# Lower-case state names the GUI/tray display (callback payload).
STATE_LABELS = {
    IDLE: "idle",
    RECORDING: "recording",
    TRANSCRIBING: "transcribing",
}

# Internal control-thread commands.
CMD_STOP = "STOP"
CMD_STOP_STREAM = "STOP_STREAM"   # finalize + stop a streaming session
CMD_STOP_COMMAND = "STOP_COMMAND"  # finalize a voice-command capture
CMD_STOP_PREVIEW = "STOP_PREVIEW"  # finalize a live-preview session (batch decode)

# Single-instance mutex name (machine-wide; blocks a 2nd background runtime).
SINGLETON_MUTEX = "Global\\VoiceFlowSingleton"

_VALID_SAMPLE_RATES = (8000, 11025, 16000, 22050, 32000, 44100, 48000)

# ---------------------------------------------------------------------------
# Default configuration. Ported verbatim from main.py DEFAULT_CONFIG, but the
# canonical trigger key is now "trigger" (back-compat alias "hotkey" is still
# accepted on load). Default model is the English-tuned small.en.
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "trigger": "ctrl+shift+space",   # keyboard combo / "mouse:..." trigger
    "model": "small.en",             # default model id (catalog id)
    "language": "en",                # None/"auto" = auto-detect; "en" = faster
    "translate_to_english": False,   # True + multilingual model -> translate to English
    "device": "auto",                # auto -> try CUDA then CPU
    "compute_type": "int8_float16",  # GPU compute (Turing+ tensor cores)
    "cpu_compute_type": "int8",      # CPU fallback compute
    "vad_filter": True,
    "beam_size": 1,                  # 1 = greedy = lowest latency
    "initial_prompt": "Hello. This is a dictation. Add commas, periods, and proper capitalization.",
    "insert_method": "paste",        # only "paste" implemented in v1
    # ---- streaming / real-time mode ----
    "mode": "batch",                 # "batch" (press->speak->paste) | "streaming"
                                     #   | "preview" (live bar -> clean paste on stop)
    "preview_max_chars": 120,        # max chars shown in the live preview bar
    "preview_pos": "",               # "x,y" where the user dragged the preview bar ("" = bottom-center)
    "streaming_chunk_seconds": 1.0,  # re-transcribe the rolling buffer this often
    "streaming_silence_seconds": 0.7,  # trailing silence that ends an utterance
    "streaming_max_buffer_seconds": 14.0,  # hard cap on the rolling buffer
    "streaming_insert": "paste",     # "paste" (reliable everywhere incl. Notepad)
                                     #   | "type" (synthesize keystrokes; cleaner
                                     #   but garbles in the Win11 Notepad)
    # ---- voice commands (hands-free editing) ----
    "voice_commands": True,          # detect spoken commands in batch mode
    "command_word": "computer",      # activation word that marks a command
    "require_command_word": True,    # True = only "<word> ..." utterances are commands
    "command_trigger": "",           # 2nd trigger that captures a spoken command
                                     #   directly (no wake word), e.g. "mouse:left+middle"
    "command_via_hold": True,        # if the trigger is a mouse chord: TAP=dictate,
                                     #   HOLD=command mode (easier than a 2nd chord)
    "command_hold_seconds": 0.7,     # how long to hold the chord to enter command mode
    # ---- AI text editing (a command that isn't a mechanical key action is sent
    #      to an LLM to rewrite the SELECTED text). Default = local Ollama. ----
    "ai_edit": True,                 # enable LLM editing fallback in command mode
    "ai_provider": "ollama",         # "ollama" (local/free) | "anthropic" | "openai"
    "ai_model": "qwen2.5:3b",        # ollama model tag, or cloud model id
    "ollama_url": "http://localhost:11434",
    "ai_api_key": "",                # only for anthropic/openai
    "ai_timeout": 90,                # seconds to wait for the model (AI edit)
    "cleanup_timeout": 10.0,         # seconds to wait for inline auto-cleanup
                                     #   (short: must not delay the paste path)
    "cleanup_model": "qwen2.5:1.5b",  # SMALL model used ONLY for medium/high
                                     #   cleanup (light is rule-based, no LLM)
    "cleanup_keep_alive": "10m",     # Ollama keep_alive so the small cleanup
                                     #   model stays warm between dictations
    # ---- debug capture: save each recording's audio + transcript for review.
    #      Off by default (privacy/disk); turn on to collect data to improve. ----
    "save_recordings": False,
    "add_trailing_space": True,
    "strip_whitespace": True,
    "allow_multiline": False,        # False -> collapse newlines to spaces
    "min_record_seconds": 0.4,
    "max_record_seconds": 120,
    "sample_rate": 16000,
    "clipboard_restore_delay_ms": 200,
    "clipboard_read_timeout_ms": 2500,
    "beep": True,
    "filter_hallucinations": True,
    "tray_icon": True,
    "download_root": None,           # None -> MODELS_DIR; or a folder path
    "local_files_only": False,       # True after first download = fully offline
    "autostart": False,              # start the background runtime at login
    "first_run_done": False,         # GUI onboarding completed
    # ---- bug reporting (delivered to the developer; no backend server) ----
    "bug_report_email": "bigairfortmyers@gmail.com",  # where bug reports go
    "bug_report_method": "form",     # "form" (silent POST -> email) | "mailto"
                                     #   (open the user's email client)
    # ---- auto-update (check openverba.com for a newer build) ----
    "auto_update_check": True,                                   # check on startup (once/day)
    "update_manifest_url": "https://openverba.com/latest.json",  # where to look
    "last_update_check": 0,                                      # epoch seconds; 0 = never
    "last_notified_version": "",     # last version we popped a tray notice for (anti-nag)
    # ---- transcript history + learning (local-only, text-only) ----
    "transcript_history": True,       # record every committed transcript's TEXT (local-only)
    "personal_vocab_enabled": True,   # bias recognition with learned vocabulary (hotwords + prompt)
    "corrections_enabled": True,      # apply learned exact corrections before paste (batch)
    "snippets_enabled": True,         # expand user text-expansion snippets before paste (batch)
    # ---- per-app context modes (contextual auto-formatting) ----
    "per_app_modes": False,           # enable per-app mode auto-select (biases Whisper by foreground app)
    # ---- AI auto-cleanup of dictation (opt-in; local Ollama; batch only) ----
    "auto_cleanup": False,            # opt-in; requires Ollama + ai_model
    "cleanup_level": "light",         # "off" | "light" | "medium" | "high"
    # ---- file transcription (audio/video file -> text / SRT / VTT) ----
    "file_transcribe_enabled": True,  # feature gate (soft-disable if needed)
    "file_transcribe_batch_limit": 10,  # max files queued in one batch (UX bound)
    "file_transcribe_supported_exts": [  # extensions offered in the file picker
        ".mp3", ".wav", ".m4a", ".flac", ".webm", ".ogg",
        ".mp4", ".mkv", ".avi", ".mov",
    ],
}
