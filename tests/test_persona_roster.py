"""VOICE_HUD_VOICE_ROSTER — the env override for the roster of kokoro voice
ids /voice accepts (see server.py's DEFAULT_VOICE_ROSTER / parse_voice_roster
/ VOICE_ROSTER). It reaches /replay only as kokoro_voices()'s last-resort
fallback, not as the thing replay normally resolves against.

parse_voice_roster() is pure and most of its edge cases are exercised
directly. That alone does not prove the env var is actually read, though:
VOICE_ROSTER is computed once at server.py's module load from
os.environ.get("VOICE_HUD_VOICE_ROSTER", ...), so reassigning
os.environ in-process after voice_hud.server is already imported (as it will
be by the time this module is collected under a full `pytest -q` run) is
inert — the same hazard tests/test_work_state.py documents for
VOICE_HUD_STATE_DIR, but VOICE_ROSTER has none of that module's import-order
entanglement, so a fresh subprocess per case is enough to prove it directly:
run a bare `python -c` import with a controlled environment and read
VOICE_ROSTER back out. That is what
test_env_var_overrides_the_default_roster_at_import_time and
test_env_var_absent_falls_back_to_default_voice_roster_at_import_time do
below; a wrong env-var key name in server.py fails them, where the pure
parser tests would not notice.
"""

from __future__ import annotations

import os
import subprocess
import sys

from voice_hud.server import parse_voice_roster


def test_blank_input_yields_an_empty_tuple_so_the_caller_falls_back():
    assert parse_voice_roster("") == ()


def test_a_well_formed_value_overrides_with_its_own_ids_in_order():
    assert parse_voice_roster("af_bella,af_heart") == ("af_bella", "af_heart")


def test_stray_whitespace_around_entries_is_trimmed():
    assert parse_voice_roster(" af_bella , af_heart ") == ("af_bella", "af_heart")


def test_blank_entries_from_stray_commas_are_dropped_not_kept_as_empty_strings():
    assert parse_voice_roster("af_bella,,af_heart,") == ("af_bella", "af_heart")


def test_whitespace_only_entries_are_also_dropped():
    assert parse_voice_roster("af_bella,   ,af_heart") == ("af_bella", "af_heart")


def test_never_raises_on_input_with_no_usable_ids():
    assert parse_voice_roster(",,,   ,") == ()


def test_entries_without_a_kokoro_prefix_are_dropped_not_passed_through():
    # persona_voice() reads every roster entry as v.split("_", 1)[1] — the
    # suffix after the FIRST underscore. A bare display name like "bella"
    # has no underscore, so letting it through would not shrink the
    # roster, it would hand /replay's kokoro-down fallback an IndexError.
    assert parse_voice_roster("af_bella,bella,af_heart") == ("af_bella", "af_heart")


def test_entries_with_an_empty_suffix_are_dropped_not_passed_through():
    # "_" in entry is not enough: "af_" has an underscore but its suffix
    # (what persona_voice() actually reads) is "". Letting it through would
    # not raise — it would silently match a persona-less /replay (missing
    # persona coerces to "") and return the junk id instead of falling
    # back to DEFAULT_REPLAY_VOICE, so the replay 503s instead of playing.
    assert parse_voice_roster("af_bella,af_,af_heart") == ("af_bella", "af_heart")


def test_entries_with_an_empty_prefix_are_also_dropped():
    # Symmetric case: a bare "_" or "_bella" has a non-empty suffix but no
    # real kokoro language/gender prefix before it — not a valid id either.
    assert parse_voice_roster("af_bella,_,_bella,af_heart") == ("af_bella", "af_heart")


def test_env_var_overrides_the_default_roster_at_import_time():
    env = {**os.environ, "VOICE_HUD_VOICE_ROSTER": "af_bella,af_heart"}
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from voice_hud.server import VOICE_ROSTER; print(','.join(VOICE_ROSTER))",
        ],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    assert result.stdout.strip() == "af_bella,af_heart"


def test_env_var_absent_falls_back_to_default_voice_roster_at_import_time():
    env = {k: v for k, v in os.environ.items() if k != "VOICE_HUD_VOICE_ROSTER"}
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from voice_hud.server import VOICE_ROSTER, DEFAULT_VOICE_ROSTER\n"
            "print(VOICE_ROSTER == DEFAULT_VOICE_ROSTER)",
        ],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    assert result.stdout.strip() == "True"


def test_env_var_with_only_invalid_entries_also_falls_back_at_import_time():
    env = {**os.environ, "VOICE_HUD_VOICE_ROSTER": "bella,heart"}
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from voice_hud.server import VOICE_ROSTER, DEFAULT_VOICE_ROSTER\n"
            "print(VOICE_ROSTER == DEFAULT_VOICE_ROSTER)",
        ],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    assert result.stdout.strip() == "True"
