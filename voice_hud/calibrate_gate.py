#!/usr/bin/env python3
"""Calibration harness for wake_guard's tier-1 acoustic thresholds.

This is the thing that produced the numbers in wake_guard's header comment,
and it is checked in so those numbers can be RE-DERIVED rather than trusted.
If the local whisper server is ever upgraded, retrained or swapped, run this
first: if the two populations stop separating, the thresholds are wrong and
test_wake_guard.py's measured fixtures should be updated to match.

    ~/.local/share/uv/tools/voice-mode/bin/python voice-hud/calibrate_gate.py

Everything stays on this machine: negatives are generated arithmetically,
positives are synthesized by the local Kokoro server, and both go to the
local whisper server. No audio leaves the box and no microphone is opened,
so this is safe to run while the daemon is live.

Stdlib only, so any python3 will do.
"""
import os
import io
import json
import math
import random
import struct
import sys
import time
import urllib.request
import uuid
import wave

WHISPER = os.environ.get("VOICE_HUD_WHISPER_URL", "http://127.0.0.1:2022/v1/audio/transcriptions")
KOKORO = os.environ.get("VOICE_HUD_KOKORO_URL", "http://127.0.0.1:8880/v1") + "/audio/speech"
SR = 16000


# ---------------------------------------------------------------------------
# Generated negatives — no microphone, no recording, just arithmetic
# ---------------------------------------------------------------------------
def pcm_wav(samples: tuple, sample_rate: int = SR) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(b"".join(
            struct.pack("<h", max(-32768, min(32767, int(s * 32767)))) for s in samples
        ))
    return buf.getvalue()


def silence(seconds: float) -> bytes:
    """Pure digital silence — what a dead or unrouted mic actually returns.
    This is the case that produced " Thank you." on 2026-08-17."""
    return pcm_wav(tuple(0.0 for _ in range(int(SR * seconds))))


def white_noise(seconds: float, rms: float, seed: int = 7) -> bytes:
    """Room tone: above RMS_GATE, so the energy gate passes it through and
    only the transcript-level guard can catch it."""
    rng = random.Random(seed)
    raw = tuple(rng.gauss(0.0, 1.0) for _ in range(int(SR * seconds)))
    current = math.sqrt(sum(x * x for x in raw) / len(raw))
    return pcm_wav(tuple(x * (rms / current) for x in raw))


