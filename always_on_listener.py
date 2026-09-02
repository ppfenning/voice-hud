#!/usr/bin/env python3
"""Always-on wake listener — hears "hey Jarvis" (and, as a transition alias,
"hey Bella") for the whole life of a voice session, not only while the
session happens to be in standby.

THE GAP THIS CLOSES: wake_listener.py only owns the mic while standby is
true. The moment Claude is busy — running tools, waiting on agents, mid-
reply — nothing is listening, so Pat physically cannot start a conversation
by voice and has to type into the HUD inbox instead ("listening mode isn't
something that I can always go into... we should have the context window
open for us to be able to talk"). This daemon is meant to run continuously
for the whole session instead, in parallel with (or in place of) the
standby-gated wake_listener.py.

On hearing the wake phrase it captures whatever the user says next and POSTs
it to the HUD inbox (POST /inbox), same path as the typed/dictated inbox.
An inbox post counts as activity for server.py's auto-idle standby (see its
read_standby() docstring), so this both wakes a session that had gone idle
AND queues a directive for a busy one — the session picks it up at its next
inbox poll either way.

Safety guarantees, each with one function below:
  - Absolute mute       muted_now() gates every recording; /mute == true means
                         nothing is captured, full stop. Checked before every
                         chunk, not just once per loop.
  - Never self-hears     speaking_now() polls /state.json's `status` field;
                         "speaking" (TTS playing) pauses capture, plus
                         SPEAKING_COOLDOWN_SECONDS after it ends, so trailing
                         TTS audio in the mic buffer can't leak in and self-
                         trigger a wake.
  - Phrase, not name     WAKE_WORDS are phrases ("hey bella", "okay bella",
                         ...), never the bare name — "Bella" alone comes up
                         constantly in normal speech/meetings.
  - Energy gate          RMS_GATE — silence is never sent to whisper.
  - Anti-hallucination   whisper emits stock filler ("Thank you.", captioning
                         boilerplate) on near-silence even past the energy
                         gate. wake_guard.judge_directive() catches that
                         before anything is posted, in tiers: the old phrase
                         blocklist, then the decoder's own confidence tells,
                         then a fast accept for obviously substantive text,
                         then `claude -p` for the ambiguous residue only.
                         Switch it off with VOICEHUD_DIRECTIVE_GATE=off,
                         which restores the pre-08-21 blocklist exactly.
                         See handle_wake().
  - Bare wake is a wake  a matched phrase with NO directive after it posts the
                         WAKE_ONLY sentinel rather than nothing — see
                         handle_wake(). Guards are unchanged; only the
                         empty-remainder case stopped being silent.
  - No double-post       a small debounce file (DEBOUNCE_FILE) records the
                         last successful post; a second wake — or a second
                         instance of this script sharing the same mic — within
                         WAKE_DEBOUNCE_SECONDS is a silent no-op.

Also writes a heartbeat file every loop tick so the HUD server can report
whether this daemon is alive (server.py's read_listening(), surfaced as
state.json's "listening" field and the WAKE LISTENER readout on the page).
That heartbeat carries CAPTURE EVIDENCE, not just a timestamp — see
touch_heartbeat(). "The loop is turning" and "we can hear you" are
different facts, and conflating them is what let this daemon sit deaf for a
whole working session behind a green ARMED indicator (2026-08-18).

WHY THE WAKE WORD DIED MID-TASK (root cause, 2026-08-18): nothing here ever
paused on "busy" — the gate is `status == "speaking"` and there is no
working/busy status at all. The daemon had gone deaf at the device layer.
PortAudio caches its device table inside Pa_Initialize() and never re-reads
CoreAudio, so when Hammerspoon flipped the system default input (Pixel Buds
<-> dock mic — routine on this machine), this process kept resolving and
recording the OLD device. When that device was gone the read hung; when it
was merely no longer routed the read SUCCEEDED and returned digital zeros
forever, raising nothing. Error-driven recovery cannot see the second case
at all. Worse, in the direction that matters most here the OLD device keeps
working: with the stream on the Pixel Buds, moving the system default to
the built-in mic changed nothing at all — same level, no error, no stall —
the daemon simply listened to the wrong microphone indefinitely.

The fixes: os_default_input_id() reads the live route straight from
CoreAudio (PortAudio structurally cannot answer this), and
maybe_rebind_if_device_moved() follows it; rebind_stream() re-initializes
PortAudio before re-resolving (measurement in default_input_index());
device identity is (index, NAME) because reinit renumbers indices
(device_identity()); and maybe_rebind_if_deaf() is the backstop for a
device that goes silent in place rather than moving.

Complements wake_listener.py rather than editing it: that script is the
lighter standby-only listener and is left as-is. Running both at once against
the same mic is unnecessary — once this daemon is running, standby wake is
already covered via the /inbox path (server.py's read_standby() auto-clears
on any inbox activity — see its docstring).

AUDIO QUALITY (fixed 2026-08-17): the ORIGINAL implementation called
sd.rec() per chunk — which opens AND closes a CoreAudio input stream every
single call, roughly every 0.3-2s. That constant device churn was found to
disrupt concurrent TTS playback (static/crackle) on Pat's DisplayLink dock,
a device already known to be picky about buffering (see
sitecustomize-portaudio-latency.py). Fixed by opening ONE long-lived
sd.InputStream (open_stream()) and reading from it continuously
(read_chunk()) instead — the device is opened once and stays open for the
life of the process, reopened only on an actual device/stream error, never
on a mute/speaking/cooldown gate. See the 2026-08-17 verification note in
the repo (or ask Pat) for the actual before/after listening comparison —
don't take the mechanism as proof; PortAudio/CoreAudio buffering issues are
notoriously hardware- and driver-specific.

RESILIENCE (added 2026-08-17, after a silent death): PortAudio/sounddevice
can throw when the input device it resolved disappears mid-capture —
Bluetooth earbuds going out of range, dying, or going back in the case is
routine, not exceptional. read_chunk() re-resolves the CURRENT default
input device fresh whenever it needs to (re)open the stream (never caches
a device across a reopen) and, on a device/stream error, closes the
stream, forces PortAudio to rescan (refresh_devices()), and retries rather
than raising — the NEXT read transparently reopens on whatever the current
default now is. listen_forever()'s loop body is additionally wrapped in a
catch-all so ANY unexpected exception is logged with a full traceback
(log_exception(), which goes to stderr — captured to a file by whatever
launched this: see com.voicemode.always-on-listener.plist) before the loop
continues; the failure mode this replaces was total silence, which is worse
than a noisy log. acquire_singleton_lock() makes a second launch (manual, or
a launchd race) a safe no-op instead of two processes fighting over the mic
and double-posting.

Run with the voice-mode venv python (has sounddevice + numpy):
  ~/.local/share/uv/tools/voice-mode/bin/python always_on_listener.py

Normally supervised by launchd (com.voicemode.always-on-listener.plist,
KeepAlive) so a crash the code itself can't prevent still self-heals within
seconds instead of needing Pat to notice and restart it by hand.
"""
import ctypes
import io
import json
import os
import queue
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
import wave
from pathlib import Path

