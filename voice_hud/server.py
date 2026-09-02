#!/usr/bin/env python3
"""Voice HUD server — serves the suit-interface page and derives live state
from voicemode's own event log (~/.voicemode/logs/events/*.jsonl).

Endpoints:
  GET  /            -> index.html
  GET  /state.json  -> {status, voice, requested_voice, lines, telemetry,
                        listening} (listening.alive = always_on_listener.py
                        heartbeat within LISTENING_ALIVE_SECONDS)
  GET  /voice       -> current requested voice override (for the Claude session to poll)
  POST /voice       -> set requested voice override  {"voice": "af_bella"}
  GET  /mute        -> {"muted": bool} (Claude polls before every converse; muted
                       means no TTS AND no listening — text-only replies)
  POST /mute        -> set mute state  {"muted": true}
  GET  /standby     -> {"standby": bool} (background-only signal, not a UI mode —
                       see read_standby(); background waiters still consult it,
                       e.g. wake_listener.py's standby-gated loop)
  POST /standby     -> set standby state {"standby": true} — always honored as
                       written; read_standby()'s auto-idle logic only takes over
                       on a LATER read, so an explicit write here is never
                       immediately clobbered
  GET  /cast        -> {"seats": [{name, voice, model, lane, surface}], "dir",
                       "unavailable"} the standing agent cast, read from the
                       seat definition files in VOICE_HUD_CAST_DIR (default
                       ~/.claude/agents — see read_cast); also on state.json
                       as "cast" so the reticle can draw the org ring
  GET  /tasks       -> {"items": [{id, label, status, ts, persona?, detail?,
                       heartbeat_file?, liveness, liveness_age}]} active ops
                       shown on the HUD; ts is server-stamped on each status
                       change (see stamp_tasks) — persona/detail/heartbeat_file
                       are whatever the session last posted, if anything.
                       liveness/liveness_age are SERVER-COMPUTED (see
                       task_liveness), not caller input — ignored if posted.
  POST /tasks       -> replace the ops list {"items": [{id, label, status,
                       persona?, detail?, heartbeat_file?}]} (session is the
                       writer). persona (str, optional) = the agent's assigned
                       voice-name, e.g. "Emma"; detail (str, optional) = a
                       sentence describing what it's doing. heartbeat_file
                       (str, optional, added 08-18 round 2) = absolute path to
                       a file the running agent/task itself appends to (e.g. a
                       background Agent/Bash task's own .output file under
                       /private/tmp/.../tasks/<id>.output) — its mtime is a
                       REAL liveness probe (see task_liveness), same standard
                       as bridge_alive() for plan bridges. The server only
                       ever stat()s this path; it is NEVER opened or read.
                       Without it, a "running" item can only ever be reported
                       liveness:"unknown" — self-reported, unconfirmable —
                       never confidently "running" or "orphaned" on posting
                       cadence alone (that was the round-1 bug: it measured
                       how recently the ORCHESTRATOR re-posted, not whether
                       the agent was alive). All three added fields are
                       OPTIONAL and purely additive — items posted without
                       them render exactly as before; ts is always
                       server-stamped, never taken from the caller.
                       heartbeat_file, once set, survives a later POST that
                       omits it (see stamp_tasks) so an unrelated field edit
                       can't silently drop the liveness handle.
  GET  /inbox       -> {"items": [{text, ts}]} typed directives from the page; the
                       Claude session polls before each converse and clears after acting
  POST /inbox       -> append a directive {"text": "..."}; counts as activity for
                       the auto-idle standby timer (see read_standby())
  POST /inbox/audio -> accept a raw WAV body (Content-Type audio/wav, <=10MB),
                       reject it as noise if it's near-silent (wav_bytes_rms) or
                       whisper's transcript fails wake_guard.judge_directive()'s
                       tiered gate (phrase blocklist, then the decoder's own
                       confidence tells, then a fast accept for substantive text,
                       then `claude -p` for the ambiguous residue) — the same
                       guard always_on_listener.py applies to the ambient wake —
                       otherwise forward to the local whisper server and enqueue
                       the resulting text through the same path as POST /inbox.
                       Returns {"ok": bool, "text": str, "tier": str, "reason": str}
                       — tier "silence" is the RMS gate (nothing was said at
                       all); any other tier on ok:false is the directive gate
                       rejecting a transcript it DID hear, and reason says why.
                       The HUD labels those two differently on purpose.
  POST /inbox/clear -> empty the inbox
  POST /skip        -> cut the utterance voicemode is speaking RIGHT NOW and let
                       the session carry on, WITHOUT muting (skip_forward on
                       voicemode's control socket). Mute state is NOT touched.
                       Returns {"ok": bool, "error": str}; ok:false with
                       "nothing playing" just means nothing was speaking.
  POST /say         -> {"text": "...", "persona": "Jarvis"?} append a TEXT-ONLY
                       assistant reply (quiet mode — the owner muted, so the session
                       never calls the voice tool; the reply must still land in
                       the comms feed). Timestamped server-side, persisted to
                       says.json (same durability pattern as inbox_history.json),
                       merged chronologically into /state.json's `lines` as
                       who="say". NEVER triggers TTS. Returns {"ok": true}
  POST /replay      -> {"text": "...", "voice": "bm_jarvis"?, "persona": "Emma"?}
                       re-synthesise one comms line through the LOCAL kokoro
                       and return it as audio/mpeg, so the page can replay a
                       line whose audio is long gone (the HUD stores text
                       only). `voice` is a spoken line's literal event-log
                       voice id and always wins; `persona` is a text-only
                       POST /say line's display name, resolved back to its
                       kokoro voice BY NAME — replaying in the original
                       persona is the point, since that is how the owner tells
                       whose finding a line was. Synthesised at
                       VOICEMODE_TTS_SPEED so a 1x replay matches how the
                       line actually sounded; the page's 1x/1.5x/2x toggle
                       multiplies on top of it via playbackRate.
                       REFUSES WITH 409 WHILE MUTED — quiet mode is a safety
                       control and replay is not a route around it. 503 with
                       {"ok": false, "reason": ...} if kokoro is unreachable.
                       Clips are cached in-process (see cached_speech).
  POST /spawn       -> open a new iTerm window running a plain-text claude session
  GET  /health      -> ok

Stdlib only. Binds 127.0.0.1:8123.
"""
import io
import json
import os
import sys
import shutil
import re
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import wave
from array import array
from datetime import date, datetime, timedelta
from functools import reduce
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

from voice_hud.paths import STATE_DIR
from voice_hud.wake_guard import (
    RMS_GATE,
    decoder_signals,
    describe,
    dictation_outcome,
    safe_judge_directive,
    silence_outcome,
    transcript_text,
)

PORT = 8123
STATIC_DIR = Path(__file__).resolve().parent / "static"
HUD_DIR = STATE_DIR  # runtime state lives outside the package; see voice_hud.paths
EVENTS_DIR = Path.home() / ".voicemode" / "logs" / "events"
# Claude Code slugifies the watched project path by replacing "/" with "-".
# Derive it rather than hardcoding a username and OS layout — the old literal
# was a macOS path and resolved to nothing after the move to Linux, which
# degrades silently (the worklog just renders empty).
SESSIONS_ROOT = Path.home() / ".claude" / "projects"
WATCH_PROJECT = Path(os.environ.get("VOICE_HUD_WATCH_PROJECT", Path.home() / "repos"))
SESSIONS_DIR = SESSIONS_ROOT / str(WATCH_PROJECT).replace("/", "-")
# WORKLOG ONLY. This used to feed the comms-feed spoken-text correlation as
# well, which is what made that correlation silently degrade on long
# sessions — see spoken_call_texts(), which now scans the whole transcript
# incrementally and no longer depends on any window at all. The worklog
# genuinely does only want recent lines (it renders the last 25), so a byte
# tail is the right shape for it and this bound is not load-bearing for
# anything else.
WORKLOG_TAIL_BYTES = 2_000_000
OVERRIDE_FILE = HUD_DIR / "voice_override.json"
MUTE_FILE = HUD_DIR / "mute.json"
STANDBY_FILE = HUD_DIR / "standby.json"
# ---- voicemode control channel (08-19) --------------------------------
# The running voicemode server binds this Unix socket WHILE IT SPEAKS and
# reads newline-delimited JSON commands from it, unlinking it the moment the
# utterance ends. So a MISSING socket is the ordinary nothing-is-speaking
# case, never a fault -- that distinction is what keeps the log quiet.
# Measured here 08-19 against voicemode 8.12.0p0, cutting ~3s into a ~40s
# passage (both landed 0.44s after the write):
#   {"command":"stop","hint":"quiet"} -- cuts playback AND returns converse
#       with a "[control: stop] user asked you to stop talking for now"
#       marker, so the session drops to text-only replies (POST /say).
#       That is MUTE.
#   {"command":"skip_forward"}        -- cuts playback just as fast but
#       returns an ORDINARY result carrying no marker, so the session simply
#       advances to its listen turn. That is SKIP.
# We speak the socket protocol directly instead of shelling out to
# `voicemode control ...` because mute is a safety control: a ~0.5s Python
# CLI cold start sits right on the path whose latency the owner is complaining
# about, and subprocess failure modes are worse than a 0.6s socket timeout.
CONTROL_SOCKET = Path(
    os.environ.get("VOICEMODE_CONTROL_SOCKET")
    or Path.home() / ".voicemode" / "control.sock"
)
CONTROL_TIMEOUT_SECONDS = 0.6
LISTENING_HEARTBEAT_FILE = HUD_DIR / "listening_heartbeat.json"
LISTENING_MISSED_SPEECH_SECONDS = 300  # how recent voicemode's own STT has
                               # to be for its disagreement with the wake
                               # listener to still be worth reporting
