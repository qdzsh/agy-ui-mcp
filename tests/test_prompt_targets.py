"""Tests for ``build_vision_prompt`` in per-target (``shot_pairs``) mode.

Imports :mod:`agy_ui_mcp.agy_runner` directly; it is stdlib-only and offline.
"""

from __future__ import annotations

from agy_ui_mcp import agy_runner


def _two_target_pairs() -> list[dict]:
    return [
        {
            "label": "desktop / 1440px",
            "current": ["/tmp/shots/desktop.png"],
            "design": ["/tmp/mocks/desktop.png"],
        },
        {
            "label": "mobile / iPhone 13 / dark",
            "current": ["/tmp/shots/mobile.png"],
            "design": ["/tmp/mocks/mobile-dark.png"],
        },
    ]


def test_shot_pairs_lists_each_target() -> None:
    prompt = agy_runner.build_vision_prompt(
        task="match the mockups",
        shot_pairs=_two_target_pairs(),
        allow_hint="src/**/*.css",
        deny_hint="**/api/**",
        request_score=True,
    )
    # Each target appears with its label and both image groups.
    assert 'Target "desktop / 1440px":' in prompt
    assert 'Target "mobile / iPhone 13 / dark":' in prompt
    assert "/tmp/shots/desktop.png" in prompt
    assert "/tmp/mocks/desktop.png" in prompt
    assert "/tmp/shots/mobile.png" in prompt
    assert "/tmp/mocks/mobile-dark.png" in prompt
    # Labels in the per-target listing.
    assert "current screenshot(s):" in prompt
    assert "design mockup(s) to match:" in prompt
    # Guidance to use responsive CSS for one codebase.
    assert "responsive" in prompt.lower()
    assert "media quer" in prompt.lower()


def test_shot_pairs_request_score_block_present() -> None:
    prompt = agy_runner.build_vision_prompt(
        task="match the mockups",
        shot_pairs=_two_target_pairs(),
        allow_hint="src/**/*.css",
        deny_hint="**/api/**",
        request_score=True,
    )
    assert "MATCH_SCORE: <integer 0-100>" in prompt
    assert "GAPS:" in prompt


def test_shot_pairs_without_score() -> None:
    prompt = agy_runner.build_vision_prompt(
        task="just align things",
        shot_pairs=_two_target_pairs(),
        allow_hint="src/**/*.css",
        deny_hint="**/api/**",
        request_score=False,
    )
    assert "MATCH_SCORE:" not in prompt
    assert "GAPS:" not in prompt


def test_shot_pairs_handles_missing_design() -> None:
    """A target with no design refs still renders without crashing."""
    pairs = [
        {
            "label": "lone",
            "current": ["/tmp/a.png"],
            "design": [],
        }
    ]
    prompt = agy_runner.build_vision_prompt(
        task="t",
        shot_pairs=pairs,
        allow_hint="x",
        deny_hint="y",
        request_score=False,
    )
    assert 'Target "lone":' in prompt
    assert "design mockup(s) to match: (none)" in prompt


def test_flat_mode_still_works() -> None:
    """When shot_pairs is None, legacy flat mode is unchanged."""
    prompt = agy_runner.build_vision_prompt(
        task="legacy",
        current_shots=["/tmp/cur.png"],
        design_refs=["/tmp/mock.png"],
        allow_hint="src/**/*.css",
        deny_hint="**/api/**",
    )
    assert "Current state screenshots" in prompt
    assert "Design reference images to match" in prompt
    assert 'Target "' not in prompt
