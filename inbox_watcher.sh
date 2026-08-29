#!/usr/bin/env bash
# inbox_watcher.sh -- wait for Pat to say something, then get out of the way.
#
# The HUD inbox is a PULL queue with no push. A directive Pat types or dictates
# into the page only gets read when a converse call polls GET /inbox before its
# turn, or when a standby waiter is sitting on /standby. So during a long stretch
# of work -- agents dispatched, tools running, nobody polling -- his message just
# sits there. On 2026-08-26 a directive posted at 08:08:53 and was found by
# accident, long after it mattered.
#
# The fix is not for the model to poll (that burns a turn every two seconds).
# It is to run THIS in the background: it blocks cheaply in shell, and exits the
# moment there is something to read. A backgrounded Bash tool call re-invokes the
# session when its command exits, so the exit itself is the push notification --
# and the queued text is on stdout, so the waking session already has the
# directive without a second fetch.
#
# Usage (from the assistant, as a backgrounded Bash call):
#
#     ~/repos/voice-hud/inbox_watcher.sh [max-seconds]     # default 600
#
# Exit codes:
#   0  a directive is waiting -- its text is on stdout
#   0  timed out with an empty inbox -- also 0, so a quiet expiry never reads
#      as a failure in the launching harness; stdout says which case it was
#   2  the HUD server is not reachable -- do not spin against a dead port
#   3  another watcher already holds the pidfile; this one exits quietly
#
# It deliberately does NOT clear the inbox: whoever acts on the directive posts
# /inbox/clear, so the queue survives if this exit is missed and converse still
# picks the message up on the normal path.

set -u

HUD="http://127.0.0.1:8123"
INTERVAL=2          # seconds between polls -- responsive, and free
MAX_MISSES=3        # consecutive curl failures tolerated (a server restart)
MAX_SECONDS="${1:-600}"

case "$MAX_SECONDS" in
  ''|*[!0-9]*) echo "inbox_watcher: max-seconds must be a whole number" >&2; exit 2 ;;
esac

HUD_DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="$HUD_DIR/inbox_watcher.pid"

# Singleton lock, same shape as always_on_listener.py's: the file holds
# {"pid": N}, a lock held by a live process means exit quietly rather than
# stacking a second watcher, and a lock from a dead process is self-healing.
holder=$(sed -n 's/.*"pid"[[:space:]]*:[[:space:]]*\([0-9]*\).*/\1/p' "$PID_FILE" 2>/dev/null)
if [ -n "$holder" ] && kill -0 "$holder" 2>/dev/null; then
  echo "inbox_watcher: already running (pid $holder) -- exiting quietly" >&2
  exit 3
else
  printf '{"pid": %d}' "$$" > "$PID_FILE"
  trap 'rm -f "$PID_FILE"' EXIT
fi

# Render the queue as one line per directive; empty output means nothing waiting.
read_inbox() {
  python3 -c '
import json, sys, time
try:
    items = (json.load(sys.stdin) or {}).get("items") or []
except Exception:
    items = []
for i in items:
    ts = time.strftime("%H:%M:%S", time.localtime(i.get("ts") or 0))
    print("[%s] %s" % (ts, (i.get("text") or "").strip()))
' 2>/dev/null
}

deadline=$(( $(date +%s) + MAX_SECONDS ))
misses=0

while [ "$(date +%s)" -lt "$deadline" ]; do
  if body=$(curl -sf -m 2 "$HUD/inbox" 2>/dev/null); then
    misses=0
    queued=$(printf '%s' "$body" | read_inbox)
    if [ -n "$queued" ]; then
      echo "inbox_watcher: directive waiting in the HUD inbox --"
      printf '%s\n' "$queued"
      exit 0
    fi
  else
    misses=$(( misses + 1 ))
    if [ "$misses" -ge "$MAX_MISSES" ]; then
      echo "inbox_watcher: HUD server unreachable at $HUD -- giving up" >&2
      exit 2
    fi
  fi
  sleep "$INTERVAL"
done

echo "inbox_watcher: nothing queued after ${MAX_SECONDS}s -- watcher expiring"
exit 0