import numpy as np
import sounddevice as sd

from wake_guard import RMS_GATE, decoder_signals, describe, safe_judge_directive, transcript_text

HUD = "http://127.0.0.1:8123"
WHISPER = os.environ.get("VOICE_HUD_WHISPER_URL", "http://127.0.0.1:2022/v1/audio/transcriptions")
HUD_DIR = Path(__file__).resolve().parent
HEARTBEAT_FILE = HUD_DIR / "listening_heartbeat.json"
DEBOUNCE_FILE = HUD_DIR / "wake_debounce.json"
PID_FILE = HUD_DIR / "always_on_listener.pid"

RATE = 16000
WAKE_CHUNK_SECONDS = 2.0        # one poll's worth of audio while listening for the phrase
UTTER_CHUNK_SECONDS = 0.8       # continuation poll size once capturing the request
SILENCE_HANG_SECONDS = 1.2      # trailing quiet that ends utterance capture
MAX_UTTERANCE_SECONDS = 30.0    # hard cap regardless of silence (requirement 5)
SPEAKING_COOLDOWN_SECONDS = 1.0  # grace after TTS ends before listening resumes
PAUSE_POLL_SECONDS = 0.3        # recheck interval for mute/speaking while paused
WAKE_DEBOUNCE_SECONDS = 4.0     # min gap between two successful inbox posts
DEVICE_ERROR_BACKOFF_SECONDS = 0.5  # FIRST pause between record retries after
                                     # a device/stream error; doubles per
                                     # consecutive failure up to the max
                                     # below, so a vanished input can never
                                     # spin the CPU while it's gone
DEVICE_ERROR_BACKOFF_MAX_SECONDS = 8.0

SIGNAL_FLOOR = 1e-6             # below this a chunk is DIGITAL silence (a
                                 # stream bound to a device that isn't
                                 # actually routed returns literal zeros),
                                 # NOT a quiet room. Measured 2026-08-18 on
                                 # Pat's machine, both 2s captures, same
                                 # silent room: the live default input
                                 # (Pixel Buds) gave rms=8.8e-5 with
                                 # 12194/32000 nonzero frames, while the
                                 # non-routed built-in mic gave rms=0.0 with
                                 # 0/32000 nonzero frames and max_abs=0.
                                 # Two orders of magnitude of daylight, so
                                 # this distinguishes "deaf" from "quiet"
                                 # without ever confusing the two.
DEAF_REBIND_SECONDS = 60.0      # pure digital silence for this long means the
                                 # stream is alive but hearing nothing —
                                 # re-acquire the current default input
                                 # rather than keep pretending (see
                                 # maybe_rebind_if_deaf())
SELF_AUDIO_TAIL_SECONDS = 0.5   # margin added after our own TTS playback ends
                                 # before a wake window is trusted again. Kept
                                 # SMALL on purpose: measured 2026-08-18 the
                                 # closest legitimate capture after a playback
                                 # end was 4s (Pat's real "Hey, Bella, what's
                                 # the status?" at +4s), so a large margin
                                 # would start eating real wakes to defend
                                 # against a leak that does not occur.
WAKE_OVERLAP_SECONDS = 1.0      # how much of the previous chunk is replayed
                                 # at the head of the next transcription
                                 # window. WITHOUT this, wake detection read
                                 # strictly consecutive 2s blocks and any
                                 # phrase straddling a block boundary was
                                 # split across two whisper calls and
                                 # matched by neither — for a ~0.75s "hey
                                 # bella" against a 2.0s block that is a
                                 # ~37% miss rate on a mic that is working
                                 # perfectly. One second of overlap
                                 # guarantees any phrase up to 1s long lands
                                 # whole inside at least one window. The
                                 # duplicate match a phrase near a boundary
                                 # can now produce is already handled by
                                 # debounced().
LEVEL_WINDOW_SECONDS = 600.0    # rolling window over which the loudest chunk
                                 # is remembered, so the HUD can show how far
                                 # the mic actually gets toward RMS_GATE
                                 # rather than only whether bytes arrive
STALL_GRACE_SECONDS = 2.0       # after a stuck read times out, how long to
                                 # let the abandoned reader thread finish
                                 # before its stream is treated as
                                 # permanently hung — see recover_stream()

# Phrases, not the bare name — identical list to wake_listener.py so the two
# daemons agree on what counts as a wake. A lone "Jarvis" (or "Bella") in
# overheard audio (meetings, TV, normal conversation) must never trigger.
# 2026-08-19 JARVIS rebrand: jarvis phrases ADDED alongside the bella ones —
# bella stays as a transition alias until Pat says to drop it. "jervis" is
# whisper's common mis-transcription of the name, same role "bela" plays
# for bella.
WAKE_WORDS = (
    "hey jarvis", "hey, jarvis", "wake up jarvis", "wake up, jarvis",
    "okay jarvis", "ok jarvis", "hey jervis", "hey, jervis",
    "hey bella", "hey, bella", "wake up bella", "wake up, bella",
    "okay bella", "ok bella", "hey bela", "hey, bela",
)


def hud_get(path: str) -> dict:
    try:
        with urllib.request.urlopen(f"{HUD}{path}", timeout=2) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, ValueError, OSError):
        return {}


def hud_post(path: str, payload: dict) -> None:
    try:
        req = urllib.request.Request(
            f"{HUD}{path}", data=json.dumps(payload).encode(), method="POST"
        )
        urllib.request.urlopen(req, timeout=2).read()
    except (urllib.error.URLError, OSError):
        pass
    return None