def blip(seconds: float, rms: float, seed: int = 3) -> bytes:
    """A 50ms transient in an otherwise silent window — a door, a keyboard."""
    total = int(SR * seconds)
    rng = random.Random(seed)
    burst = tuple(rng.gauss(0.0, 1.0) for _ in range(int(SR * 0.05)))
    samples = tuple(
        burst[i - total // 2] if 0 <= i - total // 2 < len(burst) else 0.0
        for i in range(total)
    )
    current = math.sqrt(sum(x * x for x in samples) / len(samples))
    return pcm_wav(tuple(x * (rms / current) for x in samples))


# ---------------------------------------------------------------------------
# Synthesized positives — real speech, generated locally
# ---------------------------------------------------------------------------
def kokoro(text: str, voice: str = "af_sky", attempts: int = 6) -> bytes:
    """Local TTS. Retried because Kokoro refuses connections while it is busy
    synthesizing a previous request rather than queueing them."""
    request = urllib.request.Request(
        KOKORO,
        data=json.dumps({"model": "kokoro", "input": text,
                         "voice": voice, "response_format": "wav"}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return response.read()
        except (urllib.error.URLError, OSError):
            time.sleep(4)
    return b""


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------
def wav_rms(wav_bytes: bytes) -> float:
    with wave.open(io.BytesIO(wav_bytes), "rb") as handle:
        frames = handle.readframes(handle.getnframes())
    values = struct.unpack(f"<{len(frames)//2}h", frames)
    return math.sqrt(sum((v / 32768.0) ** 2 for v in values) / max(1, len(values)))


def verbose_transcribe(wav_bytes: bytes) -> dict:
    """The verbose_json call wake_guard's tier 1 depends on."""
    boundary = uuid.uuid4().hex
    body = b"".join((
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="response_format"\r\n\r\nverbose_json\r\n'
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="temperature"\r\n\r\n0\r\n'
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="chunk.wav"\r\n'
        "Content-Type: audio/wav\r\n\r\n".encode(),
        wav_bytes,
        f"\r\n--{boundary}--\r\n".encode(),
    ))
    request = urllib.request.Request(
        WHISPER, data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        return json.loads(response.read())


def all_signals(body: dict) -> dict:
    """Every candidate signal, INCLUDING the ones that turned out not to
    separate — the point of this harness is to show that they do not, so
    nobody re-adds no_speech_prob on intuition."""
    segments = body.get("segments") or []
    first = segments[0] if segments else {}
    duration = float(body.get("duration") or 0.0) or 0.001
    words = first.get("words") or []
    ends = tuple(float(s.get("end") or 0.0) for s in segments) or (0.0,)
    return {
        "text": (body.get("text") or "").strip(),
        "duration": round(duration, 2),
        "segments": len(segments),
        "end_ratio": round(max(ends) / duration, 2),
        "language_probability": round(float(body.get("detected_language_probability") or 0.0), 3),
        "no_speech_prob": float(first.get("no_speech_prob") or 0.0),
        "avg_logprob": round(float(first.get("avg_logprob") or 0.0), 3),
        "first_word_p": round(float(words[0]["probability"]), 3) if words and words[0].get("probability") is not None else None,
        "compression_ratio": float(first.get("compression_ratio") or 0.0),
    }


NEGATIVES = (
    ("silence 2s", lambda: silence(2.0)),
    ("silence 4s", lambda: silence(4.0)),
    ("noise rms .007", lambda: white_noise(3.0, 0.007)),
    ("noise rms .010", lambda: white_noise(3.0, 0.010)),
    ("noise rms .020", lambda: white_noise(3.0, 0.020)),
    ("noise rms .040", lambda: white_noise(3.0, 0.040)),
    ("blip 50ms", lambda: blip(2.0, 0.02)),
)

POSITIVES = (
    ("short directive", "check the ETL alerts"),
    ("wake + directive", "hey Jarvis check the ETL alerts"),
    ("question", "what's the status of the drop manifests"),
    ("long directive", "hey Jarvis can you look at the drop reason audit trail epic "
                       "and tell me which tickets are still open and what is blocking them"),
    ("real speech w/ filler", "thanks, that's helpful, now check the PR"),
    ("terse", "run the tests"),
)

COLUMNS = ("case", "rms", "language_probability", "end_ratio", "no_speech_prob",
           "avg_logprob", "first_word_p", "text")
WIDTHS = (24, 9, 12, 11, 12, 12, 13)


def cell(value) -> str:
    """Tiny probabilities are unreadable at full precision and the whole
    point of showing no_speech_prob is that it is uniformly tiny."""
    if isinstance(value, float) and value != 0.0 and abs(value) < 0.001:
        return f"{value:.2e}"
    else:
        return str(value)


def row(case: str, wav_bytes: bytes) -> dict:
    return {"case": case, "rms": round(wav_rms(wav_bytes), 5), **all_signals(wav_bytes and verbose_transcribe(wav_bytes))}


def render(rows: tuple) -> str:
    header = "".join(name.ljust(width) for name, width in zip(COLUMNS, WIDTHS)) + "text"
    lines = tuple(
        "".join(cell(r.get(name)).ljust(width) for name, width in zip(COLUMNS, WIDTHS))
        + repr(r["text"])[:58]
        for r in rows
    )
    return "\n".join((header, *lines))


def separation(rows: tuple, key: str) -> str:
    negatives = tuple(r[key] for r in rows if r["case"].startswith("NEG") and r[key] is not None)
    positives = tuple(r[key] for r in rows if r["case"].startswith("POS") and r[key] is not None)
    if not negatives or not positives:
        return f"{key}: no data"
    elif max(negatives) < min(positives) or max(positives) < min(negatives):
        return (f"{key}: SEPARATES  neg [{min(negatives):.3g}, {max(negatives):.3g}]  "
                f"pos [{min(positives):.3g}, {max(positives):.3g}]")
    else:
        return (f"{key}: overlaps    neg [{min(negatives):.3g}, {max(negatives):.3g}]  "
                f"pos [{min(positives):.3g}, {max(positives):.3g}]")


def main() -> int:
    rows = tuple(
        (row(f"NEG {name}", make()) for name, make in NEGATIVES)
    ) + tuple(
        row(f"POS {name}", kokoro(text)) for name, text in POSITIVES
    )
    print(render(rows))
    print()
    for key in ("language_probability", "end_ratio", "no_speech_prob", "avg_logprob", "first_word_p"):
        print(" ", separation(rows, key))
    print("\nOnly the SEPARATES rows may be used as a tier-1 signal. As of "
          "2026-08-21 that is language_probability; end_ratio separates only "
          "the degenerate-timestamp case and is used with a wide margin.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
