"""Demonstration fixture for the /work contract, nothing more.

Whatever actually fills this contract for real — a tracker adapter, a
directory of markdown work items — lives outside this repository. This
script exists only to POST a small hardcoded work state to a running HUD
server so the /work route and the HUD's due/boards widgets can be smoke-
tested in about thirty seconds:

    python3 -m voice_hud.server &
    python3 examples/post_work_state.py
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

HUD_URL = "http://127.0.0.1:8123/work"

PAYLOAD = {
    "due": [
        {
            "id": "T-101",
            "title": "Ship the /work route",
            "due": "2026-09-03",
            "url": "https://example.test/tickets/T-101",
        },
        {
            "id": "T-102",
            "title": "Write the example poster",
            "due": "2026-09-04",
        },
    ],
    "boards": [
        {
            "name": "voice-hud",
            "sections": [
                {
                    "name": "In progress",
                    "items": [
                        {"id": "T-101", "title": "Ship the /work route"},
                    ],
                },
                {
                    "name": "Up next",
                    "items": [
                        {"id": "T-102", "title": "Write the example poster"},
                        {"id": "T-103", "title": "Smoke-test the HUD widgets"},
                    ],
                },
            ],
        },
    ],
}


def main() -> int:
    body = json.dumps(PAYLOAD).encode()
    req = urllib.request.Request(
        HUD_URL,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            post_response = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        # HTTPError is a URLError subclass, so it must be caught first: the
        # server answered (with a 4xx/5xx), it just didn't like the request
        # — a different problem from "nothing is listening on 8123".
        print(f"POST {HUD_URL} failed: HTTP {e.code} {e.read().decode()}")
        return 1
    except (urllib.error.URLError, OSError) as e:
        print(f"could not reach {HUD_URL}: {e}")
        print("is `python3 -m voice_hud.server` running on 8123?")
        return 1

    print("POST /work ->", json.dumps(post_response))

    # The POST response is only {"ok": true} — the server accepts any
    # payload whose "due" and "boards" are lists without looking at item
    # shape, so that ack alone can't show the contract actually round-
    # tripped. Read it back with GET /work, which returns the stored
    # due/boards/posted state, for the real evidence.
    with urllib.request.urlopen(HUD_URL, timeout=2) as resp:
        work_state = json.loads(resp.read().decode())

    print("GET /work ->")
    print(json.dumps(work_state, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