def to_wav_bytes(pcm: np.ndarray) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def transcribe(pcm: np.ndarray) -> str:
    boundary = uuid.uuid4().hex
    wav = to_wav_bytes(pcm)
    body = b"".join(
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="chunk.wav"\r\n'
            "Content-Type: audio/wav\r\n\r\n".encode(),
            wav,
            f"\r\n--{boundary}--\r\n".encode(),
        )
    )
    req = urllib.request.Request(
        WHISPER,
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return (json.loads(resp.read()).get("text") or "").strip()
    except (urllib.error.URLError, ValueError, OSError):
        return ""


def transcribe_detailed(pcm: np.ndarray) -> tuple:
    """Same transcription, asking for verbose_json so the decoder's own
    confidence signals come back with the text — see wake_guard's tier 1.
    Returns (text, signals).

    Deliberately a SEPARATE function rather than a flag on transcribe():
    transcribe() is what the wake-word DETECTION path calls on every chunk
    above the gate, and that path is the one thing in this daemon that must
    never regress. It is left byte-for-byte alone. This runs once, on the
    captured utterance, where the extra fields are actually wanted — so
    there is no additional whisper call, just a richer response.

    Falls back to transcribe() if the verbose request fails for any reason
    (an older whisper build, a rejected form field, a transport error), so
    the worst case is exactly today's behaviour with tier 1 abstaining —
    never a lost directive."""
    boundary = uuid.uuid4().hex
    wav = to_wav_bytes(pcm)
    body = b"".join(
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="response_format"\r\n\r\nverbose_json\r\n'
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="temperature"\r\n\r\n0\r\n'
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="chunk.wav"\r\n'
            "Content-Type: audio/wav\r\n\r\n".encode(),
            wav,
            f"\r\n--{boundary}--\r\n".encode(),
        )
    )
    req = urllib.request.Request(
        WHISPER,
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            parsed = json.loads(resp.read())
        return (transcript_text(parsed), decoder_signals(parsed))
    except (urllib.error.URLError, ValueError, OSError):
        log_line("verbose transcription failed — falling back to plain json, gate tier 1 abstains")
        return (transcribe(pcm), {})


def log_exception(context: str) -> None:
    """Never die silently: any unexpected failure gets a timestamped header
    plus the full traceback on stderr — captured to a file by whatever
    launched this process (launchd's StandardErrorPath, or a manual `2>&1
    >logfile`). An empty log after a death is the failure mode this exists
    to prevent."""
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {context}", file=sys.stderr, flush=True)
    traceback.print_exc(file=sys.stderr)
    sys.stderr.flush()
    return None


def refresh_devices() -> None:
    """Force PortAudio to rescan its device table. Fixes the well-known
    CoreAudio/PortAudio staleness where the previously-resolved default
    input device vanishing (Bluetooth out of range, USB unplugged) makes
    even a FRESH sd.rec() call error out until the host API is reset. Cheap
    and safe to call any time, not just on error."""
    sd._terminate()
    sd._initialize()
    return None


class _CoreAudioAddress(ctypes.Structure):
    _fields_ = (
        ("mSelector", ctypes.c_uint32),
        ("mScope", ctypes.c_uint32),
        ("mElement", ctypes.c_uint32),
    )


def _load_coreaudio():
    try:
        return ctypes.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
    except OSError:
        return None


_COREAUDIO = _load_coreaudio()
_CA_SYSTEM_OBJECT = 1
_CA_DEFAULT_INPUT = int.from_bytes(b"dIn ", "big")   # kAudioHardwarePropertyDefaultInputDevice
_CA_SCOPE_GLOBAL = int.from_bytes(b"glob", "big")    # kAudioObjectPropertyScopeGlobal


def os_default_input_id():
    """The AudioDeviceID the OPERATING SYSTEM is currently routing input
    through, read straight from CoreAudio.

    This exists because PortAudio cannot answer the question. PortAudio
    caches its device table in Pa_Initialize() and only rebuilds it on a
    terminate/initialize cycle, which also destroys the open stream — so a
    long-running capture process has no way to ask "has the mic moved?"
    without first throwing away the stream it is asking on behalf of. This
    call is the cheap out-of-band answer: no subprocess, no PortAudio, no
    dependency on the brew-installed SwitchAudioSource, and it changes the
    instant the system default changes (verified 2026-08-18: id 82 for the
    built-in mic, 93 for the Pixel Buds, flipping live with the route).

    Only the numeric id is read, never the name — the id is all that's
    needed to detect a change, and the name comes from PortAudio after the
    rebind. Returns None if CoreAudio is unavailable or the call fails, in
    which case the silence watchdog is the remaining safety net."""
    if _COREAUDIO is None:
        return None
    else:
        address = _CoreAudioAddress(_CA_DEFAULT_INPUT, _CA_SCOPE_GLOBAL, 0)
        out = ctypes.c_uint32(0)
        size = ctypes.c_uint32(ctypes.sizeof(out))
        try:
            status = _COREAUDIO.AudioObjectGetPropertyData(
                ctypes.c_uint32(_CA_SYSTEM_OBJECT), ctypes.byref(address),
                ctypes.c_uint32(0), None, ctypes.byref(size), ctypes.byref(out),
            )
        except Exception:
            return None
        return out.value if status == 0 else None


def default_input_index() -> int:
    """PortAudio's idea of the current default input device.

    ⚠️ THIS IS ONLY AS FRESH AS THE LAST refresh_devices(). PortAudio
    enumerates devices once, inside Pa_Initialize(), and caches that table
    for the life of the host API — it does NOT re-read CoreAudio on each
    query. So when the SYSTEM default input moves under a long-running
    process (Hammerspoon flipping between the Pixel Buds and the dock mic,
    which is routine here), this keeps returning the OLD device forever.

    Measured 2026-08-18, one process, OS default moved built-in mic ->
    Pixel Buds mid-run:
        [fresh process]        PA default idx=6 'MacBook Pro Microphone'
        [after switch, no reinit] PA default idx=6 'MacBook Pro Microphone'  <- STALE
        [after switch, w/ reinit]  PA default idx=0 "Patrick's Pixel Buds Pro 2"

    That is the whole bug: ensure_stream() used to treat this value as
    live, so a default-input change that raised no error was invisible and
    the daemon happily kept recording a device nobody was talking into.
    Anything that needs the CURRENT default must call rebind_stream()
    (which re-initializes first), not just re-read this."""
    return sd.query_devices(kind="input")["index"]


def device_identity(index: int) -> tuple:
    """(index, name) — the NAME is what actually identifies a device across
    a PortAudio re-initialization, because _terminate()/_initialize()
    RENUMBERS the table. Observed 2026-08-18: 'MacBook Pro Microphone' was
    index 2, 3 and 6 across three consecutive processes on this machine.
    Comparing bare indices can therefore report "same device" when the index
    now points at a completely different mic, which is exactly how a
    recovery can look successful while binding to the wrong input."""
    try:
        return (index, sd.query_devices(index)["name"])
    except Exception:
        return (index, None)


_stream_state = {"stream": None, "identity": None}  # the ONE long-lived input
                                    # stream — see open_stream() for why this
                                    # replaced a fresh sd.rec() per chunk.
                                    # `identity` is (index, name), never a
                                    # bare index — see device_identity().


def open_stream(device: int) -> sd.InputStream:
    """Open and start ONE input stream on the given device index. Called
    once at startup and again only when the device actually needs to
    change (an error, or ensure_stream() noticing the default moved) —
    never per chunk. The old code called sd.rec() (which opens AND closes a
    CoreAudio stream every single call) roughly every 0.3-2s; that churn
    was found (2026-08-17) to be disrupting concurrent TTS playback on
    Pat's fragile DisplayLink dock (static/crackle). A single persistent
    stream removes the churn entirely. No explicit `latency=` — inherits
    the venv's sitecustomize.py `sd.default.latency = ("high", "high")`,
    the same fix already applied to TTS output."""
    stream = sd.InputStream(samplerate=RATE, channels=1, dtype="int16", device=device)
    stream.start()
    return stream


def ensure_stream() -> sd.InputStream:
    """Returns the current stream, (re)opening it if there isn't one yet OR
    if PortAudio's default input has moved since it was opened.

    NOTE the limit of that second condition, which the previous version of
    this docstring got wrong: it only sees a move that PortAudio's CACHED
    device table already knows about (see default_input_index()). A live
    OS-level route change is invisible here until something calls
    refresh_devices(). That gap is now covered from two sides —
    recover_stream() for a device that errors or hangs, and
    maybe_rebind_if_deaf() for a device that keeps returning digital
    silence — both of which go through rebind_stream()."""
    current_default = default_input_index()
    identity = device_identity(current_default)
    if _stream_state["stream"] is None or _stream_state["identity"] != identity:
        close_stream_quietly()
        stream = open_stream(current_default)
        _stream_state["stream"] = stream
        _stream_state["identity"] = identity
        _capture_state["device_index"] = identity[0]
        _capture_state["device_name"] = identity[1]
        # Stamp WHICH OS route this stream was opened against, so
        # maybe_rebind_if_device_moved() has something truthful to compare
        # against later. Recorded at open time, never inferred afterwards.
        _capture_state["os_device_id"] = os_default_input_id()
        return stream
    else:
        return _stream_state["stream"]


def close_stream_quietly() -> None:
    stream = _stream_state["stream"]
    _stream_state["stream"] = None
    _stream_state["identity"] = None
    if stream is None:
        return None
    else:
        try:
            stream.close()
        except Exception:
            pass
        return None


def rebind_stream(reason: str) -> None:
    """FULL re-acquisition against the device the OS is actually using now:
    drop the stream, force PortAudio to rebuild its device table, and let
    the next ensure_stream() resolve the default from that fresh table.

    The re-initialize is the load-bearing half, and it is why plain
    "close and reopen" recovery could report success while staying deaf —
    reopening without it just rebinds to the same stale device (see the
    measurement in default_input_index()). Deliberately does NOT open the
    new stream itself: the very next ensure_stream() does that, so there is
    exactly one place that opens streams."""
    _capture_state["reason"] = reason
    _capture_state["rebinds"] += 1
    _capture_state["last_rebind_ts"] = time.time()
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] rebind_stream: {reason}", file=sys.stderr, flush=True)
    close_stream_quietly()
    refresh_devices()
    touch_heartbeat("recovering")
    return None


