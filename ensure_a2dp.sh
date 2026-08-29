#!/usr/bin/env bash
# Make sure Pat's headphones are BOTH selected and in A2DP before a voice turn.
#
# Two failures this guards, both hit repeatedly on 2026-08-24:
#
#  1. WRONG DEVICE. Pat: "please pass all vocals through the Amiron, that's my
#     main headphone, so if you don't talk through there I can't even hear you."
#     Anything -- a disconnect, a dock event, this script's own renegotiation --
#     can leave the default output somewhere else, and he is often walking around
#     and not looking at the screen, so he has no way to notice.
#
#  2. WRONG PROFILE. The Amiron slid into HFP (hands-free) three separate times.
#     In HFP it enumerates as 1 channel / 16000 Hz and voicemode opens a 24 kHz
#     Kokoro stream against it, so he hears distortion or nothing -- while every
#     layer reports success.
#
# The diagnostic for (2) is PortAudio's device table, NOT SwitchAudioSource: the
# device stays correctly *selected* throughout, so a routing check says all fine.
# A2DP = 2ch/44100+. HFP = 1ch/16000.
#
# Silent and non-fatal by design -- this runs ahead of every converse and must
# never be the reason a voice turn fails to start.
set -u

PREFERRED="Amiron wireless"

command -v SwitchAudioSource >/dev/null 2>&1 || exit 0
PY="$HOME/.local/share/uv/tools/voice-mode/bin/python"
[ -x "$PY" ] || exit 0

is_hfp() {
  "$PY" - "$1" <<'PYEOF' 2>/dev/null
import sys
try:
    import sounddevice as sd
except Exception:
    sys.exit(1)                      # cannot tell -> treat as fine
name = sys.argv[1]
for d in sd.query_devices():
    if name.split()[0] in d["name"] and d["max_output_channels"] > 0:
        # A2DP is stereo at 44.1k+; HFP is mono at 16k
        sys.exit(0 if (d["max_output_channels"] < 2 or d["default_samplerate"] < 44100) else 1)
sys.exit(1)
PYEOF
}

AVAILABLE=$(SwitchAudioSource -a -t output 2>/dev/null) || exit 0

# (1) If the preferred headset is connected but not selected, select it.
if printf '%s\n' "$AVAILABLE" | grep -qF "$PREFERRED"; then
  CURRENT=$(SwitchAudioSource -c -t output 2>/dev/null)
  if [ "$CURRENT" != "$PREFERRED" ]; then
    SwitchAudioSource -s "$PREFERRED" -t output >/dev/null 2>&1 && sleep 1
    echo "voice-hud: routed output back to $PREFERRED (was $CURRENT)" >&2
  fi
fi

CURRENT=$(SwitchAudioSource -c -t output 2>/dev/null) || exit 0
case "$CURRENT" in
  *Amiron*|*AirPods*|*Buds*) ;;   # bluetooth: profile can be wrong
  *) exit 0 ;;                     # wired/built-in: HFP cannot apply
esac

# (2) If it is in HFP, renegotiate by bouncing off the device and back.
is_hfp "$CURRENT" || exit 0

FALLBACK=$(printf '%s\n' "$AVAILABLE" | grep -vF "$CURRENT" | head -1)
[ -n "$FALLBACK" ] || exit 0
SwitchAudioSource -s "$FALLBACK" -t output >/dev/null 2>&1
sleep 1
SwitchAudioSource -s "$CURRENT" -t output >/dev/null 2>&1
sleep 2

# Never leave him on the fallback, whatever happened above.
NOW=$(SwitchAudioSource -c -t output 2>/dev/null)
[ "$NOW" = "$CURRENT" ] || SwitchAudioSource -s "$CURRENT" -t output >/dev/null 2>&1

if is_hfp "$CURRENT"; then
  echo "voice-hud: $CURRENT still in HFP after renegotiation" >&2
else
  echo "voice-hud: renegotiated $CURRENT back to A2DP" >&2
fi
exit 0
