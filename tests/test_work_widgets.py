"""The /work widgets read exactly the field names the server emits — locked on both sides."""

from __future__ import annotations

import json
import re
from pathlib import Path

from voice_hud import server

PAGE = (Path(server.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")


def test_server_shape_is_what_the_page_reads(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "WORK_STATE_FILE", tmp_path / "work_state.json")
    assert server.read_work_state() == {"due": [], "boards": [], "posted": False}
    (tmp_path / "work_state.json").write_text(json.dumps({
        "due": [{"id": "T-1", "title": "Ship it", "due": "2026-09-03"}],
        "boards": [{"name": "DE", "sections": [{"name": "In Progress", "items": [{"id": "T-1", "title": "Ship it"}]}]}],
    }))
    state = server.read_work_state()
    assert state["posted"] is True and state["due"][0]["title"] == "Ship it"
    assert state["boards"][0]["sections"][0]["items"][0]["id"] == "T-1"
    for name in ("work.posted", "work.due", "work.boards", "d.due", "d.title", "b.name", "b.sections", "sec.name", "sec.items"):
        assert name in PAGE, f"the page must read `{name}` — the server's own field name"


def test_state_json_carries_work_under_the_key_the_page_polls():
    assert '"work": read_work_state(),' in Path(server.__file__).read_text(encoding="utf-8")
    assert re.search(r"const work = s\.work", PAGE)


def test_the_panel_hides_until_something_has_posted():
    assert 'id="work" hidden' in PAGE
    assert "if (!work || !work.posted)" in PAGE and "workEl.hidden = true" in PAGE