READ_STALL_SECONDS = 5.0  # margin added to the requested chunk length before
                          # a blocking stream.read() counts as stuck, not
                          # just slow
MAX_CONSECUTIVE_STREAM_FAILURES = 6  # this many recoveries in a row with no
                          # successful read in between means THIS process's
                          # PortAudio instance is unrecoverably poisoned —
                          # see recover_stream()

_stream_failures = {"count": 0}

# Evidence that this daemon can actually HEAR, as opposed to merely still
# being scheduled. Everything here is written into the heartbeat file so the
# HUD can tell those two states apart — see touch_heartbeat(). Before this,
# the heartbeat carried a bare {"ts": ...} that was touched from the PAUSED
# loop and from the FAILURE path alike, so a daemon stuck recovering forever,
# or one bound to a device returning pure silence, both read as a healthy
# green ARMED on the page. A frozen indicator that still looks alive is worse
# than one that admits it is dead (Pat, 08-18).
_capture_state = {
    "started_ts": time.time(),
    "last_read_ts": 0.0,    # last read that RETURNED, silence or not
    "last_signal_ts": 0.0,  # last read carrying real signal (> SIGNAL_FLOOR)
    "rms": 0.0,
    "device_index": None,
    "device_name": None,
    "mode": "starting",     # starting | capturing | paused | recovering
    "rebinds": 0,
    "last_rebind_ts": 0.0,
    "reason": "",
    "os_device_id": None,   # CoreAudio AudioDeviceID the open stream was
                            # bound against — see maybe_rebind_if_device_moved()
    # LEVEL evidence, distinct from SIGNAL evidence above and added
    # 2026-08-18 after the signal-only version proved insufficient.
    # SIGNAL_FLOOR (1e-6) answers "is audio arriving"; RMS_GATE (0.006)
    # answers "is that audio loud enough to be worth transcribing", and
    # everything BETWEEN those two numbers is a listener that is alive,
    # capturing, reporting itself perfectly healthy, and structurally
    # incapable of ever hearing a wake word — because listen_forever()
    # discards every sub-gate chunk before whisper ever sees it. Pat sat in
    # that band (measured 1.6e-5, i.e. 0.003x the gate) and found out by
    # talking to a machine that could not hear him while the HUD showed
    # green. These fields exist so that band has a name and a number.
    "levels": (),           # rolling ((ts, rms), ...) over LEVEL_WINDOW_SECONDS
    "above_gate_ts": 0.0,   # last chunk that actually reached RMS_GATE
}

# Reader threads left blocked inside stream.read() by a timed-out chunk.
# Tracked, not forgotten: re-initializing PortAudio while one of these is
# still inside Pa_ReadStream on the stream being torn down is a use-after-
# free, and is the most likely way to poison this process's whole audio
# stack rather than recover it. See recover_stream().
_abandoned = {"threads": ()}


def abandoned_readers_alive() -> int:
    live = tuple(t for t in _abandoned["threads"] if t.is_alive())
    _abandoned["threads"] = live
    return len(live)


def backoff_seconds(failures: int) -> float:
    """Bounded exponential backoff: 0.5, 1, 2, 4, 8, 8, ... A vanished
    device must never be retried in a hot loop, and must never be retried so
    slowly that a device that came back sits unnoticed for minutes."""
    return min(
        DEVICE_ERROR_BACKOFF_SECONDS * (2 ** max(0, failures - 1)),
        DEVICE_ERROR_BACKOFF_MAX_SECONDS,
    )


def _blocking_read(stream, frames: int, out_q: "queue.Queue") -> None:
    """Runs on its own thread so read_chunk() can put a timeout around a
    call that has no timeout parameter of its own. NOT joined/cancelled on
    timeout — see recover_stream()'s docstring for why."""
    try:
        data, _overflowed = stream.read(frames)
        out_q.put(("ok", data))
    except Exception as e:
        out_q.put(("error", e))


