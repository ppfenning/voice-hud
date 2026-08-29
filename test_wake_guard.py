#!/usr/bin/env python3
"""Tests for the tiered directive gate in wake_guard.py.

Stdlib + pytest only, and NOTHING here touches the mic, whisper, Kokoro or
`claude -p` — every transport is injected, so the whole suite runs offline
in milliseconds. The one genuinely-live test is skipped unless
VOICEHUD_LIVE_TESTS=1 is set explicitly.

The signal fixtures below are not invented: they are the numbers actually
measured on this machine on 2026-08-21 by probing the local whisper server
with generated negatives (digital silence, white noise at several RMS
levels, a short blip) and Kokoro-synthesized positives (clean, and degraded
with mixed-in noise down to SNR<1, and attenuated to just above RMS_GATE).
If whisper is ever swapped for a build with different calibration, THESE
tests are the thing that should fail first.
"""
import json
import os
import subprocess
import time

import pytest

import wake_guard as wg


# --------------------------------------------------------------------------
# Measured calibration vectors (2026-08-21, local faster-whisper on :2022)
# --------------------------------------------------------------------------
SIG_SILENCE_2S = {"language_probability": 0.386, "end_ratio": 14.99}
SIG_SILENCE_4S = {"language_probability": 0.386, "end_ratio": 7.50}
SIG_NOISE_0007 = {"language_probability": 0.390, "end_ratio": 1.00}
SIG_NOISE_0020 = {"language_probability": 0.397, "end_ratio": 1.00}
SIG_NOISE_0040 = {"language_probability": 0.411, "end_ratio": 1.00}
SIG_BLIP = {"language_probability": 0.468, "end_ratio": 0.52}

SIG_SPEECH_CLEAN = {"language_probability": 1.000, "end_ratio": 0.88}
SIG_SPEECH_NOISY = {"language_probability": 0.998, "end_ratio": 1.00}
SIG_SPEECH_SNR_UNDER_1 = {"language_probability": 0.989, "end_ratio": 1.00}
SIG_SPEECH_QUIET = {"language_probability": 1.000, "end_ratio": 0.80}
SIG_SPEECH_QUIET_NOISY = {"language_probability": 0.981, "end_ratio": 1.00}  # worst positive

NEGATIVE_SIGNALS = (
    SIG_SILENCE_2S, SIG_SILENCE_4S, SIG_NOISE_0007,
    SIG_NOISE_0020, SIG_NOISE_0040, SIG_BLIP,
)
POSITIVE_SIGNALS = (
    SIG_SPEECH_CLEAN, SIG_SPEECH_NOISY, SIG_SPEECH_SNR_UNDER_1,
    SIG_SPEECH_QUIET, SIG_SPEECH_QUIET_NOISY,
)

REAL_DIRECTIVES = (
    "check the ETL alerts",
    "hey Jarvis check the ETL alerts",
    "what's the status of the drop manifests",
    "run the tests",
    "thanks, that's helpful, now check the PR",
    "look at the drop reason audit trail epic and tell me what is blocking it",
)


# Terse real commands. Every one of these was REJECTED by the first cut of
# this gate: content_words() ate the object pronoun as filler, the utterance
# fell under the two-content-word bar, reached tier 3, and `claude -p`
# answered NO to "stop it". On the wake path that means Pat gets a
# hearing-you ack while his command is dropped — and "stop" is the single
# worst command to lose. A bare imperative verb IS a complete directive.
TERSE_COMMANDS = (
    "stop", "stop it", "do it", "cancel that", "hold on", "read it",
    "go on", "yes do that", "wait", "skip it", "run it", "check that",
)


def never_asks(_text, **_kwargs):
    raise AssertionError("the LLM tier must not be reached for this input")


def always_says(answer):
    return lambda _text, **_kwargs: answer


def explodes(_text, **_kwargs):
    raise subprocess.TimeoutExpired(cmd="claude", timeout=8.0)