LISTENING_CAPTURE_STALE_SECONDS = 75  # no chunk carrying REAL audio for this
                               # long => the daemon is up but deaf (DEGRADED,
                               # not ARMED). Comfortably above the daemon's
                               # own DEAF_REBIND_SECONDS=60 so a rebind gets
                               # its chance to fix things before the page
                               # starts complaining, and far above the ~2.4s
                               # normal gap between successful reads.
LISTENING_ALIVE_SECONDS = 15  # always_on_listener.py touches this every loop
                               # tick (even while paused for mute/speaking);
                               # stale past this TTL reads as daemon-down
TASKS_FILE = HUD_DIR / "tasks.json"
INBOX_FILE = HUD_DIR / "inbox.json"
INBOX_HISTORY_FILE = HUD_DIR / "inbox_history.json"
SAYS_FILE = HUD_DIR / "says.json"
SAYS_STORED = 100   # durability cap, matches inbox_history
SAYS_SERVED = 50    # feed-payload cap — only the newest slice reaches /state.json
MAX_EVENTS = 800
EASTERN = ZoneInfo(os.environ.get("VOICE_HUD_TZ", "America/New_York"))
WHISPER_TRANSCRIBE_URL = os.environ.get("VOICE_HUD_WHISPER_URL", "http://127.0.0.1:2022/v1/audio/transcriptions")
MAX_AUDIO_BYTES = 10_000_000

STATUS_BY_EVENT = {
    "TTS_START": "speaking",
    "TTS_PLAYBACK_START": "speaking",
    "TTS_FIRST_AUDIO": "speaking",
    # TTS_PLAYBACK_END was emitted by voicemode but NOT mapped here, so
    # "speaking" persisted from first audio all the way to TOOL_REQUEST_END
    # — i.e. for the whole converse() tool call, not the whole utterance.
    # Measured over 2026-08-18's event log that stretched the always-on
    # listener's deaf window to as long as 71s per reply, since it gates on
    # status == "speaking". Ending the window when the audio actually ends
    # is both more truthful and strictly safer than it sounds: the daemon
    # still holds SPEAKING_COOLDOWN_SECONDS after the gate lifts, which is
    # the guard that actually stops trailing TTS self-triggering a wake.
    "TTS_PLAYBACK_END": "standby",
    "RECORDING_START": "listening",
    "RECORDING_END": "processing",
    "STT_START": "processing",
    "STT_COMPLETE": "standby",
    "TOOL_REQUEST_END": "standby",
    "SESSION_END": "standby",
}

VOICE_ROSTER = (
    "af_bella", "af_heart", "af_nicole", "af_sky", "af_nova", "af_sarah",
    "af_aoede", "af_river", "bf_emma", "am_michael", "bm_fable", "bm_george",
)


# ---- the cast: who the seats are, read from their own definitions ----------
# The HUD draws the org as a standing ring around the reticle. It does not
# carry a roster of its own: the seats are the agent definition files a
# session loads (`~/.claude/agents/*.md`, or wherever VOICE_HUD_CAST_DIR
# points), so adding a seat there adds it here. Only frontmatter and one
# regex over the body are read — name, tools, model, and the first
# backticked voice id — and nothing here is required: a missing directory is
# "unavailable", not an error.
CAST_DIR = Path(os.environ.get("VOICE_HUD_CAST_DIR", Path.home() / ".claude" / "agents")).expanduser()
CAST_CACHE_SECONDS = 30
_WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
_VOICE_RE = re.compile(r"`([a-z]{2}_[a-z]+)`")
_cast_cache = {"at": 0.0, "value": None}


def parse_seat(text: str) -> dict | None:
    """Pure: one seat from one definition file's text, or None if it has no frontmatter."""
    if not text.startswith("---\n"):
        return None
    parts = text.split("\n---\n", 1)
    if len(parts) != 2:
        return None
    front, body = parts[0][4:], parts[1]
    fields: dict = {}
    key = None
    for line in front.splitlines():
        if line.startswith((" ", "\t")) and key:
            fields[key] = (fields[key] + " " + line.strip()).strip()
        elif ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            fields[key] = value.strip().lstrip("|>").strip()
    name = fields.get("name")
    if not name:
        return None
    tools = [t.strip() for t in fields.get("tools", "").split(",") if t.strip()]
    voice = _VOICE_RE.search(body)
    description = fields.get("description", "")
    surface = re.split(r"[.:]\s", description, maxsplit=1)[0][:90]
    return {
        "name": name,
        "voice": voice.group(1) if voice else "",
        "model": fields.get("model", ""),
        "lane": "writes" if any(t in _WRITE_TOOLS for t in tools) else "reads",
        "surface": surface,
    }


def fetch_cast() -> dict:
    if not CAST_DIR.is_dir():
        return {"seats": [], "dir": str(CAST_DIR), "unavailable": True}
    seats = []
    for path in sorted(CAST_DIR.glob("*.md")):
        try:
            seat = parse_seat(path.read_text(encoding="utf-8"))
        except OSError:
            seat = None
        if seat:
            seats.append(seat)
    return {"seats": seats, "dir": str(CAST_DIR), "unavailable": False}


def read_cast() -> dict:
    """The cast, re-read at most every CAST_CACHE_SECONDS: state.json polls every second."""
    now = time.time()
    if _cast_cache["value"] is not None and now - _cast_cache["at"] < CAST_CACHE_SECONDS:
        return _cast_cache["value"]
    _cast_cache.update(at=now, value=fetch_cast())
    return _cast_cache["value"]


def read_events() -> tuple:
    files = sorted(EVENTS_DIR.glob("voicemode_events_*.jsonl"))
    lines = files[-1].read_text().splitlines()[-MAX_EVENTS:] if files else ()
    return tuple(
        e for e in (parse_json(line) for line in lines) if e is not None
    )


def parse_json(line: str):
    try:
        return json.loads(line)
    except ValueError:
        return None


def read_override() -> dict:
    return (
        parse_json(OVERRIDE_FILE.read_text()) or {}
        if OVERRIDE_FILE.exists()
        else {}
    )


def read_mute() -> dict:
    stored = (
        parse_json(MUTE_FILE.read_text()) or {}
        if MUTE_FILE.exists()
        else {}
    )
    return {"muted": bool(stored.get("muted"))}


def log_line(message: str) -> None:
    """The server's only logging surface: launch.sh redirects stdout+stderr
    to /tmp/voice-hud.log, so a flushed print IS the log."""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)
    return None


def send_control(command: str, hint: str = "") -> dict:
    """Write one newline-delimited JSON command to voicemode's control
    socket. Fire-and-forget -- the server applies it and writes nothing
    back. Returns a Result-shaped dict and NEVER raises: every caller is on
    a path that has to survive the control channel being unavailable."""
    body = {"command": command} if not hint else {"command": command, "hint": hint}
    line = (json.dumps(body) + "\n").encode()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(CONTROL_TIMEOUT_SECONDS)
            sock.connect(str(CONTROL_SOCKET))
            sock.sendall(line)
        return {"ok": True, "command": command, "error": ""}
    except (FileNotFoundError, ConnectionRefusedError):
        return {"ok": False, "command": command, "error": "nothing playing"}
    except OSError as exc:
        return {"ok": False, "command": command, "error": f"{type(exc).__name__}: {exc}"}


def cut_speech_for_mute(muted: bool) -> dict:
    """Going muted CUTS the utterance already in flight (owner, 08-19: "if I
    hit mute when you're in the middle of a conversation, could you please
    stop talking"). Letting the sentence finish is exactly wrong when the
    reason for muting is that someone walked in.

    Fires on every muted=True write rather than only on the false->true
    edge: re-cutting when nothing is playing is a harmless no-op, whereas
    skipping the cut because mute.json already read true would leave audio
    running. For a safety control that asymmetry settles it.

    NEVER propagates failure -- the mute flag is already on disk by the time
    this runs, so an unreachable control channel costs the cut, not the
    mute."""
    if not muted:
        return {"ok": True, "command": "", "error": ""}
    else:
        result = send_control("stop", hint="quiet")
        if result["ok"] or result["error"] == "nothing playing":
            return result
        else:
            log_line(
                f"mute: could not cut in-flight speech ({result['error']}) "
                "-- mute itself still applied"
            )
            return result


