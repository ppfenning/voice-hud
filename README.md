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

## Platform status

Written on macOS, now running on **Linux** (Ubuntu, GNOME/Wayland, PipeWire)
as well. The core (server, listener, wake guard, HUD) is portable; the
launchers pick their platform at runtime:

| Piece | macOS | Linux |
|---|---|---|
| Listener supervisor | `com.voicemode.always-on-listener.plist` (launchd) | `systemd/voice-hud-listener.service` (systemd --user) |
| HUD server | started by `launch.sh` via nohup | `systemd/voice-hud-server.service`; `launch.sh` starts the unit if it is down |
| Browser open | `open -a "Brave Browser"` | `brave-browser`, then `xdg-open` |
| HUD "spawn session" button | iTerm via osascript | Ptyxis, then `x-terminal-emulator` |
| Bluetooth A2DP guard | `ensure_a2dp.sh` (SwitchAudioSource) | no-op — PipeWire/WirePlumber keeps A2DP itself; revisit if a headset drops to HFP |

### Linux install

```bash
sudo apt install libportaudio2 ffmpeg
cd ~/repos/voice-hud && uv venv && uv pip install sounddevice numpy
mkdir -p ~/.config/systemd/user
cp systemd/*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now voice-hud-server voice-hud-listener
systemctl --user status voice-hud-listener      # must be active, or nothing is listening
```

Whisper and Kokoro come from voicemode's own service installers
(`voicemode whisper install --use-gpu`, `voicemode kokoro install`), which
register their own user units on the same ports this HUD expects.

### Pointing at remote STT/TTS

Both endpoints are env-overridable, so the speech services can live on
another host (a homelab box, say) while the listener and HUD stay wherever
the microphone is:

| Env var | Default |
|---|---|
| `VOICE_HUD_WHISPER_URL` | `http://127.0.0.1:2022/v1/audio/transcriptions` |
| `VOICE_HUD_KOKORO_URL` | `http://127.0.0.1:8880/v1` |

Set them in the systemd units (`Environment=`) or the launchd plist. voicemode
has its own equivalents (`VOICEMODE_STT_BASE_URLS`, `VOICEMODE_TTS_BASE_URLS`)
that must be set alongside, or the HUD and the conversation will use different
servers.