def recover_stream(reason: str) -> None:
    """The one recovery path for both a RAISED device/stream error and a
    SILENTLY stuck read (2026-08-17, root-caused from a live failure after
    Pat swapped audio dongles): PortAudio enumerates devices once and
    caches that table. Once the device set changes underneath a running
    process, the cached table goes stale and every subsequent InputStream
    open can fail — or, worse, appear to open fine and then never deliver
    another frame, so a plain stream.read() blocks forever with NO
    exception raised, which is exactly what read_chunk()'s old except-only
    handling could never catch. So: close whatever stream we hold, then
    re-INITIALIZE PortAudio itself (refresh_devices(), not just a reopen)
    so the NEXT ensure_stream() call re-resolves the device against a
    freshly rebuilt table instead of repeating the same failure against
    the same stale one. Tracks consecutive failures with no successful
    read in between; past MAX_CONSECUTIVE_STREAM_FAILURES this process's
    audio stack is treated as unrecoverably poisoned and the process exits
    non-zero on purpose so launchd's KeepAlive starts a clean one — a
    process that stays "alive" but can never hear anything again is worse
    than a crash, because a supervisor has nothing to react to. The HUD's
    listening.alive heartbeat check is what actually caught this failure
    live (heartbeat froze, page correctly showed OFFLINE) — that honesty
    stays regardless of this fix.

    2026-08-18 correction, and the reason this is not just a reopen: the
    stuck-read path leaves a reader thread blocked inside Pa_ReadStream on
    a stream it still holds, and the old code called sd._terminate()
    straight through that. Pa_Terminate() closes open streams out from
    under whoever is inside them — a use-after-free on the exact code path
    we reach ONLY when the audio stack is already misbehaving. So now the
    abandoned reader gets STALL_GRACE_SECONDS to come back (a merely slow
    read finishes and we recover in-process as before); if it is still
    wedged after that, re-initializing is unsafe, and the honest move is to
    exit for the supervisor rather than corrupt this process's audio stack
    and then spend six more failures discovering that. launchd's KeepAlive
    has a clean replacement listening again within a couple of seconds."""
    log_exception(f"recover_stream: {reason}")
    close_stream_quietly()
    _capture_state["reason"] = reason
    touch_heartbeat("recovering")
    grace_deadline = time.time() + STALL_GRACE_SECONDS
    while abandoned_readers_alive() > 0 and time.time() < grace_deadline:
        time.sleep(0.1)
    if abandoned_readers_alive() > 0:
        print(
            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] recover_stream: a reader "
            f"thread is STILL blocked inside stream.read() after "
            f"{STALL_GRACE_SECONDS:.0f}s — re-initializing PortAudio "
            "underneath it would close that stream out from under a live "
            "Pa_ReadStream call, so this process is exiting non-zero "
            "instead and letting the supervisor start a clean one",
            file=sys.stderr, flush=True,
        )
        raise SystemExit(3)
    else:
        pass
    refresh_devices()
    _stream_failures["count"] += 1
    if _stream_failures["count"] < MAX_CONSECUTIVE_STREAM_FAILURES:
        time.sleep(backoff_seconds(_stream_failures["count"]))
        return None
    else:
        print(
            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] recover_stream: "
            f"{_stream_failures['count']} consecutive failures with no "
            "successful read in between, despite re-initializing PortAudio "
            "each time — this process's audio stack looks unrecoverably "
            "poisoned; exiting non-zero so the supervisor starts a clean "
            "process instead of leaving a zombie listener running",
            file=sys.stderr, flush=True,
        )
        raise SystemExit(1)


def read_chunk(seconds: float) -> np.ndarray:
    """Read one chunk from the single long-lived input stream (never a
    fresh sd.rec() — see open_stream()). A vanished input mid-session is
    routine (Bluetooth earbuds die, get put away, wander out of range;
    Pat swapping dongles hit the same path 2026-08-17), not exceptional:
    on a device/stream error, or a read that never returns at all (see
    recover_stream()), this recovers and retries rather than raising or
    hanging — the caller never sees a device-change interruption, only a
    short delay, up to MAX_CONSECUTIVE_STREAM_FAILURES before this process
    gives up and exits for the supervisor to replace it. Touches the
    heartbeat on every recovery attempt so a prolonged outage still reads
    as alive-and-retrying rather than dead, right up until it's actually
    replaced."""
    frames = max(1, int(seconds * RATE))
    while True:
        stream = ensure_stream()
        result_q = queue.Queue(maxsize=1)
        reader = threading.Thread(target=_blocking_read, args=(stream, frames, result_q), daemon=True)
        reader.start()
        try:
            kind, payload = result_q.get(timeout=seconds + READ_STALL_SECONDS)
        except queue.Empty:
            # The reader thread may still be blocked inside stream.read()
            # on this exact stream object — closing it from here would
            # race with that call, so it's deliberately abandoned (leaked)
            # rather than closed. It is REMEMBERED though, in _abandoned:
            # recover_stream() must not re-initialize PortAudio while one
            # of these is still inside Pa_ReadStream.
            _abandoned["threads"] = _abandoned["threads"] + (reader,)
            _stream_state["stream"] = None
            _stream_state["identity"] = None
            recover_stream(f"stream.read() did not return within {seconds + READ_STALL_SECONDS:.1f}s — stuck stream, abandoning it")
            continue
        if kind == "ok":
            return note_successful_read(np.asarray(payload).reshape(-1))
        else:
            recover_stream(f"stream error ({payload})")
            continue


def note_successful_read(pcm: np.ndarray) -> np.ndarray:
    """Record the evidence that this read actually carried audio, and pass
    the chunk straight through.

    THREE different facts, deliberately kept apart, because collapsing any
    two of them is how this daemon has now lied twice:
      last_read_ts    the stream is delivering bytes at all
      last_signal_ts  those bytes are real sound, not the digital zeros a
                      mis-bound device returns forever without erroring
      above_gate_ts   that sound actually reached RMS_GATE, i.e. was loud
                      enough that listen_forever() would forward it to
                      whisper instead of discarding it
    Only the third one means a wake word could ever have been heard. A
    daemon can satisfy the first two indefinitely and still be completely
    deaf in the only sense Pat cares about.

    above_gate_ts is updated only while actually listening, never while
    paused for TTS: on in-ear buds the daemon's own speech leaks into the
    mic, and letting that count as "we can hear" would suppress exactly the
    warning this is here to raise."""
    now = time.time()
    level = rms_of(pcm)
    _stream_failures["count"] = 0
    _capture_state["last_read_ts"] = now
    _capture_state["rms"] = level
    _capture_state["levels"] = tuple(
        (ts, r) for ts, r in _capture_state["levels"] + ((now, level),)
        if now - ts <= LEVEL_WINDOW_SECONDS
    )
    if level >= RMS_GATE and _capture_state["mode"] == "capturing":
        _capture_state["above_gate_ts"] = now
    else:
        pass
    if level > SIGNAL_FLOOR:
        _capture_state["last_signal_ts"] = now
        return pcm
    else:
        return pcm