# --------------------------------------------------------------------------
# 1. The legacy blocklist must survive untouched — other callers import it
# --------------------------------------------------------------------------
class TestLegacyBlocklistUnchanged:
    def test_still_exported(self):
        assert callable(wg.is_hallucination)
        assert callable(wg.strip_punct)
        assert isinstance(wg.HALLUCINATION_PHRASES, tuple)
        assert wg.MIN_DIRECTIVE_WORDS == 2
        assert wg.RMS_GATE == 0.006

    @pytest.mark.parametrize("phrase", wg.HALLUCINATION_PHRASES)
    def test_every_known_phrase_is_still_caught(self, phrase):
        assert wg.is_hallucination(phrase) is True

    @pytest.mark.parametrize("phrase", wg.HALLUCINATION_PHRASES)
    def test_every_known_phrase_survives_casing_and_punctuation(self, phrase):
        assert wg.is_hallucination(f"  {phrase.title()}!  ") is True

    def test_thank_you_the_original_incident(self):
        assert wg.is_hallucination("Thank you.") is True
        assert wg.is_hallucination(" Thank you.\n") is True

    def test_too_short_is_still_caught(self):
        assert wg.is_hallucination("hello") is True
        assert wg.is_hallucination("") is True

    @pytest.mark.parametrize("text", REAL_DIRECTIVES)
    def test_real_directives_still_pass_the_legacy_guard(self, text):
        assert wg.is_hallucination(text) is False


# --------------------------------------------------------------------------
# 2. The feature flag
# --------------------------------------------------------------------------
class TestGateMode:
    @pytest.mark.parametrize("raw,expected", (
        ("off", wg.GATE_OFF),
        ("OFF", wg.GATE_OFF),
        ("  off  ", wg.GATE_OFF),
        ("heuristic", wg.GATE_HEURISTIC),
        ("llm", wg.GATE_LLM),
    ))
    def test_recognised_values(self, raw, expected):
        assert wg.gate_mode({wg.GATE_MODE_ENV: raw}) == expected

    def test_unset_defaults_to_the_new_behaviour(self):
        assert wg.gate_mode({}) == wg.DEFAULT_GATE_MODE
        assert wg.DEFAULT_GATE_MODE == wg.GATE_LLM

    def test_garbage_falls_back_to_the_default_rather_than_failing(self):
        assert wg.gate_mode({wg.GATE_MODE_ENV: "banana"}) == wg.DEFAULT_GATE_MODE
        assert wg.gate_mode({wg.GATE_MODE_ENV: ""}) == wg.DEFAULT_GATE_MODE


class TestGateModeFile:
    """P0-b: the env var cannot be set on an ALREADY-RUNNING daemon, so the
    off switch has to live on disk — same idea as mute.json."""

    def test_file_selects_the_mode(self, tmp_path):
        path = tmp_path / "gate_mode.json"
        path.write_text(json.dumps({"mode": "off"}))
        assert wg.resolve_gate_mode({}, path) == wg.GATE_OFF

    def test_env_overrides_the_file(self, tmp_path):
        path = tmp_path / "gate_mode.json"
        path.write_text(json.dumps({"mode": "off"}))
        assert wg.resolve_gate_mode({wg.GATE_MODE_ENV: "llm"}, path) == wg.GATE_LLM

    def test_absent_file_is_the_default(self, tmp_path):
        assert wg.resolve_gate_mode({}, tmp_path / "nope.json") == wg.DEFAULT_GATE_MODE

    @pytest.mark.parametrize("body", ("", "not json", "[]", "null", '{"mode": "banana"}',
                                      '{"mode": null}', '{"other": "off"}', "{"))
    def test_corrupt_file_falls_back_to_the_default_and_never_raises(self, body, tmp_path):
        path = tmp_path / "gate_mode.json"
        path.write_text(body)
        assert wg.resolve_gate_mode({}, path) == wg.DEFAULT_GATE_MODE

    def test_unreadable_path_never_raises(self, tmp_path):
        # A directory where a file is expected: must not kill the listen loop.
        assert wg.resolve_gate_mode({}, tmp_path) == wg.DEFAULT_GATE_MODE

    def test_the_documented_one_liner_actually_works(self, tmp_path):
        # This is the exact shape the module docstring tells Pat to write.
        path = tmp_path / "gate_mode.json"
        path.write_text('{"mode":"off"}')
        assert wg.resolve_gate_mode({}, path) == wg.GATE_OFF


