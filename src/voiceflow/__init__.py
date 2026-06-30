"""
VoiceFlow - free, local, offline speech-to-text dictation for the desktop.

Press a trigger -> record the mic -> transcribe locally with faster-whisper
(GPU if available, else CPU) -> paste into the focused app via the clipboard,
restoring the user's previous clipboard. Fully local/offline after the model
downloads.

This is the ``src/`` layout package. Hatchling reads ``__version__`` below for
the project version (see ``[tool.hatch.version]`` in pyproject.toml).
"""

__version__ = "1.1.0"

# Re-export the public surface the GUI / app entrypoint use. Note: engine and
# cuda import faster_whisper / ctranslate2 (CUDA DLL registration runs at engine
# import time), so we keep those imports lazy here to avoid paying that cost for
# code that only needs config/constants. Import them directly from submodules,
# e.g. `from voiceflow.engine import DictationEngine`.

from .constants import (
    APP_NAME, DATA_DIR, CONFIG_PATH, LOG_PATH, MODELS_DIR,
    DEFAULT_CONFIG, IDLE, RECORDING, TRANSCRIBING, STATE_LABELS,
    ensure_data_dir,
)
from .config import load_config, save_config, resolve_download_root

__all__ = [
    "__version__",
    "APP_NAME", "DATA_DIR", "CONFIG_PATH", "LOG_PATH", "MODELS_DIR",
    "DEFAULT_CONFIG", "IDLE", "RECORDING", "TRANSCRIBING", "STATE_LABELS",
    "ensure_data_dir",
    "load_config", "save_config", "resolve_download_root",
    # Lazily available (import from submodules to trigger CUDA setup only then):
    # voiceflow.engine.DictationEngine
    # voiceflow.platform.make_backends / detect_platform
    # voiceflow.cuda.register_cuda_dlls / gpu_runtime_present / install_gpu_runtime
]