def peak_level() -> float:
    """Loudest chunk seen in the rolling window. This is the number that
    says whether the mic can REACH the gate, as opposed to whether it is
    currently being spoken into."""
    levels = _capture_state["levels"]
    return max((r for _, r in levels), default=0.0)


def silent_seconds(now: float) -> float:
    """How long we have gone without a single frame of real audio. Measured
    from process start when nothing has ever been heard, so a daemon that
    comes up already bound to a dead device is caught too, not only one that
    goes deaf later."""
    return now - (_capture_state["last_signal_ts"] or _capture_state["started_ts"])


def maybe_rebind_if_device_moved() -> None:
    """PRIMARY watchdog: the OS moved the mic, so move with it.

    This is the one that actually fixes Pat's symptom, and it is separate
    from the silence check below because the two failures look nothing
    alike. Measured 2026-08-18 on this machine: with the listener bound to
    the Pixel Buds, switching the system default input to the built-in mic
    produced NO error, NO stall, and NO drop in level — the buds kept
    handing over perfectly good audio (rms 1.3e-05, unchanged) while Pat's
    voice was going somewhere else entirely. Nothing in an error-driven or
    silence-driven design can see that; the listener just quietly listens
    to the wrong microphone forever, which is precisely how "hey Bella"
    stops working without a single thing looking wrong.

    So the OS route is compared directly, via CoreAudio, every ungated
    loop tick (~2.4s). Cheap: one property read, no subprocess, no
    PortAudio. The recorded id is updated by ensure_stream() when the new
    stream opens, so one OS change costs exactly one rebind and cannot
    loop."""
    current = os_default_input_id()
    bound = _capture_state["os_device_id"]
    if current is None or bound is None or current == bound:
        return None
    else:
        rebind_stream(
            f"system default input moved (CoreAudio device {bound} -> "
            f"{current}) while bound to {_capture_state['device_name']!r} — "
            "following the route"
        )
        return None


def maybe_rebind_if_deaf() -> None:
    """BACKSTOP watchdog, for the other shape of the same problem: a stream
    that reads happily, raises nothing, and returns pure digital silence
    forever because the device it is bound to is no longer actually
    carrying audio. No error is ever raised in that state either, and the
    OS default may not have moved at all (a device can go silent in place),
    so this catches what maybe_rebind_if_device_moved() cannot.

    Rebinding costs ~200ms and is a no-op if it lands on the same device,
    so acting on suspicion is cheap and refusing to act is what left Pat
    unable to say "hey Bella" for a whole working session.

    Rate-limited to one attempt per DEAF_REBIND_SECONDS, and deliberately
    called ONLY from listen_forever()'s ungated path — never mid-TTS and
    never mid-utterance, so it cannot reintroduce the device churn that
    open_stream() exists to avoid."""
    now = time.time()
    silence = silent_seconds(now)
    if silence > DEAF_REBIND_SECONDS and (now - _capture_state["last_rebind_ts"]) > DEAF_REBIND_SECONDS:
        rebind_stream(
            f"no real audio for {silence:.0f}s — stream is alive but "
            f"delivering digital silence (last rms={_capture_state['rms']:.6f}, "
            f"device={_capture_state['device_name']!r}); re-acquiring the "
            "current default input"
        )
        return None
    else:
        return None


def rms_of(pcm: np.ndarray) -> float:
    return float(np.sqrt(np.mean((pcm.astype(np.float32) / 32768.0) ** 2)))


def touch_heartbeat(mode: str = None) -> None:
    """Publish liveness AND capture evidence, because they are different
    facts and only the second one answers "can this thing hear me".

    `ts` is the old contract (the loop is turning) and still drives the
    HUD's alive/OFFLINE call. `capture_ts` is new and is the honest one:
    the last moment a chunk carried real audio. server.py's read_listening()
    reports alive-but-not-capturing as DEGRADED rather than ARMED — the
    state this daemon was silently sitting in while Pat couldn't wake it.

    Written via a temp file + os.replace so a poll that lands mid-write
    reads the previous whole document instead of a truncated one and
    flashes a false OFFLINE."""
    now = time.time()
    payload = {
        "ts": now,
        "mode": mode or _capture_state["mode"],
        "pid": os.getpid(),
        "capture_ts": _capture_state["last_signal_ts"],
        "read_ts": _capture_state["last_read_ts"],
        "silent_seconds": round(silent_seconds(now), 1),
        "rms": round(_capture_state["rms"], 6),
        # The gate is published rather than duplicated in server.py so the
        # HUD can show the ratio without importing wake_guard, and so the
        # page can never disagree with the daemon about what "usable" means.
        "gate": RMS_GATE,
        "peak_rms": round(peak_level(), 6),
        "above_gate_ts": _capture_state["above_gate_ts"],
        "started_ts": _capture_state["started_ts"],
        "level_window": LEVEL_WINDOW_SECONDS,
        "device": _capture_state["device_name"],
        "device_index": _capture_state["device_index"],
        "os_device_id": _capture_state["os_device_id"],
        "failures": _stream_failures["count"],
        "rebinds": _capture_state["rebinds"],
        "reason": _capture_state["reason"],
    }
    tmp = HEARTBEAT_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, HEARTBEAT_FILE)
    return None


def speaking_now() -> bool:
    return hud_get("/state.json").get("status") == "speaking"


def muted_now() -> bool:
    return bool(hud_get("/mute").get("muted"))


def wait_until_clear_to_listen() -> None:
    """Block until it's safe to record for real: not muted, not mid-TTS, and
    past the post-speech cooldown (requirements 2 and 3). While gated, this
    DRAINS the long-lived stream — reads and immediately discards
    PAUSE_POLL_SECONDS of audio — rather than sleeping with the stream idle:
    mute/speaking/cooldown honor the gate by discarding audio, not by
    tearing the stream down, so they can't reintroduce the open/close churn
    a persistent stream exists to avoid, and the input buffer never sits
    unconsumed long enough to overflow. The read samples never leave this
    function — not transcribed, not stored, not sent anywhere — so "nothing
    is captured" still holds in the sense that matters (nothing muted is
    ever processed or transmitted), even though the OS-level buffer briefly
    holds raw frames before they're thrown away. Touches the heartbeat on
    every poll tick, paused or not, so the HUD shows this daemon as alive
    even while it's discarding audio."""
    cooldown_until = 0.0
    while True:
        if muted_now():
            _capture_state["mode"] = "paused"
            touch_heartbeat()
            cooldown_until = 0.0
            read_chunk(PAUSE_POLL_SECONDS)
        elif speaking_now():
            _capture_state["mode"] = "paused"
            touch_heartbeat()
            cooldown_until = time.time() + SPEAKING_COOLDOWN_SECONDS
            read_chunk(PAUSE_POLL_SECONDS)
        elif time.time() < cooldown_until:
            _capture_state["mode"] = "paused"
            touch_heartbeat()
            read_chunk(PAUSE_POLL_SECONDS)
        else:
            _capture_state["mode"] = "capturing"
            touch_heartbeat()
            return None