class TestGateOffRestoresTodaysBehaviour:
    @pytest.mark.parametrize("text", REAL_DIRECTIVES + wg.HALLUCINATION_PHRASES + ("thank you very much", "okay sure"))
    def test_off_mirrors_is_hallucination_exactly(self, text):
        verdict = wg.judge_directive(text, SIG_SILENCE_2S, mode=wg.GATE_OFF, ask=never_asks)
        assert verdict.accept is (not wg.is_hallucination(text))
        assert verdict.tier == "gate-off"

    def test_off_ignores_even_the_most_damning_acoustics(self):
        # Gate off must mean OFF: the acoustic tier cannot reject here.
        assert wg.judge_directive("check the ETL alerts", SIG_SILENCE_2S,
                                  mode=wg.GATE_OFF, ask=never_asks).accept is True


# --------------------------------------------------------------------------
# 3. Tier 0 — the blocklist floor. The gate is never LOOSER than today.
# --------------------------------------------------------------------------
class TestTier0BlocklistFloor:
    @pytest.mark.parametrize("mode", (wg.GATE_HEURISTIC, wg.GATE_LLM))
    @pytest.mark.parametrize("phrase", wg.HALLUCINATION_PHRASES)
    def test_known_phrases_rejected_before_any_other_tier(self, phrase, mode):
        verdict = wg.judge_directive(phrase, SIG_SPEECH_CLEAN, mode=mode, ask=never_asks)
        assert verdict.accept is False
        assert verdict.tier == "blocklist"

    def test_pristine_acoustics_cannot_rescue_a_blocklisted_phrase(self):
        verdict = wg.judge_directive("Thank you.", SIG_SPEECH_CLEAN, mode=wg.GATE_LLM, ask=always_says(True))
        assert verdict.accept is False


# --------------------------------------------------------------------------
# 4. Tier 1 — acoustic / decoder reject
# --------------------------------------------------------------------------
class TestTier1Acoustic:
    @pytest.mark.parametrize("signals", NEGATIVE_SIGNALS)
    def test_every_measured_negative_is_rejected(self, signals):
        # Text that the blocklist does NOT know about, so only the acoustic
        # tier can be doing the work here.
        verdict = wg.judge_directive("please subscribe to my channel", signals,
                                     mode=wg.GATE_LLM, ask=never_asks)
        assert verdict.accept is False
        assert verdict.tier == "acoustic"

    @pytest.mark.parametrize("signals", POSITIVE_SIGNALS)
    @pytest.mark.parametrize("text", REAL_DIRECTIVES)
    def test_no_measured_positive_is_ever_rejected_acoustically(self, signals, text):
        assert wg.acoustic_reason(signals) is None
        assert wg.judge_directive(text, signals, mode=wg.GATE_LLM, ask=never_asks).accept is True

    def test_degenerate_segment_timestamp_is_its_own_tell(self):
        # lang_prob deliberately fine; only the impossible timestamp is wrong.
        assert wg.acoustic_reason({"language_probability": 1.0, "end_ratio": 7.5}) is not None

    def test_language_probability_is_its_own_tell(self):
        assert wg.acoustic_reason({"language_probability": 0.39, "end_ratio": 0.9}) is not None

    def test_missing_signals_never_reject(self):
        # An older whisper, a plain-json response, or a transport failure must
        # fail OPEN to the text tiers — never into silence.
        assert wg.acoustic_reason({}) is None
        assert wg.acoustic_reason(None) is None
        assert wg.acoustic_reason({"language_probability": None, "end_ratio": None}) is None

    def test_a_substantive_directive_with_no_signals_still_gets_through(self):
        verdict = wg.judge_directive("check the ETL alerts", {}, mode=wg.GATE_LLM, ask=never_asks)
        assert verdict.accept is True
        assert verdict.tier == "fast-accept"

    @pytest.mark.parametrize("lang_prob,expect_reject", (
        (0.699, True), (0.70, False), (0.701, False), (0.75, False), (0.69, True),
    ))
    def test_the_floor_is_exercised_at_its_exact_boundary(self, lang_prob, expect_reject):
        # Nothing else in this file sits near 0.70 — every positive fixture is
        # 0.981+. Without this, moving the threshold breaks no test.
        signals = {"language_probability": lang_prob, "end_ratio": 0.9}
        assert (wg.acoustic_reason(signals) is not None) is expect_reject

    @pytest.mark.parametrize("ratio,expect_reject", (
        (1.49, False), (1.5, False), (1.51, True), (7.5, True),
    ))
    def test_the_timestamp_ceiling_is_exercised_at_its_boundary(self, ratio, expect_reject):
        signals = {"language_probability": 1.0, "end_ratio": ratio}
        assert (wg.acoustic_reason(signals) is not None) is expect_reject

    def test_the_chosen_threshold_sits_between_the_two_populations(self):
        worst_positive = min(s["language_probability"] for s in POSITIVE_SIGNALS)
        best_negative = max(s["language_probability"] for s in NEGATIVE_SIGNALS)
        assert best_negative < wg.LANG_PROB_FLOOR < worst_positive


