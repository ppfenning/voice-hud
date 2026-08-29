# voice-hud

A local, always-on voice assistant and glanceable heads-up display. Wake word,
speech-to-text, spoken responses, and a browser HUD showing current work,
inbox, and due items.

Everything runs on localhost. Nothing is sent anywhere except the optional
task-tracker integration, and that is off unless you configure it.

## Pieces

| File | Role |
|---|---|
| `server.py` | HUD web server + API on `:8123`; renders `index.html` |
| `always_on_listener.py` | Continuous capture, wake-word gating, transcription |
| `wake_guard.py` | Wake-word decision logic (+ `test_wake_guard.py`) |
| `calibrate_gate.py` | Tunes the wake gate against your room and mic |
| `wake_listener.py` | Thin wake-only listener |
| `inbox_watcher.sh` | Watches the inbox file and notifies |
| `ensure_a2dp.sh` | Forces Bluetooth headsets back to A2DP (macOS) |

## Dependencies

Local services, expected on these ports:

- **Whisper** STT at `127.0.0.1:2022`
- **Kokoro** TTS at `127.0.0.1:8880`
- `voicemode` event logs at `~/.voicemode/logs/events/`

Python: `sounddevice`, `numpy`. Everything else is stdlib.

## Configuration

All optional. Unset means the feature quietly disables itself rather than
erroring.

| Env var | Effect |
|---|---|
| `ASANA_API_KEY` | Enables the task widgets. Unset → they render "unavailable" |
| `ASANA_WORKSPACE_GID` | Workspace to query |
| `ASANA_USER_GID` | Whose assigned tasks the due-soon widget shows |
| `VOICE_HUD_WATCH_PROJECT` | Project dir whose Claude Code sessions feed the worklog (default `~/repos`) |

No identifiers are hardcoded. The tracker integration is generic — it matches
on project *names*, so it works against any workspace.

## ⚠️ Platform status

Written on macOS, currently running on **Linux/WSL**. The core (server,
listener, wake guard, HUD) is portable, but the launchers are not yet:

- `com.voicemode.always-on-listener.plist` is a launchd job. Templated with
  `__VOICE_HUD_DIR__` / `__VOICEMODE_PYTHON__` / `__HOME__` placeholders, but a
  systemd unit is the real answer on Linux.
- `ensure_a2dp.sh` uses macOS Bluetooth tooling; the PipeWire/PulseAudio
  equivalent is unwritten.
- `launch.sh` opens Brave with a macOS-shaped invocation.

Audio capture through WSL needs attention too — `sounddevice` needs a working
path to the host's microphone.

**Port checklist:** systemd unit · A2DP script for PipeWire · `launch.sh`
browser invocation · verify capture through WSL.