def strip_wake_phrase(transcript: str) -> str:
    """Same-breath case (requirement 5): "hey bella, what's the status" ->
    drop the leading wake phrase (and stray punctuation right after it) so
    the remainder posts as the directive instead of being discarded. Longest
    phrase first so "hey bella" doesn't shadow "wake up bella" etc."""
    lowered = transcript.lower()
    for phrase in sorted(WAKE_WORDS, key=len, reverse=True):
        if lowered.startswith(phrase):
            return transcript[len(phrase):].lstrip(" ,.-").strip()
        else:
            continue
    return transcript.strip()


def heard_wake_word(transcript: str) -> bool:
    lowered = transcript.lower()
    return any(w in lowered for w in WAKE_WORDS)


def capture_utterance(initial_pcm: np.ndarray) -> np.ndarray:
    """After the wake-detect chunk, keep recording short chunks until a run
    of near-silent chunks totalling SILENCE_HANG_SECONDS is seen, or the hard
    cap is hit (requirement 5: "listen until a short silence, with a
    sensible max ~30s"). Stops promptly (without discarding what's already
    captured) if mute or TTS starts mid-capture — handle_wake()'s guards
    still decide whether the result is postable."""
    chunks = [initial_pcm]
    total_seconds = len(initial_pcm) / RATE
    silence_run = 0.0
    while total_seconds < MAX_UTTERANCE_SECONDS and silence_run < SILENCE_HANG_SECONDS:
        touch_heartbeat()  # a long utterance (up to MAX_UTTERANCE_SECONDS) must
                            # not itself read as a stale/dead daemon on the HUD
        if muted_now() or speaking_now():
            break
        else:
            chunk = read_chunk(UTTER_CHUNK_SECONDS)
            chunks.append(chunk)
            total_seconds += UTTER_CHUNK_SECONDS
            silence_run = silence_run + UTTER_CHUNK_SECONDS if rms_of(chunk) < RMS_GATE else 0.0
    return np.concatenate(chunks)


def read_json_file(path: Path) -> dict:
    return (json.loads(path.read_text()) or {}) if path.exists() else {}


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def acquire_singleton_lock() -> bool:
    """True once this process owns PID_FILE — False means another instance
    is already alive and this one should exit quietly instead of fighting
    it for the mic (two daemons sharing one mic would double-post
    directives, on top of debounced()'s own guard). A stale lock from a
    process that's since died is self-healing: pid_alive() is False for it,
    so the next launch just claims the file."""
    holder = read_json_file(PID_FILE).get("pid")
    if holder and holder != os.getpid() and pid_alive(holder):
        return False
    else:
        PID_FILE.write_text(json.dumps({"pid": os.getpid()}))
        return True


def debounced(now: float) -> bool:
    """Cross-process double-post guard (requirement 7): a wake immediately
    followed by another wake — or a second copy of this script sharing the
    mic — posts at most once per WAKE_DEBOUNCE_SECONDS."""
    last = read_json_file(DEBOUNCE_FILE).get("last_wake_ts") or 0.0
    return (now - last) < WAKE_DEBOUNCE_SECONDS


def mark_posted(now: float) -> None:
    DEBOUNCE_FILE.write_text(json.dumps({"last_wake_ts": now}))
    return None


def post_directive(text: str) -> bool:
    """Final gate immediately before the network call: mute is absolute, so
    re-check it here even though wait_until_clear_to_listen() already gated
    the recording — mute could have flipped on during transcription."""
    now = time.time()
    if debounced(now) or muted_now():
        return False
    else:
        hud_post("/inbox", {"text": text})
        mark_posted(now)
        return True


# The payload for a wake with NOTHING after it (2026-08-19, Pat: "the Jarvis
# functionality works if it's more conversational — 'Hey Jarvis' wakes you up
# and then puts you in a hearing-you mode"). Deliberately a normal
# {"text": ...} POST /inbox directive, NOT a new field: /inbox's schema is
# byte-compatible-critical (a live session polls it), so the signal rides in
# the text as a reserved sentinel PREFIX instead of changing the shape. The
# contract for the session side is the prefix `wake:` — match on that, never
# on the whole string, so the trailing words can be reworded freely.
WAKE_ONLY_PREFIX = "wake:"
WAKE_ONLY_TEXT = "wake: hearing you"


def handle_wake(wake_pcm: np.ndarray) -> None:
    """A matched wake phrase always reaches Pat's session now — either as
    the directive he spoke in the same breath, or (new) as the WAKE_ONLY
    sentinel when he just said the name and stopped.

    THE BUG THIS FIXES (diagnosed from the live log, 08-19): the daemon
    posted only the REMAINDER after the wake phrase, so a bare "Hey Jarvis."
    stripped to an empty string and nothing was posted at all —
    indistinguishable, from Pat's side, from a dead wake word. The log
    proves the daemon was working perfectly the whole time:
        WAKE matched (rms=0.03038, 3.0s window 17:32:50-17:32:53): 'Hey Jarvis.'
    ...followed by silence, because "" is falsy.

    Every guard is unchanged and still runs BEFORE this: the phrase (never
    the bare name) must match, the window must clear RMS_GATE, it must not
    be our own TTS coming back, and post_directive() still re-checks mute
    and the debounce. The only thing that changed is what happens when the
    remainder is empty or pure filler: instead of dropping the wake on the
    floor, post the sentinel so the session can switch to hearing-you.

    2026-08-21: the acceptance decision moved from a hand-maintained phrase
    blocklist to wake_guard.judge_directive(), which keeps that blocklist as
    its floor and adds the decoder's own confidence signals, a fast accept
    for substantive text, and an LLM tier for the ambiguous residue. Note
    what a rejection costs here: NOT silence. A rejected directive still
    falls through to the WAKE_ONLY branch below and posts hearing-you, so
    the expensive failure (Pat gets no response at all) is not reachable
    from this gate — it costs him a repeat at worst.

    The `heard_wake_word(transcript)` re-check on the SECOND transcription
    is what keeps this honest. Reaching this function means the wake phrase
    was in the short detection window, but the sentinel is only posted when
    the full utterance transcript ALSO contains the phrase — so a filler
    hallucination that merely followed a wake can never manufacture one."""
    utterance_pcm = capture_utterance(wake_pcm)
    if rms_of(utterance_pcm) < RMS_GATE:
        return None
    else:
        # Heartbeat around the whisper call as well as through the gate:
        # server.py calls this listener dead after LISTENING_ALIVE_SECONDS
        # (15) without one, and transcription alone can block for up to 20.
        # A healthy daemon showing OFFLINE degrades the exact indicator Pat
        # uses to notice real deafness.
        touch_heartbeat()
        transcript, signals = transcribe_detailed(utterance_pcm)
        touch_heartbeat()
        verdict = safe_judge_directive(strip_wake_phrase(transcript), signals,
                                       keepalive=touch_heartbeat)
        directive = verdict.text
        log_line(describe(verdict))   # every decision, with the numbers behind
                                      # it, so a future false positive is
                                      # diagnosable from the log alone
        if verdict.accept:
            posted = post_directive(directive)   # unchanged path — Pat relies on it
            log_line(f"WAKE directive {'posted' if posted else 'SUPPRESSED (mute/debounce)'}: {directive!r}")
            return None
        elif heard_wake_word(transcript):
            posted = post_directive(WAKE_ONLY_TEXT)
            log_line(
                f"WAKE with no directive {'posted as ' + WAKE_ONLY_TEXT!r} — hearing-you"
                if posted else
                "WAKE with no directive SUPPRESSED (mute/debounce)"
            )
            return None
        else:
            log_line(f"WAKE dropped — no wake phrase in the full utterance: {transcript!r}")
            return None