# --------------------------------------------------------------------------
# 5. Tier 2 — fast accept, no LLM, so normal directives keep today's latency
# --------------------------------------------------------------------------
class TestTier2FastAccept:
    @pytest.mark.parametrize("text", REAL_DIRECTIVES)
    def test_real_directives_accept_without_consulting_the_llm(self, text):
        verdict = wg.judge_directive(text, SIG_SPEECH_CLEAN, mode=wg.GATE_LLM, ask=never_asks)
        assert verdict.accept is True
        assert verdict.tier == "fast-accept"

    def test_real_speech_that_merely_contains_filler_is_not_eaten(self):
        # The whole point of the content-word rule: "thanks" is filler, but
        # "check the PR" is an instruction, and the utterance is a directive.
        assert wg.is_substantive("thanks, that's helpful, now check the PR") is True

    @pytest.mark.parametrize("text", ("thank you", "thank you very much", "okay thanks",
                                      "yeah okay", "oh okay then", "you know"))
    def test_pure_pleasantries_are_not_fast_accepted(self, text):
        assert wg.is_substantive(text) is False

    @pytest.mark.parametrize("text", TERSE_COMMANDS)
    def test_terse_imperatives_are_directives_and_take_the_fast_path(self, text):
        # THE P0 REGRESSION. These must never reach tier 3, and must never
        # be rejected: a single imperative verb is a complete command.
        assert wg.is_substantive(text) is True, f"{text!r} is a real command"
        verdict = wg.judge_directive(text, SIG_SPEECH_CLEAN, mode=wg.GATE_LLM, ask=never_asks)
        assert verdict.accept is True
        assert verdict.tier == "fast-accept"

    def test_imperative_verbs_can_never_be_filler(self):
        # Fixed at the root by construction rather than by special-casing
        # phrases: FILLER_WORDS is derived by subtracting the imperatives.
        assert wg.FILLER_WORDS.isdisjoint(wg.IMPERATIVE_VERBS)
        assert "do" in wg.IMPERATIVE_VERBS and "do" not in wg.FILLER_WORDS

    @pytest.mark.parametrize("verb", ("stop", "do", "read", "go", "hold", "wait",
                                      "cancel", "check", "run", "skip", "pause"))
    def test_the_verbs_the_review_named_are_all_imperatives(self, verb):
        assert verb in wg.IMPERATIVE_VERBS
        assert wg.has_imperative(f"{verb} it") is True

    def test_pleasantries_still_contain_no_imperative(self):
        for text in ("thank you", "thank you very much", "okay thanks", "yeah okay"):
            assert wg.has_imperative(text) is False

    def test_content_words_ignore_filler_and_punctuation(self):
        assert wg.content_words("Okay, thanks — check the PR!") == ("check", "pr")

    @pytest.mark.parametrize("text", ("check alerts", "deploy staging", "rerun teco"))
    def test_terse_two_word_directives_take_the_fast_path_too(self, text):
        # These are real instructions and must not pay for an LLM round trip
        # just for being short.
        assert wg.is_substantive(text) is True
        assert wg.judge_directive(text, SIG_SPEECH_CLEAN,
                                  mode=wg.GATE_LLM, ask=never_asks).tier == "fast-accept"


