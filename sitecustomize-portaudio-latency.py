"""Pat's local fix (2026-08-17): default all PortAudio streams to 'high' latency.

voicemode opens sd.OutputStream() without a latency argument, which means
PortAudio's low-latency default. On the DisplayLink (Dell dock) output device
that produces constant crackle/static; the same audio plays clean through
CoreAudio (afplay). 'high' asks CoreAudio for larger device buffers (~tens of
ms — inaudible for voice replies) and is the standard fix.

This file lives outside the voice_mode package so `uv tool upgrade voice-mode`
keeps it; a full venv recreation (--force reinstall / python bump) drops it and
it must be re-added. See memory: project_personal_tooling_trials.
"""
try:
    import sounddevice as _sd
    _sd.default.latency = ("high", "high")
except Exception:
    pass
