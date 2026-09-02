# voice-hud

A local, always-on voice loop for a [Claude Code](https://claude.com/claude-code)
session, with a glanceable browser HUD. A wake-word listener owns the
microphone, a stdlib-only HTTP server derives live state from
[voicemode](https://github.com/mbailey/voicemode)'s event log and serves the
page, and the page shows what the session is saying, what it is running, and
whether anything is actually listening.

Everything runs on localhost. Nothing leaves the machine except calls to the
speech services you point it at (Whisper-compatible STT, Kokoro-compatible
TTS), which default to localhost too.

## How it fits together

```
 mic ─▶ always_on_listener ─▶ Whisper (STT) ─▶ wake_guard ─▶ POST /inbox ─▶ Claude Code
              │ heartbeat                                          ▲          session polls
              ▼                                                    │          /inbox, /mute
           server  ◀── ~/.voicemode/logs/events/*.jsonl            │
              │                                                    │
              ▼                                                    │
      static/index.html (HUD on :8123) ── typed / dictated directives
                       └─▶ Kokoro (TTS) to replay a spoken line
```

| Path | Role |
|---|---|
| `voice_hud/server.py` | HTTP server on `127.0.0.1:8123`. Serves the HUD, exposes the API below, derives status (speaking / listening / standby) from voicemode's event log. Stdlib only. |
| `voice_hud/always_on_listener.py` | Continuous capture, energy gate, wake-phrase match, transcription of what follows, POST to the inbox. Writes a heartbeat carrying capture evidence so the HUD can tell "alive" from "can actually hear". |
| `voice_hud/wake_guard.py` | The tiered directive gate that keeps Whisper's hallucinated filler ("Thank you.") out of the inbox. Pure decision logic, covered by `tests/`. |
| `voice_hud/calibrate_gate.py` | Measures the gate's acoustic thresholds against your room and microphone. |
| `voice_hud/wake_listener.py` | The older standby-only listener, kept for reference. |
| `voice_hud/static/index.html` | The HUD. One file, no build step. |
| `scripts/launch.sh` | Idempotent: start the server if it is down, open the page once per session. Meant to run as a Claude Code hook (below). |
| `scripts/inbox_watcher.sh` | Blocks until a directive lands in the inbox, then exits with it on stdout. Run it as a backgrounded shell call to turn the pull-queue into a push. |
| `scripts/ensure_a2dp.sh` | macOS: re-select the preferred Bluetooth headset and bounce it out of HFP before a voice turn. |
| `scripts/sitecustomize-portaudio-latency.py` | Drop into voicemode's site-packages to default PortAudio to high latency on docks that crackle. |
| `deploy/systemd/` | User units supervising the server and the listener on Linux. |
| `deploy/launchd/` | The equivalent launchd plist for macOS. |

## Requirements

- Python 3.12+. The server needs nothing else; the listener needs
  `sounddevice` and `numpy` (and `libportaudio2` on Linux).
- Whisper-compatible STT at `VOICE_HUD_WHISPER_URL` and Kokoro-compatible TTS
  at `VOICE_HUD_KOKORO_URL`. voicemode's installers (`voicemode whisper
  install`, `voicemode kokoro install`) provide both on the default ports.
- voicemode itself, for the conversation. The HUD reads its event log and
  talks to its control socket to cut or skip speech.

## Install

### Linux (systemd --user)

```bash
sudo apt install libportaudio2 ffmpeg
git clone https://github.com/ppfenning/agent-voice-hud ~/repos/voice-hud
cd ~/repos/voice-hud
uv venv && uv pip install -e ".[listener]"
mkdir -p ~/.config/systemd/user
cp deploy/systemd/*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now voice-hud-server voice-hud-listener
systemctl --user status voice-hud-listener      # must be active, or nothing is listening
```

The units assume the checkout lives at `~/repos/voice-hud` and that the
listener's venv is `.venv/` inside it; edit `WorkingDirectory` and
`ExecStart` otherwise.

### macOS (launchd)

Fill in the placeholders in `deploy/launchd/com.voicemode.always-on-listener.plist`
(`__VOICE_HUD_DIR__` is the checkout, `__VOICEMODE_PYTHON__` any python with
`sounddevice` and `numpy`), copy it to `~/Library/LaunchAgents/`, and
`launchctl bootstrap gui/$(id -u) <that path>`. The server is started by
`scripts/launch.sh` on demand, or by hand with `python3 -m voice_hud.server`.

### Open the HUD with every voice turn

Add a Claude Code `PreToolUse` hook on voicemode's converse tool. In
`~/.claude/settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "mcp__.*voicemode.*__converse",
        "hooks": [{ "type": "command", "command": "$HOME/repos/voice-hud/scripts/launch.sh" }]
      }
    ]
  }
}
```

## Configuration

All optional. Unset means the default, never an error.

| Env var | Default | Effect |
|---|---|---|
| `VOICE_HUD_STATE_DIR` | `~/.local/state/voice-hud` | Where runtime state lives: flags, inbox, heartbeat, pid files. Safe to delete. |
| `VOICE_HUD_WATCH_PROJECT` | `~/repos` | Project directory whose Claude Code sessions feed the worklog panel. |
| `VOICE_HUD_WHISPER_URL` | `http://127.0.0.1:2022/v1/audio/transcriptions` | STT endpoint (listener, server, calibrator). |
| `VOICE_HUD_KOKORO_URL` | `http://127.0.0.1:8880/v1` | TTS endpoint, used to replay comms lines. |
| `VOICE_HUD_TZ` | `America/New_York` | Timezone for the timestamps the HUD shows. |
| `VOICE_HUD_HEADSET` | unset | macOS only: the output device `ensure_a2dp.sh` re-selects before a voice turn. |
| `VOICEHUD_DIRECTIVE_GATE` | `full` | `off` keeps only the legacy phrase blocklist, `heuristic` skips the LLM tier. Writing `{"mode": ...}` to `gate_mode.json` in the state dir does the same for an already-running listener. |
| `STANDBY_IDLE_SECONDS` | see `server.py` | Idle time before the session is auto-marked standby. |
| `VOICEMODE_CONTROL_SOCKET`, `VOICEMODE_TTS_SPEED` | voicemode's | Passed through so cut/skip and replay match the live conversation. |

If the speech services live on another host, set the two URL variables in the
units or the plist **and** voicemode's own `VOICEMODE_STT_BASE_URLS` /
`VOICEMODE_TTS_BASE_URLS`, or the HUD and the conversation will use different
servers.

## HTTP API

The full contract, with the reasoning behind each field, is the docstring at
the top of `voice_hud/server.py`. In short:

| Route | Purpose |
|---|---|
| `GET /` | The HUD. |
| `GET /state.json` | Everything the page renders: status, current voice, comms lines, telemetry, active ops, listener heartbeat, playback. |
| `GET/POST /mute` | Quiet mode. Muted means no TTS **and** no listening; the server enforces it, not just the page. |
| `GET/POST /standby` | Idle flag for background waiters. |
| `GET/POST /tasks` | The "active ops" list a session publishes about its agents, with server-side liveness probing via each item's `heartbeat_file`. |
| `GET/POST /inbox`, `POST /inbox/clear` | Directives from the page or the wake listener; the session polls before each turn. |
| `POST /inbox/audio` | A WAV body: rejected as silence or filler by the same gate as the ambient wake, otherwise transcribed and queued. |
| `POST /say` | Append a text-only assistant line while muted. |
| `POST /replay` | Re-synthesise one comms line through Kokoro and return it as audio. Refuses while muted. |
| `POST /skip` | Cut the utterance being spoken right now without muting. |
| `POST /spawn` | Open a terminal running a plain-text Claude session. |
| `GET /health` | `ok`. |

## Development

```bash
uv venv && uv pip install -e ".[dev]"
pytest -q
```

The wake guard's tests are pure and offline; the handful that exercise the
transcription tier skip themselves when no Whisper server answers.

## License

MIT — see [LICENSE](LICENSE).
