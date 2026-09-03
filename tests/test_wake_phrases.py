"""VOICE_HUD_WAKE_PHRASES: parsed, lower-cased, trimmed; bare words refused; default when unset or all refused.

The listener imports numpy and sounddevice at module level and CI installs
only the `dev` extra, so the two are stubbed into sys.modules HERE — but only
when the real ones are absent, so this module passes both where the listener
extra is installed and where it is not. A skip would not be a pass.
"""

from __future__ import annotations

import importlib
import sys
import types

import pytest

for _name in ("numpy", "sounddevice"):
    try:
        importlib.import_module(_name)
    except ImportError:
        _stub = types.ModuleType(_name)
        if _name == "numpy":
            _stub.ndarray = object  # the only attribute the listener's import path touches at load
            _stub.int16 = "int16"
        sys.modules[_name] = _stub

from voice_hud import always_on_listener as aol  # noqa: E402


def test_unset_means_the_default_tuple_unchanged():
    phrases, refused = aol.parse_wake_phrases(None)
    assert phrases == aol.DEFAULT_WAKE_WORDS and refused == []
    assert aol.parse_wake_phrases("   ")[0] == aol.DEFAULT_WAKE_WORDS


def test_comma_separated_lower_cased_trimmed_and_deduplicated():
    phrases, refused = aol.parse_wake_phrases("  Hey Computer ,okay  computer, hey computer ")
    assert phrases == ("hey computer", "okay computer") and refused == []


def test_a_bare_single_word_is_refused_with_a_reason():
    phrases, refused = aol.parse_wake_phrases("hey computer, jarvis")
    assert phrases == ("hey computer",)
    assert refused == [("jarvis", "bare single word — a wake needs a phrase, never a name")]


def test_all_refused_falls_back_to_the_default_and_says_so():
    phrases, refused = aol.parse_wake_phrases("jarvis,bella")
    assert phrases == aol.DEFAULT_WAKE_WORDS
    assert refused[-1][0] == "<all>" and "falling back" in refused[-1][1]


def test_the_comma_trap_is_real_and_documented():
    """"wake up, bella" is two entries; the generic first half survives. The docstring says so."""
    phrases, refused = aol.parse_wake_phrases("wake up, bella")
    assert phrases == ("wake up",) and refused[0][0] == "bella"
    assert "Comma trap" in aol.parse_wake_phrases.__doc__


def test_the_module_level_tuple_comes_from_the_environment(monkeypatch, capsys):
    monkeypatch.setenv("VOICE_HUD_WAKE_PHRASES", "hey computer, nope")
    mod = importlib.reload(aol)
    try:
        assert mod.WAKE_WORDS == ("hey computer",)
        err = capsys.readouterr().err
        assert "refused 'nope'" in err and "effective wake phrases: ('hey computer',)" in err
    finally:
        monkeypatch.delenv("VOICE_HUD_WAKE_PHRASES")
        importlib.reload(aol)
        assert aol.WAKE_WORDS == aol.DEFAULT_WAKE_WORDS
