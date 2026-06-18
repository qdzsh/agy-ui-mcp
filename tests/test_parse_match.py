"""Tests for ``agy_runner.parse_match`` and the score-request prompt block.

These tests import :mod:`agy_ui_mcp.agy_runner` directly. That module is
stdlib-only (``re``, ``os``, ``subprocess`` etc.), so it imports offline with no
agy/playwright dependency and without spawning anything.
"""

from __future__ import annotations

from agy_ui_mcp import agy_runner


def test_well_formed_score_and_gaps() -> None:
    text = "MATCH_SCORE: 87\nGAPS: header padding, button color"
    score, gaps = agy_runner.parse_match(text)
    assert score == 87
    assert gaps == "header padding, button color"


def test_score_and_gaps_with_surrounding_noise() -> None:
    text = (
        "I opened the screenshots and the mockup and adjusted the spacing.\n"
        "Here is my self-assessment of the result so far.\n"
        "MATCH_SCORE: 72\n"
        "GAPS: nav alignment, font weight\n"
        "Let me know if you want another pass.\n"
    )
    score, gaps = agy_runner.parse_match(text)
    assert score == 72
    assert gaps == "nav alignment, font weight"


def test_gaps_none_literal_becomes_empty() -> None:
    text = "MATCH_SCORE: 100\nGAPS: NONE"
    score, gaps = agy_runner.parse_match(text)
    assert score == 100
    assert gaps == ""


def test_gaps_none_case_insensitive() -> None:
    text = "MATCH_SCORE: 95\nGAPS: none"
    score, gaps = agy_runner.parse_match(text)
    assert score == 95
    assert gaps == ""


def test_missing_gaps_line() -> None:
    text = "Did the work.\nMATCH_SCORE: 60\n"
    score, gaps = agy_runner.parse_match(text)
    assert score == 60
    assert gaps == ""


def test_missing_both_lines() -> None:
    text = "I made some CSS edits but forgot to report a score."
    score, gaps = agy_runner.parse_match(text)
    assert score is None
    assert gaps == ""


def test_empty_text() -> None:
    score, gaps = agy_runner.parse_match("")
    assert score is None
    assert gaps == ""


def test_score_above_100_is_clamped() -> None:
    text = "MATCH_SCORE: 150\nGAPS: NONE"
    score, _ = agy_runner.parse_match(text)
    assert score == 100


def test_score_below_0_is_clamped() -> None:
    text = "MATCH_SCORE: -20\nGAPS: everything"
    score, gaps = agy_runner.parse_match(text)
    assert score == 0
    assert gaps == "everything"


def test_score_exactly_at_bounds() -> None:
    assert agy_runner.parse_match("MATCH_SCORE: 0")[0] == 0
    assert agy_runner.parse_match("MATCH_SCORE: 100")[0] == 100


def test_multiple_score_lines_takes_last() -> None:
    text = (
        "MATCH_SCORE: 40\n"
        "GAPS: lots\n"
        "...revised after another look...\n"
        "MATCH_SCORE: 88\n"
        "GAPS: minor shadow\n"
    )
    score, gaps = agy_runner.parse_match(text)
    assert score == 88
    assert gaps == "minor shadow"


def test_extra_whitespace_and_indentation() -> None:
    text = "   MATCH_SCORE:    77   \n\t GAPS:   spacing tweaks  \n"
    score, gaps = agy_runner.parse_match(text)
    assert score == 77
    assert gaps == "spacing tweaks"


def test_empty_gaps_value_becomes_empty() -> None:
    text = "MATCH_SCORE: 50\nGAPS:   "
    score, gaps = agy_runner.parse_match(text)
    assert score == 50
    assert gaps == ""


def test_build_vision_prompt_requests_score_when_flagged() -> None:
    prompt = agy_runner.build_vision_prompt(
        task="make it nicer",
        current_shots=["/tmp/cur.png"],
        design_refs=["/tmp/mock.png"],
        allow_hint="src/**/*.css",
        deny_hint="**/api/**",
        request_score=True,
    )
    assert "MATCH_SCORE: <integer 0-100>" in prompt
    assert "GAPS:" in prompt
    assert "100" in prompt
    # Round-trips through parse_match's expected line format.
    assert "MATCH_SCORE:" in prompt


def test_build_vision_prompt_omits_score_by_default() -> None:
    prompt = agy_runner.build_vision_prompt(
        task="make it nicer",
        current_shots=["/tmp/cur.png"],
        design_refs=[],
        allow_hint="src/**/*.css",
        deny_hint="**/api/**",
    )
    assert "MATCH_SCORE:" not in prompt
    assert "GAPS:" not in prompt