def skip_current_utterance() -> dict:
    """Cut the in-flight utterance WITHOUT muting (owner, 08-19: "skip to the
    end of a sentence when you're speaking ... don't need the full
    playthrough"). skip_forward rather than stop precisely because it
    carries no hint: converse returns an ordinary result and the session
    just moves on, where a stop would tell it to go quiet instead."""
    result = send_control("skip_forward")
    if result["ok"] or result["error"] == "nothing playing":
        return result
    else:
        log_line(f"skip: could not cut in-flight speech ({result['error']})")
        return result


def read_playback(events: tuple) -> dict:
    """The assistant's own most recent TTS playback interval, in epoch
    seconds, taken from the EVENT LOG rather than inferred from the status
    field. always_on_listener.py uses this to refuse any wake match whose
    audio window sits inside our own speech, so that self-triggering is
    structurally impossible rather than merely unlikely.

    Why a second mechanism when the listener already pauses on
    status == "speaking": that pause fails OPEN. Its hud_get() returns {}
    on any error, which reads as "not speaking", so the entire gate quietly
    disappears whenever this server is unreachable — including the seconds
    around every restart of it. Measured 2026-08-18, the pause itself is
    working (0 above-gate captures during 1626s of playback across 37
    intervals), so this is not a fix for an observed leak; it is a guard on
    the path that has no gate at all when the gate is down."""
    starts = tuple(iso_epoch(e.get("timestamp") or "") or 0
                   for e in events if e.get("event_type") == "TTS_PLAYBACK_START")
    ends = tuple(iso_epoch(e.get("timestamp") or "") or 0
                 for e in events if e.get("event_type") == "TTS_PLAYBACK_END")
    start = max(starts, default=0)
    end = max(ends, default=0)
    active = bool(start and start > end)
    return {"start": start, "end": None if active else end, "active": active}


def read_listening(events: tuple = ()) -> dict:
    """Whether always_on_listener.py's continuous wake daemon is alive — the
    HUD's always-visible WAKE LISTENER indicator, and what the status line's
    LISTENING state is actually keyed off of (owner, 08-17: a false "armed"
    reading is worse than none, so the page must say OFFLINE the moment this
    goes stale, never keep claiming armed).

    08-18: `alive` alone was still a false ARMED, just a subtler one. It only
    ever meant "that process's loop is turning", and the loop turns just as
    happily while the daemon is bound to a mic that is no longer routed and
    is handing back digital silence — which is exactly how "hey Jarvis"
    stopped working mid-session behind a green indicator. So the heartbeat
    now carries capture evidence (see always_on_listener.touch_heartbeat())
    and this reports THREE states, not two:
        alive=False              -> OFFLINE  (nothing is listening)
        alive, no recent capture -> DEGRADED (process up, hearing nothing)
        alive + recent capture   -> ARMED    (genuinely listening)
    Same principle as before, applied one layer deeper: the page must never
    claim armed on the strength of a fact that doesn't mean armed."""
    stored = (
        parse_json(LISTENING_HEARTBEAT_FILE.read_text()) or {}
        if LISTENING_HEARTBEAT_FILE.exists()
        else {}
    )
    now = time.time()
    alive = (now - (stored.get("ts") or 0)) < LISTENING_ALIVE_SECONDS
    capture_ts = stored.get("capture_ts") or 0
    gate = stored.get("gate")
    peak = stored.get("peak_rms")
    above_gate_ts = stored.get("above_gate_ts") or 0
    # Ground truth that the owner was speaking, from the OTHER consumer of the same
    # microphone: voicemode's own recorder transcribed something. If it did,
    # and the always-on listener never once reached its usable threshold in
    # that window, the listener missed speech it should have heard — the two
    # processes share a mic, so that is a real fault rather than a quiet
    # room. This cross-check is what makes "below usable level" reportable
    # WITHOUT false-alarming every time the owner simply isn't talking, which a
    # bare "nothing above the gate lately" test would do constantly.
    heard_elsewhere = max(
        (iso_epoch(e.get("timestamp") or "") or 0
         for e in events if e.get("event_type") == "STT_COMPLETE"),
        default=0,
    )
    # Only speech from AFTER this daemon started counts. A freshly restarted
    # listener has no above-gate observation yet, and blaming it for an
    # utterance that predates it would make every restart look broken.
    started_ts = stored.get("started_ts") or 0
    missed_speech = bool(
        heard_elsewhere
        and (now - heard_elsewhere) < LISTENING_MISSED_SPEECH_SECONDS
        and heard_elsewhere > above_gate_ts
        and heard_elsewhere > started_ts
    )
    # A heartbeat with no capture_ts key at all is a pre-08-18 daemon still
    # running the old format: report it alive but capture-unknown rather
    # than inventing a green light for it.
    capturing = bool(capture_ts) and (now - capture_ts) < LISTENING_CAPTURE_STALE_SECONDS
    reason = (
        "" if not alive
        else "no audio arriving" if not capturing
        else "voicemode transcribed speech this listener never heard above its wake threshold"
        if missed_speech
        else ""
    )
    return {
        "alive": alive,
        "capturing": bool(alive and capturing),
        "degraded": bool(alive and (not capturing or missed_speech)),
        "gate": gate,
        "peak_rms": peak,
        # How close the loudest recent chunk got to being transcribable at
        # all. This is the number that was missing: the daemon can report
        # healthy capture forever while this sits at 0.003x.
        "gate_ratio": round(peak / gate, 4) if (gate and peak is not None) else None,
        "level_window": stored.get("level_window"),
        "missed_speech": missed_speech,
        "degraded_reason": reason,
        "device": stored.get("device"),
        "rms": stored.get("rms"),
        "mode": stored.get("mode"),
        "rebinds": stored.get("rebinds"),
        "reason": stored.get("reason") or "",
        "silent_seconds": round(now - capture_ts) if capture_ts else None,
    }


FINISHED_TTL_SECONDS = 600

# ROUND 2 (2026-08-18, owner/coordinator caught this): the FIRST version of
# this fix stamped a `seen` heartbeat on every /tasks POST that touched an
# item, orphaned-flagged whatever hadn't been touched in a while. That
# measures how recently the ORCHESTRATOR re-posted the fleet, not whether
# the underlying agent is alive — the exact assert-without-verifying bug
# this whole pass exists to eliminate. Proof it was wrong in both
# directions on the SAME live fleet: `sarah2`, a genuinely healthy
# 31-minute-old agent, got flagged orphaned purely because nobody had
# re-POSTed her; `sky3` kept reading "running" long after actually
# finishing, for the identical reason (nothing re-posted it either).
#
# The real fix needs a probe that checks the AGENT, not the orchestrator's
# posting rhythm — same standard as bridge_alive() for plan bridges: a
# genuine external fact, not a self-report. `heartbeat_file` (see the /tasks
# docstring above) is that probe: an absolute path to a file the running
# agent/task itself appends to, whose mtime is real evidence of activity.
# task_liveness() below stat()s it — nothing more, never opens/reads it.
#
# HEARTBEAT_STALE_SECONDS=20min, from real ceilings rather than posting
# cadence: the Bash tool's own documented cap is 600s (10 min) for a SINGLE
# call, and a healthy agent can legitimately go a full one of those without
# touching its own output file even once (one long grep/fetch/build with no
# interim output). Doubling that ceiling gives a whole second long call's
# worth of headroom before silence reads as death. Corroborated by real
# data the day this was written: sarah2's actual heartbeat file, ~31
# minutes into a real task, had a last-write only ~146s old — two orders of
# magnitude under this threshold during completely normal operation, so the
# threshold has real room to spare rather than being a near-miss.
HEARTBEAT_STALE_SECONDS = 20 * 60


def read_tasks_raw() -> list:
    stored = (
        parse_json(TASKS_FILE.read_text()) or {}
        if TASKS_FILE.exists()
        else {}
    )
    items = stored.get("items")
    return items if isinstance(items, list) else []


def heartbeat_age(path):
    """Seconds since `path` was last modified, or None if it can't be
    stat'd (missing, permission error, not a real path). NEVER opens or
    reads the file — mtime only, the same bare-probe discipline as
    bridge_alive()'s TCP connect. This is a liveness PROBE, not a peek at
    the (potentially enormous) subagent transcript it points at."""
    try:
        return time.time() - os.stat(path).st_mtime
    except (OSError, TypeError, ValueError):
        return None


def task_liveness(item: dict, now: float) -> dict:
    """VERIFIED vs ASSUMED for one 'running' op — see HEARTBEAT_STALE_SECONDS
    for why this is no longer POST-recency-based. Adds `liveness` (only
    meaningful when status=="running", else None) and `liveness_age`
    (seconds, real evidence age):
      - no `heartbeat_file` at all -> "unknown". There is nothing to probe,
        so this MUST NOT read as confidently alive (that was the round-1
        bug) — it's a pure self-report, same trust level as before this
        whole pass existed, just now visibly flagged as unconfirmed rather
        than silently presented as verified.
      - heartbeat_file stat()s fresh (age <= HEARTBEAT_STALE_SECONDS) ->
        "verified": real, current evidence the worker is still touching
        its own output.
      - heartbeat_file given but stale, OR the stat() itself fails (file
        missing/unreadable) -> "orphaned": a real probe was attempted and
        came back negative — same standard as bridge_alive() returning
        False for a plan. A missing file has no mtime to measure staleness
        from, so `liveness_age` falls back to time-since-`ts` in that one
        sub-case, purely so there's an honest number to show and a basis
        for read_tasks() to eventually let the ghost age off."""
    if item.get("status") != "running":
        return {**item, "liveness": None, "liveness_age": None}
    else:
        path = item.get("heartbeat_file")
        if not path:
            return {**item, "liveness": "unknown", "liveness_age": None}
        else:
            probe_age = heartbeat_age(path)
            effective_age = probe_age if probe_age is not None else (now - (item.get("ts") or now))
            return (
                {**item, "liveness": "verified", "liveness_age": round(effective_age)}
                if probe_age is not None and probe_age <= HEARTBEAT_STALE_SECONDS
                else {**item, "liveness": "orphaned", "liveness_age": round(effective_age)}
            )