# --------------------------------------------------------------------------
# 6. Tier 3 — LLM adjudication, only for the ambiguous band
# --------------------------------------------------------------------------
AMBIGUOUS = "thank you very much"          # not blocklisted, not substantive


class TestTier3Llm:
    def test_ambiguous_text_reaches_the_llm(self):
        asked = []

        def spy(text, **_kwargs):
            asked.append(text)
            return False

        verdict = wg.judge_directive(AMBIGUOUS, SIG_SPEECH_CLEAN, mode=wg.GATE_LLM, ask=spy)
        assert asked == [AMBIGUOUS]
        assert verdict.accept is False
        assert verdict.tier == "llm"

    def test_llm_yes_accepts(self):
        verdict = wg.judge_directive(AMBIGUOUS, SIG_SPEECH_CLEAN, mode=wg.GATE_LLM, ask=always_says(True))
        assert verdict.accept is True
        assert verdict.tier == "llm"

    def test_only_the_ambiguous_band_costs_an_llm_call(self):
        # Blocklisted -> tier 0; substantive -> tier 2; bad acoustics -> tier 1.
        for text, signals in (("Thank you.", SIG_SPEECH_CLEAN),
                              ("check the ETL alerts", SIG_SPEECH_CLEAN),
                              (AMBIGUOUS, SIG_SILENCE_2S)):
            wg.judge_directive(text, signals, mode=wg.GATE_LLM, ask=never_asks)


class TestTier3FallbackIsAlwaysTheStatusQuo:
    @pytest.mark.parametrize("broken", (
        explodes,
        lambda _t, **_k: (_ for _ in ()).throw(OSError("claude not on PATH")),
        always_says(None),                      # unparsable answer
    ))
    def test_llm_failure_falls_back_to_the_blocklist_verdict(self, broken):
        verdict = wg.judge_directive(AMBIGUOUS, SIG_SPEECH_CLEAN, mode=wg.GATE_LLM, ask=broken)
        assert verdict.accept is (not wg.is_hallucination(AMBIGUOUS))
        assert verdict.tier == "llm-unavailable"

    def test_fallback_never_becomes_accept_everything(self):
        # A blocklisted phrase must still be rejected even when the LLM is down.
        verdict = wg.judge_directive("Thank you.", SIG_SPEECH_CLEAN, mode=wg.GATE_LLM, ask=explodes)
        assert verdict.accept is False


class TestHeuristicModeSkipsTheLlmEntirely:
    @pytest.mark.parametrize("text", REAL_DIRECTIVES + (AMBIGUOUS,))
    def test_never_calls_out(self, text):
        verdict = wg.judge_directive(text, SIG_SPEECH_CLEAN, mode=wg.GATE_HEURISTIC, ask=never_asks)
        assert verdict.accept is True

    def test_tiers_0_and_1_still_apply(self):
        assert wg.judge_directive("Thank you.", SIG_SPEECH_CLEAN,
                                  mode=wg.GATE_HEURISTIC, ask=never_asks).accept is False
        assert wg.judge_directive("please subscribe to my channel", SIG_SILENCE_2S,
                                  mode=wg.GATE_HEURISTIC, ask=never_asks).accept is False


class TestHeartbeatKeepalive:
    """P1-a: the LLM call runs INLINE on the daemon's single listen loop. At
    8s with no heartbeat, plus whisper's 20s, the HUD's own liveness check
    (LISTENING_ALIVE_SECONDS = 15) flips the listener to OFFLINE while it is
    perfectly healthy — degrading the one indicator Pat uses to spot
    deafness. The gate must keep the heartbeat beating while it waits."""

    def test_keepalive_is_called_before_during_and_after(self):
        ticks = []

        class SlowProcess:
            returncode = 0

            def __init__(self):
                self.polls = 0

            def poll(self):
                self.polls += 1
                return 0 if self.polls > 3 else None

            def communicate(self, timeout=None):
                return ("YES", "")

            def kill(self):
                return None

        answer = wg.ask_claude("do it", timeout=8.0,
                               keepalive=lambda: ticks.append(1),
                               spawn=lambda _prompt: SlowProcess(),
                               tick=0.0)
        assert answer is True
        assert len(ticks) >= 3, "heartbeat must be touched while waiting, not just at the ends"

    def test_a_missing_keepalive_is_not_an_error(self):
        class Instant:
            returncode = 0

            def poll(self):
                return 0

            def communicate(self, timeout=None):
                return ("NO", "")

            def kill(self):
                return None

        assert wg.ask_claude("thanks", spawn=lambda _p: Instant(), tick=0.0) is False

    def test_judge_directive_threads_the_keepalive_through_to_the_llm(self):
        ticks = []
        wg.judge_directive(AMBIGUOUS, SIG_SPEECH_CLEAN, mode=wg.GATE_LLM,
                           ask=lambda _t, keepalive=None: (keepalive and keepalive()) or True,
                           keepalive=lambda: ticks.append(1))
        assert ticks, "the LLM tier must receive the keepalive callback"

    def test_the_timeout_leaves_headroom_over_the_measured_latency(self):
        # `claude -p` with MCP disabled measured 3.13s on 2026-08-21.
        assert wg.LLM_TIMEOUT_SECONDS <= 8.0
        assert wg.LLM_TIMEOUT_SECONDS >= 5.0


