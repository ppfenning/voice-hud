#!/bin/bash
# Idempotent voice-HUD launcher: start the server if it's not up, and open the
# page once per Claude session (the PreToolUse hook feeds JSON with session_id
# on stdin). Opening is decoupled from server startup so the HUD still shows up
# when the server survived from an earlier session. Must return fast and never
# block the tool call.

# Keep a bluetooth headset in A2DP before the voice turn starts (see ensure_a2dp.sh).
"$(dirname "$0")/ensure_a2dp.sh" || true

INPUT=$(cat 2>/dev/null)
SESSION=$(printf '%s' "$INPUT" | sed -n 's/.*"session_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
MARKER="/tmp/voice-hud-opened-${SESSION:-default}"

if ! curl -sf -m 1 http://127.0.0.1:8123/health >/dev/null 2>&1; then
  nohup python3 "$HOME/repos/voice-hud/server.py" > /tmp/voice-hud.log 2>&1 &
  sleep 0.6
fi

if [ ! -e "$MARKER" ]; then
  open -a "Brave Browser" http://127.0.0.1:8123 2>/dev/null || open http://127.0.0.1:8123
  touch "$MARKER"
fi
exit 0
