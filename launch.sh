#!/bin/bash
# Idempotent voice-HUD launcher: start the server if it's not up, and open the
# page once per Claude session (the PreToolUse hook feeds JSON with session_id
# on stdin). Opening is decoupled from server startup so the HUD still shows up
# when the server survived from an earlier session. Must return fast and never
# block the tool call.
#
# Portable across macOS and Linux: `open -a` exists only on macOS, so the
# browser step probes for it and falls back to Brave's Linux binary, then to
# xdg-open. Brave on purpose — plans and the HUD are never to open elsewhere.

# Keep a bluetooth headset in A2DP before the voice turn starts (see ensure_a2dp.sh).
"$(dirname "$0")/ensure_a2dp.sh" || true

INPUT=$(cat 2>/dev/null)
SESSION=$(printf '%s' "$INPUT" | sed -n 's/.*"session_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
MARKER="${TMPDIR:-/tmp}/voice-hud-opened-${SESSION:-default}"
URL="http://127.0.0.1:8123"

if ! curl -sf -m 1 "$URL/health" >/dev/null 2>&1; then
  # Under systemd the unit owns the server; only fall back to nohup when it
  # is genuinely absent (no unit, or a machine that never installed one).
  if command -v systemctl >/dev/null 2>&1 \
     && systemctl --user list-unit-files voice-hud-server.service >/dev/null 2>&1; then
    systemctl --user start voice-hud-server.service 2>/dev/null
  else
    nohup python3 "$HOME/repos/voice-hud/server.py" > "${TMPDIR:-/tmp}/voice-hud.log" 2>&1 &
  fi
  sleep 0.6
fi

open_browser() {
  if command -v open >/dev/null 2>&1 && [ "$(uname)" = "Darwin" ]; then
    open -a "Brave Browser" "$1" 2>/dev/null || open "$1"
  elif command -v brave-browser >/dev/null 2>&1; then
    nohup brave-browser "$1" >/dev/null 2>&1 &
  elif command -v brave >/dev/null 2>&1; then
    nohup brave "$1" >/dev/null 2>&1 &
  else
    nohup xdg-open "$1" >/dev/null 2>&1 &
  fi
}

if [ ! -e "$MARKER" ]; then
  open_browser "$URL"
  touch "$MARKER"
fi
exit 0
