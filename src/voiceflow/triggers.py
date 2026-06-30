"""
voiceflow.triggers - platform-neutral trigger-UX helpers for the GUI.

The trigger *picker* in the GUI needs three things that are conceptually
OS-agnostic UX but whose concrete implementation is OS-specific (live capture
uses the platform's hooks):

  * classify_trigger(trigger) -> {"trigger","label","clean","warning"}
  * PRESETS                   -> list of clean, conflict-free suggestions
  * TriggerRecorder           -> live "press your trigger" capture

Rather than have the GUI import an OS module directly, it imports these from
here; we pull them from the active platform backend (selected by
voiceflow.platform.detect_platform). The engine itself uses the richer
TriggerBackend (voiceflow.platform.make_trigger_backend) for registration.
"""

from __future__ import annotations

from . import platform as _platform

_backend = _platform._backend_module()

# Re-export the platform's trigger-UX surface.
TriggerRecorder = _backend.TriggerRecorder
classify_trigger = _backend.classify_trigger
PRESETS = _backend.PRESETS

# The combined registration backend (keyboard + mouse + chord behind one API),
# in case a non-engine caller wants direct access.
register_trigger = _backend.register_trigger

__all__ = ["TriggerRecorder", "classify_trigger", "PRESETS", "register_trigger"]
