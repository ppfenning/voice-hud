"""GET /cast and state.json's "cast": the standing seats, read from their own
definition files, never from a roster the HUD carries."""

from __future__ import annotations

import json
import urllib.request
from http.server import ThreadingHTTPServer
from threading import Thread

import pytest

from voice_hud import server

NOVA = """---
name: nova
description: |
  Read-only recon: codebase sweeps, log analysis, research. Returns
  evidence-backed conclusions and never edits.

  Use when: (1) "where does X live".
tools: Bash, Read, Grep, Glob, Skill, Agent
model: opus
---

You are the recon seat. In a voice session you are **Nova**, reporting in the
`af_nova` voice, opening "Nova here, back from ...".
"""

SARAH = """---
name: sarah
description: Build. Takes one scoped unit of work and opens one pull request.
tools: Bash, Read, Write, Edit, Grep, Glob
model: opus
---

In a voice session you are **Sarah**, in the `af_sarah` voice.
"""


@pytest.fixture()
def cast_dir(tmp_path, monkeypatch):
    (tmp_path / "nova.md").write_text(NOVA, encoding="utf-8")
    (tmp_path / "sarah.md").write_text(SARAH, encoding="utf-8")
    (tmp_path / "notes.md").write_text("# not a seat\n", encoding="utf-8")
    monkeypatch.setattr(server, "CAST_DIR", tmp_path)
    monkeypatch.setattr(server, "_cast_cache", {"at": 0.0, "value": None})
    return tmp_path


@pytest.fixture()
def client(cast_dir):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        thread.join()


def _get(base, route):
    with urllib.request.urlopen(f"{base}{route}") as resp:
        return resp.status, json.loads(resp.read().decode())


def test_parse_seat_reads_name_voice_model_and_lane() -> None:
    nova = server.parse_seat(NOVA)
    assert nova == {"name": "nova", "voice": "af_nova", "model": "opus", "lane": "reads",
                    "surface": "Read-only recon"}
    sarah = server.parse_seat(SARAH)
    assert sarah["lane"] == "writes" and sarah["voice"] == "af_sarah" and sarah["surface"] == "Build"


def test_parse_seat_refuses_what_is_not_a_seat() -> None:
    assert server.parse_seat("# just a note\n") is None
    assert server.parse_seat("---\ntools: Read\n---\nno name\n") is None


def test_get_cast_lists_the_seats_and_skips_other_files(client) -> None:
    status, body = _get(client, "/cast")
    assert status == 200 and body["unavailable"] is False
    assert [s["name"] for s in body["seats"]] == ["nova", "sarah"]


def test_state_json_carries_the_cast(client) -> None:
    _, body = _get(client, "/state.json")
    assert [s["name"] for s in body["cast"]] == ["nova", "sarah"]


def test_a_missing_cast_dir_is_unavailable_not_an_error(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(server, "CAST_DIR", tmp_path / "absent")
    monkeypatch.setattr(server, "_cast_cache", {"at": 0.0, "value": None})
    assert server.read_cast() == {"seats": [], "dir": str(tmp_path / "absent"), "unavailable": True}