def read_tasks() -> dict:
    """Finished (done/failed) items age off the display after FINISHED_TTL.
    ORPHANED running items (task_liveness found a real probe with no
    evidence of life) age off after one more FINISHED_TTL window past the
    moment they actually crossed the stale threshold — long enough that the owner
    sees the ghost, not so long it never leaves. UNKNOWN running items (no
    heartbeat_file at all) are NEVER pruned this way: "no evidence either
    way" is not the same fact as "confirmed gone", and hiding it would just
    be a different false claim."""
    now = time.time()
    items = [task_liveness(i, now) for i in read_tasks_raw()]
    return {
        "items": [
            i for i in items
            if (i["status"] in ("running", "pending") and i["liveness"] != "orphaned")
            or (i["liveness"] == "orphaned" and (i["liveness_age"] - HEARTBEAT_STALE_SECONDS) < FINISHED_TTL_SECONDS)
            or (i["status"] in ("done", "failed") and (now - (i.get("ts") or now)) < FINISHED_TTL_SECONDS)
        ]
    }


def stamp_tasks(items: list) -> list:
    """Preserve each item's ts while its status is unchanged, so age-out is
    measured from the status change, not the last list rewrite. Also
    preserve heartbeat_file from the prior record when a POST omits it for
    an already-known id — the liveness probe should survive an unrelated
    field edit (e.g. a detail-text update) rather than silently reverting to
    UNKNOWN because one POST forgot to repeat it. A POST that DOES include
    heartbeat_file always wins (lets the caller correct or clear it)."""
    prior = {i.get("id"): i for i in read_tasks_raw()}
    now = time.time()
    return [
        {
            **i,
            "ts": (
                prior[i.get("id")].get("ts")
                if i.get("id") in prior and prior[i.get("id")].get("status") == i.get("status")
                else now
            ) or now,
            "heartbeat_file": i.get("heartbeat_file") or prior.get(i.get("id"), {}).get("heartbeat_file"),
        }
        for i in items
    ]


SPAWN_MODELS = ("sonnet", "opus", "fable")
SPAWN_EFFORTS = ("low", "medium", "high")
SPAWN_COOLDOWN_SECONDS = 10
_spawn_state = {"at": 0.0}


def spawn_session(model, effort) -> dict:
    """Open a new iTerm window (iTerm on macOS, Ptyxis or x-terminal-emulator on Linux) running a plain-TEXT claude session — a second
    VOICE session would fight this one for the mic, so /spawn never starts
    voicemode. Flags are whitelisted; anything else is silently omitted."""
    now = time.time()
    if now - _spawn_state["at"] < SPAWN_COOLDOWN_SECONDS:
        return {"ok": False, "error": "cooldown"}
    else:
        flags = (
            (f" --model {model}" if model in SPAWN_MODELS else "")
            + (f" --effort {effort}" if effort in SPAWN_EFFORTS else "")
        )
        command = f"cd ~/repos && claude{flags}"
        if sys.platform == "darwin":
            script = (
                'tell application "iTerm"\n'
                "\tactivate\n"
                "\tcreate window with default profile\n"
                "\ttell current session of current window\n"
                f'\t\twrite text "{command}"\n'
                "\tend tell\n"
                "end tell"
            )
            subprocess.Popen(["osascript", "-e", script])
        else:
            # Linux: a new window in whichever terminal is installed, running
            # the user's login shell so PATH and the claude binary resolve.
            # `exec $SHELL` keeps the window open after claude exits.
            shell = os.environ.get("SHELL", "/bin/bash")
            inner = f"{command}; exec {shell}"
            if shutil.which("ptyxis"):
                argv = ["ptyxis", "--new-window", "--", shell, "-lc", inner]
            else:
                argv = ["x-terminal-emulator", "-e", shell, "-lc", inner]
            subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _spawn_state["at"] = now
        return {"ok": True, "command": f"claude{flags}".strip()}


def read_inbox() -> dict:
    stored = (
        parse_json(INBOX_FILE.read_text()) or {}
        if INBOX_FILE.exists()
        else {}
    )
    items = stored.get("items")
    return {"items": items if isinstance(items, list) else []}


def read_inbox_history() -> list:
    stored = (
        parse_json(INBOX_HISTORY_FILE.read_text()) or {}
        if INBOX_HISTORY_FILE.exists()
        else {}
    )
    items = stored.get("items")
    return items if isinstance(items, list) else []


def append_inbox_history(text: str, ts: float) -> None:
    items = (read_inbox_history() + [{"text": text, "ts": ts, "status": "queued"}])[-100:]
    INBOX_HISTORY_FILE.write_text(json.dumps({"items": items}))
    return None


def mark_inbox_history_seen() -> None:
    items = [
        {**i, "status": "seen"} if i.get("status") == "queued" else i
        for i in read_inbox_history()
    ]
    INBOX_HISTORY_FILE.write_text(json.dumps({"items": items}))
    return None


def enqueue_inbox_text(text: str) -> int:
    """Shared by the typed inbox and the dictated (audio) inbox — both land in
    the live queue + inbox_history with status queued, same comms-feed entry."""
    now = time.time()
    items = (read_inbox()["items"] + [{"text": text, "ts": now}])[-50:]
    INBOX_FILE.write_text(json.dumps({"items": items}))
    append_inbox_history(text, now)
    return len(items)


def read_says() -> list:
    stored = (
        parse_json(SAYS_FILE.read_text()) or {}
        if SAYS_FILE.exists()
        else {}
    )
    items = stored.get("items")
    return items if isinstance(items, list) else []


def append_say(text: str, persona: str) -> float:
    """Text-only assistant reply (quiet mode, POST /say): persisted like
    inbox_history, timestamped SERVER-side so ordering in the merged comms
    feed can't be skewed by a client clock. Never touches TTS."""
    now = time.time()
    entry = {"text": text, "ts": now, **({"persona": persona} if persona else {})}
    items = (read_says() + [entry])[-SAYS_STORED:]
    SAYS_FILE.write_text(json.dumps({"items": items}))
    return now


STANDBY_IDLE_SECONDS = int(os.environ.get("STANDBY_IDLE_SECONDS", 20 * 60))
# No manual standby control in the UI (owner, 08-17 — "listening" vs "muted"
# are the only two user-facing states now). Standby is a pure background
# idle timer: no user directive and no assistant activity for this long
# arms it on its own; any fresher activity clears it. Configurable via env
# for tuning without a code change.


def last_activity_epoch() -> float:
    """Most recent of: a voicemode TTS/STT event, an inbox directive, a live
    session-transcript write, or a still-running task — the activity signal
    read_standby()'s idle timer measures against. Reuses sources
    build_state() already reads rather than a dedicated heartbeat file."""
    event_ts = max(
        (iso_epoch(e.get("timestamp") or "") for e in read_events()), default=0.0
    )
    inbox_ts = max((i.get("ts") or 0 for i in read_inbox_history()), default=0.0)
    session_ts = max(
        (f.stat().st_mtime for f in SESSIONS_DIR.glob("*.jsonl")), default=0.0
    )
    tasks_ts = max(
        (t.get("ts") or 0 for t in read_tasks_raw() if t.get("status") == "running"),
        default=0.0,
    )
    return max(event_ts, inbox_ts, session_ts, tasks_ts)


def write_standby(standby: bool, since: float) -> None:
    """`since` is when THIS value was last set — by a POST /standby (any
    value) or by read_standby()'s own auto-arm/auto-clear. Comparing fresh
    activity against it is what lets a deliberate write hold (not get
    immediately re-armed or re-cleared by stale activity on the very next
    read) while still letting genuine activity clear an auto-armed standby."""
    STANDBY_FILE.write_text(json.dumps({"standby": standby, "since": since}))
    return None