_wake_window = {"tail": None, "ts": 0.0}


def wake_window(chunk: np.ndarray) -> np.ndarray:
    """The audio actually handed to whisper: this chunk with the tail of the
    previous one replayed in front of it, so a wake phrase spoken across a
    read boundary is still whole somewhere. See WAKE_OVERLAP_SECONDS.

    The carried tail is dropped if it is stale — older than one chunk plus a
    margin, which is what happens across a mute or a TTS pause. Replaying
    audio from before a pause would let speech that was deliberately not
    listened to reappear afterwards, which is both wrong and a way to
    self-trigger on the assistant's own trailing words."""
    now = time.time()
    tail = _wake_window["tail"]
    fresh = tail is not None and (now - _wake_window["ts"]) < (WAKE_CHUNK_SECONDS + 1.0)
    _wake_window["tail"] = chunk[-int(WAKE_OVERLAP_SECONDS * RATE):]
    _wake_window["ts"] = now
    return np.concatenate((tail, chunk)) if fresh else chunk


_playback_seen = {"start": 0.0, "end": 0.0, "active": False, "ts": 0.0}


def own_playback() -> dict:
    """Our own most recent TTS playback interval, from the HUD's event-log
    view. The last successful answer is remembered, so a HUD that goes away
    mid-utterance leaves us with "playback was active" rather than with
    nothing — which is the whole point, since the ordinary pause gate reads
    an unreachable HUD as "not speaking" and disappears."""
    payload = hud_get("/state.json").get("playback")
    if isinstance(payload, dict) and payload.get("start"):
        _playback_seen.update(
            start=payload["start"],
            end=payload.get("end") or 0.0,
            active=bool(payload.get("active")),
            ts=time.time(),
        )
        return _playback_seen
    else:
        return _playback_seen


def window_is_our_own_audio(window_start: float, window_end: float) -> bool:
    """True when this audio window is (mostly) our own speech coming back in
    through the microphone.

    The MIDPOINT of the window is tested, not its edges. A window that
    merely straddles the end of playback is overwhelmingly Pat answering the
    instant we stopped talking — measured at +4s, and his real wake word
    landed in exactly such a window — so testing overlap at the edges would
    reject the most natural thing he does. A window whose middle is inside
    our playback is ours.

    This is deliberately independent of the status-based pause: it is
    checked at the moment of ACCEPTING a wake, so even if the pause gate is
    entirely absent (HUD unreachable, status wrong, cooldown too short) our
    own voice still cannot put a directive into Pat's inbox."""
    playback = own_playback()
    if not playback["start"]:
        return False
    else:
        midpoint = (window_start + window_end) / 2.0
        stop = time.time() if playback["active"] else playback["end"] + SELF_AUDIO_TAIL_SECONDS
        return playback["start"] <= midpoint <= stop


def log_line(message: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}", file=sys.stderr, flush=True)
    return None


def listen_forever() -> None:
    """The catch-all around the loop body is the last line of defense
    (requirement 2, 2026-08-17): read_chunk() already recovers from
    device errors on its own, but ANY other unexpected exception here would
    previously kill the whole process with nothing but a possibly-lost
    traceback. Now it's logged with a full traceback and the loop keeps
    going — a noisy log beats a silent death every time."""
    while True:
        try:
            wait_until_clear_to_listen()
            maybe_rebind_if_device_moved()  # the OS moved the mic
            maybe_rebind_if_deaf()          # or the mic went silent in place
                                            # both only here: never mid-TTS,
                                            # never mid-utterance
            window = wake_window(read_chunk(WAKE_CHUNK_SECONDS))
            # The window's true time span, recorded rather than inferred.
            # Log timestamps are stamped AFTER transcription returns, which
            # made calibrating anything against them guesswork.
            window_end = time.time()
            window_start = window_end - len(window) / RATE
            level = rms_of(window)
            if level < RMS_GATE:
                continue
            else:
                # Everything from here on is LOGGED. Until 2026-08-18 the
                # wake path was completely silent unless it threw, so
                # "the log shows zero wake detections" was an artifact of
                # never logging any rather than evidence of never hearing
                # any — and it was read as the latter during this
                # investigation. A path whose success and near-miss are
                # both invisible cannot be diagnosed.
                heard = transcribe(window)
                span = f"{time.strftime('%H:%M:%S', time.localtime(window_start))}-{time.strftime('%H:%M:%S', time.localtime(window_end))}"
                if heard_wake_word(heard) and window_is_our_own_audio(window_start, window_end):
                    log_line(
                        f"WAKE REJECTED as our own playback (rms={level:.5f}, "
                        f"window {span}): {heard!r} — refusing to wake myself"
                    )
                elif heard_wake_word(heard):
                    log_line(f"WAKE matched (rms={level:.5f}, {len(window)/RATE:.1f}s window {span}): {heard!r}")
                    handle_wake(window)
                elif heard.strip():
                    log_line(f"above gate, no wake phrase (rms={level:.5f}): {heard!r}")
                else:
                    log_line(f"above gate, whisper returned nothing (rms={level:.5f})")
        except Exception:
            log_exception("listen_forever: unexpected error, continuing")
            time.sleep(1.0)


def main() -> int:
    if not acquire_singleton_lock():
        print(
            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] another instance is already running "
            "(PID_FILE held by a live process) — exiting quietly",
            file=sys.stderr, flush=True,
        )
        return 0
    else:
        listen_forever()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