class TestSubprocessHygiene:
    """P2: subprocess timeouts SIGKILL only `claude` itself; its stdio MCP
    servers survive. One of those is voicemode, so a leaked audio-touching
    child is a plausible route to causing the exact bug this gate prevents.
    Cheapest fix: do not start them at all, and kill the whole group if the
    call does time out anyway."""

    def test_mcp_servers_are_disabled_on_the_command_line(self):
        assert "--strict-mcp-config" in wg.LLM_COMMAND
        assert '{"mcpServers":{}}' in wg.LLM_COMMAND

    def test_the_prompt_goes_on_stdin_not_argv(self):
        # --mcp-config is variadic and swallows a trailing positional prompt.
        assert not any("Transcript" in part for part in wg.LLM_COMMAND)

    def test_timeout_kills_the_whole_process_group(self):
        killed = []

        class Hanging:
            returncode = None
            pid = 4242

            def poll(self):
                return None

            def communicate(self, timeout=None):
                return ("", "")

            def kill(self):
                killed.append("kill")

        answer = wg.ask_claude("thanks", timeout=0.01,
                               spawn=lambda _p: Hanging(), tick=0.0,
                               reap=lambda proc: killed.append(("group", proc.pid)))
        assert answer is None                      # unavailable -> caller falls back
        assert ("group", 4242) in killed           # the GROUP, not just the child


class TestDictationOutcome:
    """P1-b: on the push-to-talk path a rejection enqueues nothing and
    returns ok=false — indistinguishable from "nothing was heard". That is
    real silence, worse than the wake path which at least acks."""

    def test_a_gate_rejection_still_reports_what_was_heard(self):
        verdict = wg.judge_directive("Thank you.", SIG_SPEECH_CLEAN, mode=wg.GATE_LLM, ask=never_asks)
        outcome = wg.dictation_outcome(verdict)
        assert outcome["ok"] is False
        assert outcome["text"] == "Thank you."      # distinguishable from a failure

    def test_nothing_heard_is_still_an_empty_string(self):
        verdict = wg.judge_directive("", {}, mode=wg.GATE_LLM, ask=never_asks)
        outcome = wg.dictation_outcome(verdict)
        assert outcome["ok"] is False and outcome["text"] == ""

    def test_an_accepted_dictation_is_unchanged(self):
        verdict = wg.judge_directive("check the ETL alerts", SIG_SPEECH_CLEAN,
                                     mode=wg.GATE_LLM, ask=never_asks)
        outcome = wg.dictation_outcome(verdict)
        assert outcome["ok"] is True and outcome["text"] == "check the ETL alerts"

    def test_the_response_shape_never_changes(self):
        for text in ("Thank you.", "", "check the ETL alerts"):
            outcome = wg.dictation_outcome(
                wg.judge_directive(text, {}, mode=wg.GATE_HEURISTIC, ask=never_asks))
            assert set(outcome) == {"ok", "text", "tier", "reason"}
            assert isinstance(outcome["ok"], bool) and isinstance(outcome["text"], str)
            assert isinstance(outcome["tier"], str) and isinstance(outcome["reason"], str)


