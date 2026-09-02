"""Where runtime state lives.

Everything the server and the listener write while running — the heartbeat,
the inbox, mute/standby flags, pid files, the gate-mode override — churns
constantly and belongs to one machine, so it lives OUTSIDE the package:
$VOICE_HUD_STATE_DIR if set, else ~/.local/state/voice-hud. The directory is
created on first import so every writer can assume it exists.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["STATE_DIR", "state_dir"]


def state_dir() -> Path:
    raw = os.environ.get("VOICE_HUD_STATE_DIR")
    path = Path(raw).expanduser() if raw else Path.home() / ".local" / "state" / "voice-hud"
    path.mkdir(parents=True, exist_ok=True)
    return path


STATE_DIR = state_dir()