def read_standby() -> dict:
    """Auto-idle standby (owner, 08-17 — replaces the old manual tap-to-sleep
    UI, which is gone; the endpoint and its persisted state stay for
    background waiters like wake_listener.py). Idle >= STANDBY_IDLE_SECONDS
    (measured from whichever is more recent: real activity, or the last
    time this flag itself was touched) auto-arms it; fresher real activity
    than that touch auto-clears it. A POST /standby write is honored as-is
    immediately — this function only ever acts on a LATER read, so it never
    clobbers an explicit write, it just may supersede it once time (or
    activity) has actually moved on."""
    stored = (
        parse_json(STANDBY_FILE.read_text()) or {}
        if STANDBY_FILE.exists()
        else {}
    )
    was = bool(stored.get("standby"))
    since = stored.get("since") or 0.0
    activity = last_activity_epoch()
    idle_for = time.time() - max(activity, since)
    if not was and idle_for >= STANDBY_IDLE_SECONDS:
        write_standby(True, since)
        return {"standby": True}
    elif was and activity > since:
        write_standby(False, time.time())
        return {"standby": False}
    else:
        return {"standby": was}


def transcribe_wav_bytes(wav_bytes: bytes) -> str:
    """POST raw WAV bytes to the local whisper server as multipart/form-data
    (field "file", filename chunk.wav) — same recipe as wake_listener.py's
    transcribe(), minus the PCM->WAV step since the browser already sends WAV."""
    boundary = uuid.uuid4().hex
    body = b"".join(
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="chunk.wav"\r\n'
            "Content-Type: audio/wav\r\n\r\n".encode(),
            wav_bytes,
            f"\r\n--{boundary}--\r\n".encode(),
        )
    )
    req = urllib.request.Request(
        WHISPER_TRANSCRIBE_URL,
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return (json.loads(resp.read()).get("text") or "").strip()
    except (urllib.error.URLError, ValueError, OSError):
        return ""


def transcribe_wav_bytes_detailed(wav_bytes: bytes) -> tuple:
    """Same call, asking for verbose_json so the decoder confidence signals
    wake_guard's tier 1 needs come back alongside the text. Returns
    (text, signals), and falls back to transcribe_wav_bytes() on any failure
    so an older whisper build costs the gate its acoustic tier — never the
    dictation itself."""
    boundary = uuid.uuid4().hex
    body = b"".join(
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="response_format"\r\n\r\nverbose_json\r\n'
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="temperature"\r\n\r\n0\r\n'
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="chunk.wav"\r\n'
            "Content-Type: audio/wav\r\n\r\n".encode(),
            wav_bytes,
            f"\r\n--{boundary}--\r\n".encode(),
        )
    )
    req = urllib.request.Request(
        WHISPER_TRANSCRIBE_URL,
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            parsed = json.loads(resp.read())
        return (transcript_text(parsed), decoder_signals(parsed))
    except (urllib.error.URLError, ValueError, OSError):
        return (transcribe_wav_bytes(wav_bytes), {})


def wav_bytes_rms(wav_bytes: bytes) -> float:
    """Stdlib-only normalized RMS (0..1) of a 16-bit PCM WAV's samples —
    mirrors always_on_listener.py's numpy-based rms_of() against the same
    wake_guard.RMS_GATE, so /inbox/audio can reject a near-silent dictation
    upload before it ever reaches whisper, same as the ambient wake path
    already does. Returns 1.0 (never gates) for anything unparsable or not
    16-bit PCM, so a format surprise fails open to the phrase guard instead
    of silently swallowing a real directive."""
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as w:
            width = w.getsampwidth()
            frames = w.readframes(w.getnframes())
    except (wave.Error, EOFError, ValueError):
        return 1.0
    else:
        if width != 2 or not frames:
            return 1.0
        else:
            samples = array("h", frames)
            mean_sq = sum(s * s for s in samples) / len(samples)
            return (mean_sq ** 0.5) / 32768.0


# ---- comms replay: re-synthesise a spoken line (08-24) ----------------
# the owner is usually walking around the room while a line is spoken, so the comms
# feed is where they finds it afterwards -- but the HUD stores no audio, only
# text derived from voicemode's event log, so "say that again" was impossible.
# Replay re-synthesises through the SAME local kokoro that spoke it, in the
# SAME voice: the personas are how they tells whose finding a line was, and a
# generic browser SpeechSynthesis voice would erase exactly that.
KOKORO_BASE = os.environ.get("VOICE_HUD_KOKORO_URL", "http://127.0.0.1:8880/v1")
KOKORO_SPEECH_URL = f"{KOKORO_BASE}/audio/speech"
KOKORO_VOICES_URL = f"{KOKORO_BASE}/audio/voices"
KOKORO_MODEL = "tts-1"
KOKORO_VOICES_CACHE_SECONDS = 600
REPLAY_TIMEOUT_SECONDS = 120
REPLAY_MAX_CHARS = 4000  # matches POST /say's own cap
DEFAULT_REPLAY_VOICE = "bm_george"  # same display default as build_state()
# Cap the clip cache by BYTES, not entries: mp3 here runs ~16KB per second of
# speech, so a long line is megabytes and an entry count would bound nothing.
REPLAY_CACHE_BYTES = 32_000_000
VOICEMODE_ENV_FILE = Path.home() / ".voicemode" / "voicemode.env"
_kokoro_voices = {"at": 0.0, "value": ()}
# (voice, text) -> mp3 bytes, insertion-ordered so the oldest evicts first.
# Same shape as the other process-lifetime caches on this server.
_replay_clips = {}


def env_value(text: str, key: str) -> str:
    """Pure: the last uncommented `KEY=value` assignment in a voicemode.env
    body. Last wins because that file appends overrides at the bottom over a
    commented-out documentation block above."""
    values = tuple(
        line.split("=", 1)[1].strip().strip('"').strip("'")
        for line in text.splitlines()
        if line.strip().startswith(key + "=")
    )
    return values[-1] if values else ""


def parse_speed(raw: str) -> float:
    """Pure: kokoro's `speed` multiplier, clamped to what it accepts."""
    try:
        return min(4.0, max(0.25, float(raw)))
    except ValueError:
        return 1.0


def read_tts_speed() -> float:
    """Synthesise a replay at the SAME rate voicemode speaks at, so a 1x
    replay sounds like the line actually sounded. The owner runs 1.25 today; the
    page's own 1x/1.5x/2x toggle multiplies on top of this via playbackRate,
    which is why this is the base rather than a hardcoded 1.0."""
    return parse_speed(
        os.environ.get("VOICEMODE_TTS_SPEED")
        or env_value(
            VOICEMODE_ENV_FILE.read_text() if VOICEMODE_ENV_FILE.exists() else "",
            "VOICEMODE_TTS_SPEED",
        )
    )


REPLAY_SPEED = read_tts_speed()


def fetch_kokoro_voices() -> tuple:
    """Kokoro's own voice list. Fetched rather than hardcoded because the
    personas in says.json already range across most of it (Kore, Fenrir,
    Lewis, Puck...), and a stale local copy would silently replay a line in
    the wrong voice — the one thing this feature exists to get right."""
    try:
        with urllib.request.urlopen(KOKORO_VOICES_URL, timeout=2.0) as resp:
            payload = parse_json(resp.read().decode()) or {}
    except (urllib.error.URLError, OSError, ValueError):
        return ()
    voices = payload.get("voices")
    return tuple(v for v in voices if isinstance(v, str)) if isinstance(voices, list) else ()


def kokoro_voices() -> tuple:
    """Cached roster. A failed fetch keeps the last known good rather than
    emptying it (expiring beats deleting), so a momentarily-down kokoro can't turn every persona into the
    default voice."""
    now = time.time()
    if _kokoro_voices["value"] and now - _kokoro_voices["at"] < KOKORO_VOICES_CACHE_SECONDS:
        return _kokoro_voices["value"]
    else:
        fetched = fetch_kokoro_voices()
        _kokoro_voices.update(at=now, value=fetched or _kokoro_voices["value"] or VOICE_ROSTER)
        return _kokoro_voices["value"]


def persona_voice(persona: str, roster: tuple) -> str:
    """Pure: which kokoro voice a display persona ("Emma", "Kore") names.
    The feed renders a spoken line's author as the voice id minus its
    language/gender prefix (see index.html's renderer), so the way back is
    exactly that suffix. First match in roster order wins, which only matters
    for suffixes kokoro reuses across languages (santa, alex, dora, alpha);
    the roster is English-first, so those resolve to the English voice.
    An unrecognised persona — an agent name like "Incident", or a voice
    kokoro no longer serves — returns "" rather than guessing a prefix."""
    name = persona.strip().lower()
    return next((v for v in roster if v.split("_", 1)[1] == name), "")


def resolve_replay_voice(voice: str, persona: str, roster: tuple) -> str:
    """Pure: the kokoro voice to replay a comms line in. A spoken line
    carries its literal voice id from the event log and that always wins; a
    text-only POST /say line carries only a display persona, so it is
    resolved by name. Anything unresolvable falls back to the default rather
    than failing the replay — hearing the line in the wrong voice still
    beats not hearing it."""
    named = voice.strip()
    if named in roster:
        return named
    else:
        matched = persona_voice(persona, roster)
        return matched or (DEFAULT_REPLAY_VOICE if DEFAULT_REPLAY_VOICE in roster else roster[0])


# Emoji, dingbats, arrows and their variation selectors. Kokoro reads these
# ALOUD by name, so a line opening "✅✅ **CONFIRMED**" replays as "white white
# confirmed" -- measured 2026-08-24 by transcribing a replay back through the
# local whisper. Markdown emphasis is deliberately NOT stripped: the same
# measurement showed kokoro already swallows ** and ` silently, so touching
# them would be unverified meddling with the text they asked to hear again.
UNSPEAKABLE = re.compile(
    "["
    "←-⇿"              # arrows
    "⌀-➿"              # misc technical + symbols + dingbats: ⌨ ⚠ ✅ ✓ ❌
    "⬀-⯿"              # misc symbols and arrows: ⭐ ⬆
    "︀-️"              # variation selectors (the invisible half of ⚠️)
    "\U0001f000-\U0001faff"      # emoji, pictographs, flags
    "]+"
)


def speakable(text: str) -> str:
    """Pure: the line with symbols no one wants read aloud removed, and the
    gaps they leave closed up."""
    return " ".join(UNSPEAKABLE.sub(" ", text).split())


def synthesize_speech(text: str, voice: str, speed: float) -> tuple:
    """(mp3 bytes, error) — Result-shaped, never raises. mp3 rather than the
    pcm voicemode streams internally because this has to land in an <audio>
    element the page can pause, resume and rate-shift."""
    body = json.dumps(
        {
            "model": KOKORO_MODEL,
            "input": text,
            "voice": voice,
            "response_format": "mp3",
            "speed": speed,
        }
    ).encode()
    req = urllib.request.Request(
        KOKORO_SPEECH_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=REPLAY_TIMEOUT_SECONDS) as resp:
            return (resp.read(), "")
    except urllib.error.HTTPError as exc:
        return (b"", f"kokoro HTTP {exc.code}")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return (b"", f"{type(exc).__name__}: {exc}")


def replay_failure_reason(error: str) -> str:
    """Pure: what the HUD says when synthesis fails, as opposed to what the
    log records. Kokoro restarts its worker every N requests
    (VOICEMODE_KOKORO_MAX_REQUESTS) and refuses connections for a second or
    two while it does — hit live during this feature's own verification,
    08-24, and the retry a moment later succeeded. So the common failure is
    retryable, and the page should say so instead of showing an errno."""
    return (
        "engine busy — retry"
        if "Connection refused" in error or "timed out" in error
        else "engine offline"
    )


def trim_replay_clips() -> None:
    while (
        sum(len(v) for v in _replay_clips.values()) > REPLAY_CACHE_BYTES
        and len(_replay_clips) > 1
    ):
        del _replay_clips[next(iter(_replay_clips))]
    return None


def cached_speech(text: str, voice: str, speed: float) -> tuple:
    """(mp3 bytes, error, cache_hit). The page keeps its own object-URL cache
    so a second press of the same line never gets here at all; this one is
    what makes replay instant again after a page RELOAD, which is otherwise
    the common case (the owner reloads the tab after every server restart)."""
    key = (voice, text)
    if key in _replay_clips:
        return (_replay_clips[key], "", True)
    else:
        audio, error = synthesize_speech(text, voice, speed)
        if error:
            return (b"", error, False)
        else:
            _replay_clips[key] = audio
            trim_replay_clips()
            return (audio, "", False)


PLANS_DIR = Path.home() / "repos" / "plans"
PLANS_CACHE_SECONDS = 30
_plans_cache = {"at": 0.0, "value": ()}


def bridge_alive(url: str) -> bool:
    match = re.search(r"127\.0\.0\.1%3A(\d+)", url) or re.search(r"127\.0\.0\.1:(\d+)", url)
    if not match:
        return False
    else:
        try:
            with socket.create_connection(("127.0.0.1", int(match.group(1))), timeout=0.2):
                return True
        except OSError:
            return False


def read_plans() -> tuple:
    """Visual plans with a served bridge URL. Each `live` IS a genuine probe
    (bridge_alive actually connects) — but the result is cached for
    PLANS_CACHE_SECONDS, so what's on screen between refreshes is a real fact
    that's up to 30s old, not a live one. build_state() also exposes
    _plans_cache["at"] (as plans_checked_epoch) so the page can show that age
    and read it as stale if a refresh is ever overdue (see index.html:
    amber past PLANS_CACHE_SECONDS means the cache itself stopped turning,
    not just "within its normal window")."""
    now = time.time()
    if now - _plans_cache["at"] < PLANS_CACHE_SECONDS:
        return _plans_cache["value"]
    else:
        plans = tuple(
            {
                "name": d.name,
                "url": (d / ".plan-url").read_text().strip(),
                "live": bridge_alive((d / ".plan-url").read_text().strip()),
            }
            for d in sorted(PLANS_DIR.iterdir())
            if d.is_dir() and (d / ".plan-url").exists()
        ) if PLANS_DIR.exists() else ()
        _plans_cache.update(at=now, value=plans)
        return plans


def local_time(ts: str) -> str:
    try:
        return datetime.fromisoformat(ts).astimezone(EASTERN).strftime("%H:%M:%S")
    except ValueError:
        return ts[11:19]


def iso_epoch(ts: str) -> float:
    try:
        return datetime.fromisoformat(ts).timestamp()
    except ValueError:
        return 0.0


def epoch_local_time(ep: float) -> str:
    return datetime.fromtimestamp(ep, tz=EASTERN).strftime("%H:%M:%S")


def worklog_entry(event):
    if event.get("type") != "assistant":
        return None
    else:
        content = (event.get("message") or {}).get("content") or []
        texts = tuple(
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
        joined = "\n".join(t for t in texts if t).strip()
        if joined:
            return {"t": local_time(event.get("timestamp") or ""), "text": joined[:2500]}
        else:
            return None


def read_session_lines() -> tuple:
    """Tail the newest session transcript once, parsed to dicts — shared by
    read_worklog() and the Comms-feed spoken-text correlation so the file
    isn't read (or re-decoded) twice per request. A partial first line, cut
    mid-JSON by the byte-offset tail, simply fails to parse and is dropped."""
    files = sorted(SESSIONS_DIR.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    if not files:
        return ()
    else:
        text = files[-1].read_bytes()[-WORKLOG_TAIL_BYTES:].decode(errors="replace")
        return tuple(e for e in map(parse_json, text.splitlines()) if isinstance(e, dict))


def read_worklog(session_lines: tuple) -> tuple:
    """Full text of the active Claude session's replies, newest-following
    from whichever session transcript is live."""
    entries = tuple(w for w in map(worklog_entry, session_lines) if w is not None)
    return entries[-25:]


def session_active() -> bool:
    """True while the live session transcript is being written (Claude's turn
    is busy) — the page shows 'working' instead of idle/standby."""
    files = tuple(SESSIONS_DIR.glob("*.jsonl"))
    if not files:
        return False
    else:
        newest = max(f.stat().st_mtime for f in files)
        return (time.time() - newest) < 20


def transcript_entry(event: dict):
    kind = event.get("event_type")
    data = event.get("data") or {}
    raw_ts = event.get("timestamp") or ""
    ts = local_time(raw_ts)
    ep = iso_epoch(raw_ts)
    if kind == "TTS_START" and data.get("message"):
        return {"who": "claude", "text": data["message"], "voice": data.get("voice"), "t": ts, "ep": ep}
    elif kind == "STT_COMPLETE" and data.get("text"):
        return {"who": "you", "text": " ".join(data["text"].split()), "t": ts, "ep": ep}
    else:
        return None


def converse_call_texts(call_input: dict) -> tuple:
    """Flatten one *converse tool_use call's input into the ordered spoken
    utterances it produced. The plain `message` form is a single utterance;
    the multi-turn `turns` form is one utterance per `say`/`ask` turn, each
    carrying its own voice override when given, else the call-level voice."""
    call_voice = call_input.get("voice")
    turns = call_input.get("turns")
    if isinstance(turns, list):
        return tuple(
            (turn.get("say") or turn.get("ask"), turn.get("voice") or call_voice)
            for turn in turns
            if isinstance(turn, dict) and (turn.get("say") or turn.get("ask"))
        )
    else:
        message = call_input.get("message")
        return ((message, call_voice),) if message else ()


def converse_pairs_in_line(entry: dict) -> tuple:
    """The spoken utterances in one parsed transcript line, if it is an
    assistant turn containing *converse tool_use blocks. Matches on the tool
    name CONTAINING "converse" (not an exact string) so a plugin rename
    doesn't silently break this — the live name is currently the rather
    long mcp__plugin_voicemode_voicemode__converse."""
    if entry.get("type") != "assistant":
        return ()
    else:
        return tuple(
            pair
            for block in ((entry.get("message") or {}).get("content") or [])
            if isinstance(block, dict)
            and block.get("type") == "tool_use"
            and "converse" in (block.get("name") or "")
            for pair in converse_call_texts(block.get("input") or {})
        )


# Incremental scan state for the converse-candidate harvest below. Session
# transcripts are append-only, so each poll only has to look at the bytes
# added since the last one; this dict is what remembers where that was.
_CONVERSE_SCAN = {"path": None, "offset": 0, "partial": b"", "candidates": ()}
MAX_CONVERSE_CANDIDATES = 500  # bounds memory on a multi-day session; far
                                # above the <=800 events read_events() can
                                # surface, so it can never starve a match


def spoken_call_texts() -> tuple:
    """Every spoken utterance issued via a *converse tool_use call in the
    WHOLE live session transcript, in call/turn order. This is where the
    FULL text lives — voicemode's own event log stores only TTS_START's
    first 200 chars in data.message, and repairing that is the entire point
    of the comms-feed correlation.

    WHY THIS NO LONGER USES read_session_lines()'s byte tail (2026-08-18,
    and this is the "again" in the owner's "showing only 200 characters again"):
    the candidates were harvested from the same fixed-size tail the worklog
    uses, so the repair silently degraded the moment a session's transcript
    outgrew that window. It is not a gentle degradation either, because the
    window is measured in BYTES of transcript while the thing it needs to
    contain is spoken turns, and the ratio between them is set by how much
    unrelated tool output (bash dumps, file reads, subagent results) landed
    in between. Measured on this session — 24 spoken turns, 1.49MB file:

        tail=  400,000  candidates= 4   still-truncated=16 of 24
        tail=  800,000  candidates=10   still-truncated=12 of 24
        tail=1,400,000  candidates=26   still-truncated= 0 of 24

    i.e. it took ~58KB of transcript per recovered utterance that day, and
    the previous fix for this was to raise the constant (400KB -> 2MB). That
    is why it regressed: widening a byte window buys time proportional to
    how chatty the tools happen to be, and every long session eventually
    spends it. So the window is gone rather than widened. The transcript is
    append-only, so this scans it once and thereafter only reads the bytes
    appended since the last poll, JSON-parsing only lines that mention
    converse at all. Cost is O(new bytes) per poll instead of O(window), and
    coverage is the whole session instead of its last N megabytes."""
    files = sorted(SESSIONS_DIR.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    if not files:
        return ()
    else:
        path = files[-1]
        size = path.stat().st_size
        # A different session file, or one that shrank (rotated/rewritten),
        # invalidates every offset we remember — rescan it from zero.
        restart = _CONVERSE_SCAN["path"] != str(path) or size < _CONVERSE_SCAN["offset"]
        start = 0 if restart else _CONVERSE_SCAN["offset"]
        with path.open("rb") as fh:
            fh.seek(start)
            chunk = fh.read()
        buffered = (b"" if restart else _CONVERSE_SCAN["partial"]) + chunk
        rows = buffered.split(b"\n")
        # The last element is whatever follows the final newline: either
        # empty, or a line the writer hasn't finished yet. Either way it is
        # carried forward rather than parsed, so a half-written JSON line is
        # never dropped, just deferred to the next poll.
        complete, partial = rows[:-1], rows[-1]
        found = tuple(
            pair
            for raw in complete
            if b"converse" in raw
            for entry in (parse_json(raw.decode("utf-8", "replace")),)
            if isinstance(entry, dict)
            for pair in converse_pairs_in_line(entry)
        )
        prior = () if restart else _CONVERSE_SCAN["candidates"]
        _CONVERSE_SCAN.update(
            path=str(path),
            # EOF, not "EOF minus the partial line": `partial` is carried
            # in memory and prepended next poll, so rewinding the offset
            # over it as well would read those bytes twice and splice a
            # corrupt duplicate line. Caught by test, 2026-08-18.
            offset=start + len(chunk),
            partial=partial,
            candidates=(prior + found)[-MAX_CONVERSE_CANDIDATES:],
        )
        return _CONVERSE_SCAN["candidates"]


SOURCE_TRUNCATION_CHARS = 200  # voicemode writes TTS_START's data.message
                                # cut to exactly this many chars; a shorter
                                # event text is therefore already complete


def resolve_spoken_text(candidates: tuple, truncated: str) -> tuple:
    """One correlation step: find the first not-yet-used transcript text that
    STARTS WITH this 200-char-truncated event-log text — a prefix match is
    reliable here since the truncation is a literal prefix — consume it, and
    return the full text alongside the candidates left for the next event.
    No match means its transcript record isn't in the session file at all:
    pass it through as-is rather than drop the line, but flag it (third
    tuple element) — this is the "still can't be recovered" case the
    Comms feed must mark rather than silently show as if it were complete
    (owner, 08-18: a stale/truncated line rendered identically to a whole one
    is exactly the failure mode this whole pass is about).

    Only texts that are ACTUALLY at the truncation length are repaired. A
    shorter event text is already the complete utterance and has nothing to
    recover, so prefix-matching it would be all risk and no benefit: it
    could consume a longer candidate that merely opens with the same words,
    which both corrupts that line and steals the candidate the real line
    needed later. Gating on the length makes a short line a guaranteed
    no-op instead — and correctly NOT flagged, since it was never truncated
    in the first place. (Non-repaired lines still can't be stolen FROM:
    their candidate is under 200 chars, so no 200-char prefix can match it.)

    Unmatched candidates are deliberately left in place rather than skipped
    past. Converse calls that never spoke — blocked by the permission
    classifier, most recently — appear here as candidates with no
    corresponding TTS_START event, and this session carried two of them.
    Searching the whole remaining list rather than only its head is what
    makes those harmless: a never-spoken candidate is simply never matched,
    instead of desynchronizing every line after it."""
    if len(truncated) < SOURCE_TRUNCATION_CHARS:
        return (candidates, truncated, False)
    else:
        match = next((i for i, (full, _) in enumerate(candidates) if full.startswith(truncated)), None)
        return (
            (candidates[:match] + candidates[match + 1:], candidates[match][0], False)
            if match is not None
            else (candidates, truncated, True)
        )


def correlate_spoken_texts(claude_lines: tuple, candidates: tuple) -> tuple:
    """Swap each Comms-feed 'claude' line's event-log text (truncated to 200
    chars at the source) for the full spoken text recovered from the session
    transcript, in chronological order. Timestamp and voice stay event-log-
    derived — only text is replaced. Each line also carries `truncated`:
    True means recovery genuinely failed and what's shown is still the
    200-char event-log prefix, not the full utterance — the page marks
    those rather than rendering them identically to a whole line."""
    def step(acc, line):
        remaining, resolved = acc
        remaining_next, text, truncated = resolve_spoken_text(remaining, line["text"])
        return remaining_next, resolved + ({**line, "text": text, "truncated": truncated},)

    _, resolved = reduce(step, claude_lines, (candidates, ()))
    return resolved


def typed_entries() -> tuple:
    """the owner's typed inbox messages, merged into the comms feed with a status chip."""
    return tuple(
        {
            "who": "typed",
            "text": i.get("text", ""),
            "t": epoch_local_time(i.get("ts") or 0),
            "ep": i.get("ts") or 0,
            "status": i.get("status", "queued"),
        }
        for i in read_inbox_history()
    )


def say_entries() -> tuple:
    """Text-only assistant replies (POST /say — quiet mode), merged into the
    comms feed like spoken assistant lines but flagged who="say" so the page
    can chip them as text-only. Carries `persona` (display name), NOT `voice`,
    so build_state()'s spoken-voice readout is never polluted by an unspoken
    line. Only the newest SAYS_SERVED are served to keep the payload small."""
    return tuple(
        {
            "who": "say",
            "text": i.get("text", ""),
            # JARVIS rebrand 08-19: display default only — personas posted by
            # the session pass through untouched, same field, same schema.
            "persona": i.get("persona") or "Jarvis",
            "t": epoch_local_time(i.get("ts") or 0),
            "ep": i.get("ts") or 0,
        }
        for i in read_says()[-SAYS_SERVED:]
    )


def telemetry(events: tuple) -> dict:
    def last_metric(kind: str, extract):
        matches = tuple(e for e in events if e.get("event_type") == kind)
        return extract((matches[-1].get("data") or {})) if matches else None

    return {
        "ttfa_ms": last_metric("TTS_PLAYBACK_END", lambda d: (d.get("metrics") or {}).get("ttfa_ms")),
        "tts_ms": last_metric("TTS_PLAYBACK_END", lambda d: (d.get("metrics") or {}).get("total_time_ms")),
        "stt_ms": last_metric("STT_COMPLETE", lambda d: (d.get("metrics") or {}).get("request_time_ms")),
        "rec_s": last_metric("RECORDING_END", lambda d: d.get("duration")),
        "turns": sum(1 for e in events if e.get("event_type") == "STT_COMPLETE"),
    }


def build_state() -> dict:
    session_lines = read_session_lines()
    events = read_events()
    spoken = tuple(e for e in map(transcript_entry, events) if e is not None)
    claude_spoken = correlate_spoken_texts(
        tuple(e for e in spoken if e["who"] == "claude"),
        spoken_call_texts(),
    )
    other_spoken = tuple(e for e in spoken if e["who"] != "claude")
    entries = tuple(sorted(claude_spoken + other_spoken + typed_entries() + say_entries(), key=lambda e: e.get("ep") or 0))
    statuses = tuple(
        STATUS_BY_EVENT[e["event_type"]]
        for e in events
        if e.get("event_type") in STATUS_BY_EVENT
    )
    spoken_voices = tuple(e["voice"] for e in entries if e.get("voice"))
    override = read_override()
    plans = read_plans()
    return {
        "status": statuses[-1] if statuses else "standby",
        # bm_george = the owner's chosen JARVIS voice (08-19): shown as the default
        # before anything has been spoken this session. The REAL voice choice
        # lives in voicemode's own config, never here — this is display-only
        # and is overridden by the actual spoken voice the moment one exists.
        "voice": spoken_voices[-1] if spoken_voices else "bm_george",
        "requested_voice": override.get("voice"),
        "muted": read_mute()["muted"],
        "standby": read_standby()["standby"],
        "tasks": read_tasks()["items"],
        "cast": read_cast()["seats"],
        "worklog": read_worklog(session_lines),
        "session_active": session_active(),
        "plans": plans,
        # When this was last ACTUALLY probed (see read_plans()) — read AFTER
        # calling read_plans() above so a cache-refresh on this very call is
        # reflected, not the stale value from before it.
        "plans_checked_epoch": _plans_cache["at"],
        "plans_cache_seconds": PLANS_CACHE_SECONDS,
        "heartbeat_stale_seconds": HEARTBEAT_STALE_SECONDS,
        "roster": VOICE_ROSTER,
        "lines": entries[-60:],
        "total": len(entries),
        "telemetry": telemetry(events),
        "listening": read_listening(events),
        "playback": read_playback(events),
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        return None

    def _send(self, body: bytes, ctype: str, code: int = 200, extra: tuple = ()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in extra:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)
        return None

    def do_GET(self):
        route = self.path.split("?")[0]
        if route in ("/", "/index.html"):
            return self._send((STATIC_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif route == "/state.json":
            return self._send(json.dumps(build_state()).encode(), "application/json")
        elif route == "/voice":
            return self._send(json.dumps(read_override()).encode(), "application/json")
        elif route == "/mute":
            return self._send(json.dumps(read_mute()).encode(), "application/json")
        elif route == "/standby":
            return self._send(json.dumps(read_standby()).encode(), "application/json")
        elif route == "/tasks":
            return self._send(json.dumps(read_tasks()).encode(), "application/json")
        elif route == "/cast":
            return self._send(json.dumps(read_cast()).encode(), "application/json")
        elif route == "/inbox":
            return self._send(json.dumps(read_inbox()).encode(), "application/json")
        elif route == "/health":
            return self._send(b"ok", "text/plain")
        else:
            return self._send(b"not found", "text/plain", 404)

    def do_POST(self):
        route = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length") or 0)
        if route == "/inbox/audio":
            return self._handle_inbox_audio(length)
        else:
            return self._handle_json_post(route, length)

    def _handle_inbox_audio(self, length: int):
        """Same noise guard as always_on_listener.py's ambient wake path
        (owner, 08-17 — whisper hallucinated "Thank you." on a dead-mic
        recording and it got enqueued as a directive, twice): reject
        near-silent uploads before they reach whisper at all, and reject a
        transcript that's a stock hallucination or too short to be a real
        directive before it reaches the inbox."""
        if length <= 0 or length > MAX_AUDIO_BYTES:
            return self._send(json.dumps({"ok": False, "error": "bad length"}).encode(), "application/json", 413)
        else:
            wav_bytes = self.rfile.read(length)
            if wav_bytes_rms(wav_bytes) < RMS_GATE:
                # The ONLY branch that means "no speech detected" — the audio
                # never reached whisper, so there is no transcript and no gate
                # verdict. It carries tier "silence" so the HUD can say that
                # and stop saying it about gate rejections (below), which are
                # a completely different problem with a different fix.
                return self._send(json.dumps(silence_outcome()).encode(), "application/json")
            else:
                transcript, signals = transcribe_wav_bytes_detailed(wav_bytes)
                text = transcript[:2000]
                # safe_judge_directive never raises, so this handler always
                # answers: a gate failure here would otherwise leave the
                # request hanging with nothing enqueued and no reply.
                verdict = safe_judge_directive(text, signals)
                log_line(describe(verdict))
                # Rejected dictation is deliberately NOT enqueued, but
                # dictation_outcome reports the transcript AND the tier and
                # reason that rejected it, so the rejection is distinguishable
                # from having heard nothing — and says which tier, so the owner can
                # tell a stock filler phrase from a confidence-floor reject
                # without going to the log.
                enqueue_inbox_text(text) if verdict.accept else None
                return self._send(json.dumps(dictation_outcome(verdict)).encode(), "application/json")

    def _handle_replay(self, payload: dict):
        """Re-synthesise one comms line for playback in the page.

        MUTE IS ENFORCED HERE, not only in the page. Quiet mode is the owner's
        meeting-safety control and their stated number-one requirement, so a
        replay button must never be a route around it: while mute.json says
        muted this refuses to produce audio at all, 409. (The page also
        refuses to PLAY while muted, and pauses a replay the moment mute
        flips on — that second guard is the load-bearing one for a clip
        already cached in the browser, which never reaches this server.)

        The live-turn collision is deliberately NOT guarded here. The page
        holds the same `speaking` status this server derives and refuses to
        start (and pauses mid-replay) on it; putting it here as well would
        add a state that can stick — a TTS_PLAYBACK_START whose matching END
        never arrives would refuse every replay until the event log rolled,
        with nothing on screen saying why."""
        text = speakable(str(payload.get("text") or ""))[:REPLAY_MAX_CHARS]
        if read_mute()["muted"]:
            return self._send(
                json.dumps({"ok": False, "reason": "quiet mode"}).encode(),
                "application/json",
                409,
            )
        elif not text:
            return self._send(
                json.dumps({"ok": False, "reason": "empty text"}).encode(),
                "application/json",
                400,
            )
        else:
            voice = resolve_replay_voice(
                str(payload.get("voice") or ""),
                str(payload.get("persona") or ""),
                kokoro_voices(),
            )
            audio, error, hit = cached_speech(text, voice, REPLAY_SPEED)
            if error:
                log_line(f"replay: {voice} synthesis failed — {error}")
                return self._send(
                    json.dumps({"ok": False, "reason": replay_failure_reason(error)}).encode(),
                    "application/json",
                    503,
                )
            else:
                return self._send(
                    audio,
                    "audio/mpeg",
                    200,
                    (("X-Replay-Voice", voice), ("X-Replay-Cached", "1" if hit else "0")),
                )

    def _handle_json_post(self, route: str, length: int):
        payload = parse_json(self.rfile.read(length).decode() or "{}") or {}
        if route == "/voice":
            voice = payload.get("voice")
            if voice in VOICE_ROSTER:
                OVERRIDE_FILE.write_text(json.dumps({"voice": voice}))
                return self._send(json.dumps({"ok": True, "voice": voice}).encode(), "application/json")
            else:
                return self._send(json.dumps({"ok": False, "error": "unknown voice"}).encode(), "application/json", 400)
        elif route == "/mute":
            muted = bool(payload.get("muted"))
            # Order is the guarantee: the flag lands on disk BEFORE we try to
            # cut, so mute always succeeds even if the cut cannot.
            MUTE_FILE.write_text(json.dumps({"muted": muted}))
            cut_speech_for_mute(muted)
            return self._send(json.dumps({"ok": True, "muted": muted}).encode(), "application/json")
        elif route == "/skip":
            skipped = skip_current_utterance()
            return self._send(
                json.dumps({"ok": skipped["ok"], "error": skipped["error"]}).encode(),
                "application/json",
            )
        elif route == "/standby":
            standby = bool(payload.get("standby"))
            write_standby(standby, time.time())
            return self._send(json.dumps({"ok": True, "standby": standby}).encode(), "application/json")
        elif route == "/tasks":
            items = payload.get("items")
            if isinstance(items, list):
                TASKS_FILE.write_text(json.dumps({"items": stamp_tasks(items)}))
                return self._send(json.dumps({"ok": True, "count": len(items)}).encode(), "application/json")
            else:
                return self._send(json.dumps({"ok": False, "error": "items must be a list"}).encode(), "application/json", 400)
        elif route == "/inbox":
            text = str(payload.get("text") or "").strip()[:2000]
            if text:
                count = enqueue_inbox_text(text)
                return self._send(json.dumps({"ok": True, "count": count}).encode(), "application/json")
            else:
                return self._send(json.dumps({"ok": False, "error": "empty text"}).encode(), "application/json", 400)
        elif route == "/inbox/clear":
            INBOX_FILE.write_text(json.dumps({"items": []}))
            mark_inbox_history_seen()
            return self._send(json.dumps({"ok": True}).encode(), "application/json")
        elif route == "/say":
            text = str(payload.get("text") or "").strip()[:4000]
            persona = str(payload.get("persona") or "").strip()[:40]
            if text:
                append_say(text, persona)
                return self._send(json.dumps({"ok": True}).encode(), "application/json")
            else:
                return self._send(json.dumps({"ok": False, "error": "empty text"}).encode(), "application/json", 400)
        elif route == "/replay":
            return self._handle_replay(payload)
        elif route == "/spawn":
            result = spawn_session(payload.get("model"), payload.get("effort"))
            return self._send(json.dumps(result).encode(), "application/json", 200 if result["ok"] else 429)
        else:
            return self._send(b"not found", "text/plain", 404)


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
