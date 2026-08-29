#!/usr/bin/env python3
"""Wake-word listener for voice standby — "hey Jarvis" (or the transition
alias "hey Bella") brings Claude back.

Runs only while the HUD standby flag is true. Listens to the default mic in
short chunks, energy-gates them, transcribes loud chunks against the LOCAL
whisper server (port 2022), and looks for the wake word. Nothing is stored and
nothing leaves the machine; each chunk is discarded after the check.

Exits when: the wake word is heard (flips standby false first), standby is
cleared externally (HUD/session), or the HUD server goes away. The Claude
session watches the standby flag and resumes conversation when it flips false.

Run with the voice-mode venv python (has sounddevice + numpy):
  ~/.local/share/uv/tools/voice-mode/bin/python wake_listener.py
"""
import io
import json
import time
import urllib.error
import urllib.request
import uuid
import wave

import numpy as np
import sounddevice as sd

HUD = "http://127.0.0.1:8123"
WHISPER = "http://127.0.0.1:2022/v1/audio/transcriptions"
RATE = 16000
CHUNK_SECONDS = 1.6  # short chunks = fast wake; "bella" alone matches, so a
                     # phrase split across a boundary still lands in chunk 2
RMS_GATE = 0.006  # normalized RMS below this = ambient noise, skip whisper
# Phrases, not the bare name: a lone name in overheard audio (meetings, TV)
# must not wake the session. 2026-08-19 JARVIS rebrand: jarvis phrases ADDED
# alongside bella (transition alias, removal is Pat's later call); "jervis"
# covers whisper's common mis-transcription, same role as "bela".
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
        with urllib.request.urlopen(req, timeout=15) as resp:
            return (json.loads(resp.read()).get("text") or "").lower()
    except (urllib.error.URLError, ValueError, OSError):
        return ""


def record_chunk() -> np.ndarray:
    rec = sd.rec(int(CHUNK_SECONDS * RATE), samplerate=RATE, channels=1, dtype="int16")
    sd.wait()
    return rec.reshape(-1)


def rms_of(pcm: np.ndarray) -> float:
    return float(np.sqrt(np.mean((pcm.astype(np.float32) / 32768.0) ** 2)))


def main() -> int:
    while True:
        standby = hud_get("/standby")
        if not standby.get("standby"):
            return 0
        elif hud_get("/mute").get("muted"):
            time.sleep(1.0)
        else:
            chunk = record_chunk()
            if rms_of(chunk) < RMS_GATE:
                continue
            elif any(w in transcribe(chunk) for w in WAKE_WORDS):
                hud_post("/standby", {"standby": False})
                return 0
            else:
                continue


if __name__ == "__main__":
    raise SystemExit(main())
