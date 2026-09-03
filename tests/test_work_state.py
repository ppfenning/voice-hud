"""GET/POST /work — the plain replace-list tracker contract (see the
Endpoints: docstring at the top of voice_hud/server.py). No liveness, no
aging: this is deliberately simpler than /tasks.

Isolation here comes from the `client` fixture's
`monkeypatch.setattr(server, "WORK_STATE_FILE", tmp_path / ...)`, NOT from an
env var: voice_hud.paths binds STATE_DIR (and therefore server.HUD_DIR) at
first import of voice_hud.paths, which — under the full `pytest -q` run —
already happened via tests/test_wake_guard.py before this module is even
collected. Setting VOICE_HUD_STATE_DIR here would be inert. Monkeypatching
the file constant per-test is what actually keeps these tests off the
owner's real ~/.local/state/voice-hud.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from threading import Thread

import pytest

from voice_hud import server


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "WORK_STATE_FILE", tmp_path / "work_state.json")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    port = httpd.server_address[1]
    thread = Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        thread.join()


def _get(base, route):
    with urllib.request.urlopen(f"{base}{route}") as resp:
        return resp.status, json.loads(resp.read().decode())


def _post(base, route, payload):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(f"{base}{route}", data=body, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def test_get_work_before_any_post_returns_empty_and_not_posted(client):
    status, body = _get(client, "/work")
    assert status == 200
    assert body == {"due": [], "boards": [], "posted": False}


def test_a_valid_post_round_trips_through_a_subsequent_get(client):
    payload = {
        "due": [{"id": "T-1", "title": "ship it", "when": "2026-09-03"}],
        "boards": [{"name": "sprint", "url": "https://example.test/sprint"}],
    }
    post_status, post_body = _post(client, "/work", payload)
    assert post_status == 200
    assert post_body == {"ok": True}

    get_status, get_body = _get(client, "/work")
    assert get_status == 200
    assert get_body == {"due": payload["due"], "boards": payload["boards"], "posted": True}


def test_an_intentionally_empty_post_is_still_posted_true(client):
    _post(client, "/work", {"due": [], "boards": []})
    status, body = _get(client, "/work")
    assert status == 200
    assert body == {"due": [], "boards": [], "posted": True}


@pytest.mark.parametrize(
    "payload",
    [
        {"boards": []},  # due missing entirely
        {"due": []},  # boards missing entirely
        {"due": "not-a-list", "boards": []},
        {"due": [], "boards": "not-a-list"},
    ],
)
def test_a_malformed_post_is_rejected_and_leaves_prior_state_untouched(client, payload):
    good = {"due": [{"id": "T-1"}], "boards": [{"name": "sprint"}]}
    _post(client, "/work", good)

    status, body = _post(client, "/work", payload)
    assert status == 400
    assert body["ok"] is False
    assert "error" in body

    get_status, get_body = _get(client, "/work")
    assert get_status == 200
    assert get_body == {"due": good["due"], "boards": good["boards"], "posted": True}
