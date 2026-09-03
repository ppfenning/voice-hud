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

class _AnyAttributeModule(types.ModuleType):
    """A stand-in that answers every attribute — the listener's import path
    touches numpy and sounddevice names in annotations and defaults
    (`sd.InputStream`, `np.ndarray`, `np.int16`, ...), and a stub that guesses
    the list is a CI failure the day a new one appears. Nothing here is ever
    CALLED by these tests; only module-load has to succeed."""

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        # A distinct placeholder class per name, never `object`: pytest.approx
        # probes sys.modules["numpy"] and asks isinstance(x, np.ndarray), and
        # with ndarray == object every float became "an array".
        placeholder = type(name, (), {})
        setattr(self, name, placeholder)
        return placeholder


_STUBBED = []
for _name in ("numpy", "sounddevice"):
    try:
        importlib.import_module(_name)
    except ImportError:
        sys.modules[_name] = _AnyAttributeModule(_name)
        _STUBBED.append(_name)

from voice_hud import always_on_listener as aol  # noqa: E402

# The listener holds its own references now; take the stand-ins back out of
# sys.modules so nothing else (pytest.approx probes numpy) meets them.
for _name in _STUBBED:
    sys.modules.pop(_name, None)


def _reload_listener():
    """Reload with the stand-ins present, then take them out again."""
    for _n in _STUBBED:
        sys.modules[_n] = _AnyAttributeModule(_n)
    try:
        return importlib.reload(aol)
    finally:
        for _n in _STUBBED:
            sys.modules.pop(_n, None)


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
    mod = _reload_listener()
    try:
        assert mod.WAKE_WORDS == ("hey computer",)
        err = capsys.readouterr().err
        assert "refused 'nope'" in err and "effective wake phrases: ('hey computer',)" in err
    finally:
        monkeypatch.delenv("VOICE_HUD_WAKE_PHRASES")
        _reload_listener()
        assert aol.WAKE_WORDS == aol.DEFAULT_WAKE_WORDS