class TestSilenceIsNotAGateRejection:
    """08-24: the HUD rendered EVERY non-accepted dictation as "no speech
    detected", so a gate rejection looked like a dead microphone and sent
    Pat to check his hardware. The API had to carry enough to tell the two
    apart before index.html could label them apart — these are the three
    outcomes the page now renders differently."""

    def test_the_rms_gate_has_its_own_tier(self):
        # No transcript exists on this path — the audio never reached whisper.
        assert wg.silence_outcome() == {
            "ok": False, "text": "", "tier": wg.SILENCE_TIER,
            "reason": "below the RMS gate — the microphone heard nothing",
        }

    def test_silence_outcome_cannot_be_mutated_through_its_caller(self):
        first = wg.silence_outcome()
        first["text"] = "clobbered"
        assert wg.silence_outcome()["text"] == ""

    def test_a_gate_rejection_never_claims_the_silence_tier(self):
        for text, signals in (("Thank you.", SIG_SPEECH_CLEAN),   # blocklist
                              ("mm hmm", SIG_NOISE_0040)):        # acoustic floor
            outcome = wg.dictation_outcome(
                wg.judge_directive(text, signals, mode=wg.GATE_LLM, ask=never_asks))
            assert outcome["ok"] is False
            assert outcome["tier"] != wg.SILENCE_TIER
            assert outcome["reason"]           # the page prints this verbatim

    def test_the_acoustic_reject_reports_the_number_that_rejected_it(self):
        outcome = wg.dictation_outcome(
            wg.judge_directive("what is the status", SIG_NOISE_0040,
                               mode=wg.GATE_LLM, ask=never_asks))
        assert outcome["tier"] == "acoustic"
        assert "language_probability" in outcome["reason"] and "0.411" in outcome["reason"]

    def test_an_accepted_dictation_is_tellable_from_both(self):
        outcome = wg.dictation_outcome(
            wg.judge_directive("check the ETL alerts", SIG_SPEECH_CLEAN,
                               mode=wg.GATE_LLM, ask=never_asks))
        assert outcome["ok"] is True and outcome["tier"] != wg.SILENCE_TIER


class TestSafeJudgeNeverRaises:
    """Both call sites are in a place where an exception is expensive: the
    daemon's listen loop, and an HTTP handler that would otherwise leave the
    request hanging with the queue unwritten."""

    @pytest.mark.parametrize("signals", (object(), "nonsense", 3, [1, 2]))
    def test_absurd_signals_still_produce_a_verdict(self, signals):
        verdict = wg.safe_judge_directive("check the ETL alerts", signals,
                                          mode=wg.GATE_LLM, ask=never_asks)
        assert verdict.accept is True

    def test_an_exploding_gate_falls_back_to_the_blocklist(self, monkeypatch):
        monkeypatch.setattr(wg, "judge_directive",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        assert wg.safe_judge_directive("check the ETL alerts", {}).accept is True
        assert wg.safe_judge_directive("Thank you.", {}).accept is False
        assert wg.safe_judge_directive("check the ETL alerts", {}).tier == "gate-error"


# --------------------------------------------------------------------------
# 7. Signal extraction from a real verbose_json body
# --------------------------------------------------------------------------
VERBOSE_SILENCE = {
    "text": " Thank you.",
    "duration": 2.0,
    "language": "en",
    "detected_language_probability": 0.386,
    "segments": [{"start": 0.0, "end": 29.98, "no_speech_prob": 7.77e-13,
                  "avg_logprob": -0.266,
                  "words": [{"word": " Thank", "probability": 0.275},
                            {"word": " you.", "probability": 0.998}]}],
}


class TestDecoderSignals:
    def test_extracts_the_signals_that_matter(self):
        s = wg.decoder_signals(VERBOSE_SILENCE)
        assert s["language_probability"] == pytest.approx(0.386)
        assert s["end_ratio"] == pytest.approx(29.98 / 2.0)

    def test_the_silence_body_is_rejected_end_to_end(self):
        s = wg.decoder_signals(VERBOSE_SILENCE)
        assert wg.acoustic_reason(s) is not None

    @pytest.mark.parametrize("body", ({}, {"text": "hi"}, {"segments": []},
                                      {"duration": 0, "segments": [{"end": 1.0}]},
                                      {"segments": [{"end": None}], "duration": 2.0},
                                      None))
    def test_malformed_bodies_yield_no_signals_rather_than_raising(self, body):
        s = wg.decoder_signals(body)
        assert isinstance(s, dict)
        assert wg.acoustic_reason(s) is None

    def test_text_is_recovered_verbatim(self):
        assert wg.transcript_text(VERBOSE_SILENCE) == "Thank you."
        assert wg.transcript_text(None) == ""
        assert wg.transcript_text({}) == ""


# --------------------------------------------------------------------------
# 8. The log line — a future false positive has to be diagnosable
# --------------------------------------------------------------------------
class TestInstrumentation:
    def test_describes_the_decision_and_the_signals_that_drove_it(self):
        verdict = wg.judge_directive("please subscribe to my channel", SIG_SILENCE_2S,
                                     mode=wg.GATE_LLM, ask=never_asks)
        line = wg.describe(verdict)
        assert "REJECT" in line
        assert "acoustic" in line
        assert "0.386" in line          # the number that drove it
        assert "subscribe" in line      # the text, so it can be reproduced

    def test_accept_is_labelled_too(self):
        line = wg.describe(wg.judge_directive("check the ETL alerts", SIG_SPEECH_CLEAN,
                                              mode=wg.GATE_LLM, ask=never_asks))
        assert "ACCEPT" in line and "fast-accept" in line

    def test_no_audio_is_ever_logged(self):
        verdict = wg.judge_directive("check the ETL alerts", SIG_SPEECH_CLEAN,
                                     mode=wg.GATE_LLM, ask=never_asks)
        assert all(isinstance(v, (int, float, str, type(None))) for v in verdict.signals.values())


# --------------------------------------------------------------------------
# 9. The prompt handed to `claude -p`
# --------------------------------------------------------------------------
class TestLlmPromptAndParsing:
    def test_prompt_embeds_the_transcript_and_demands_one_token(self):
        prompt = wg.llm_prompt("thank you very much")
        assert "thank you very much" in prompt
        assert "YES" in prompt and "NO" in prompt

    @pytest.mark.parametrize("raw,expected", (
        ("YES", True), ("NO", False), (" yes\n", True), ("no.", False),
        ("Yes", True), ("", None), ("maybe", None), ("I think so", None),
    ))
    def test_answer_parsing(self, raw, expected):
        assert wg.parse_llm_answer(raw) is expected


# --------------------------------------------------------------------------
# 10. Live integration — opt in explicitly, needs whisper + Kokoro + claude
# --------------------------------------------------------------------------
@pytest.mark.skipif(not os.environ.get("VOICEHUD_LIVE_TESTS"),
                    reason="set VOICEHUD_LIVE_TESTS=1 to exercise the real local services")
class TestLive:
    """Tier 3 had ZERO real-`claude` coverage: every other test injects a
    stub, and the phrases used were substantive enough that they never
    reached tier 3 in production. That gap is exactly why "stop it" -> NO
    shipped. These call the real binary."""

    def test_claude_p_answers_no_for_a_bare_pleasantry(self):
        assert wg.ask_claude("thank you very much") is False

    def test_claude_p_answers_yes_for_a_real_directive(self):
        assert wg.ask_claude("could you take a look at the failing build") is True

    @pytest.mark.parametrize("text", ("stop it", "do it", "cancel that", "hold on", "wait"))
    def test_the_real_llm_classifies_terse_imperatives_as_directives(self, text):
        # Belt and braces: these are fast-accepted before tier 3 now, but if
        # a future edit ever routes one here, the LLM must not drop it.
        assert wg.ask_claude(text) is True

    def test_no_stray_processes_survive_a_forced_timeout(self):
        import subprocess as sp
        before = sp.run(("pgrep", "-f", "claude"), capture_output=True, text=True).stdout.split()
        assert wg.ask_claude("thank you very much", timeout=0.5) is None
        time.sleep(2.0)
        after = sp.run(("pgrep", "-f", "claude"), capture_output=True, text=True).stdout.split()
        assert len(after) <= len(before), f"leaked processes: {set(after) - set(before)}"
