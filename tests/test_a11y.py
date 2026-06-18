"""Tests for the axe-core accessibility helpers in ``screenshot``.

The pure helpers (axe bundle loading, violation summarization) and the
skip-when-unavailable path of :func:`audit_a11y` are covered fully offline. The
live browser audit is exercised separately against a running demo.
"""

from __future__ import annotations

from pathlib import Path

from agy_ui_mcp import screenshot
from agy_ui_mcp.scope import Target


# --- _load_axe_js -----------------------------------------------------------


def test_load_axe_js_returns_vendored_bundle():
    src = screenshot._load_axe_js()
    assert src is not None
    assert "axe" in src
    assert len(src) > 100_000  # the real minified bundle is ~0.5MB


def test_load_axe_js_none_when_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(screenshot, "_AXE_JS_CACHE", [])
    monkeypatch.setattr(screenshot, "_AXE_JS_PATH", tmp_path / "nope.js")
    assert screenshot._load_axe_js() is None


# --- summarize_a11y ---------------------------------------------------------


def test_summarize_a11y_empty():
    assert "no accessibility violations" in screenshot.summarize_a11y([])


def test_summarize_a11y_orders_by_impact_and_lists_nodes():
    violations = [
        {"id": "minor-thing", "impact": "minor", "help": "Minor", "helpUrl": "u1",
         "count": 1, "nodes": [{"target": [".x"], "failureSummary": "s"}]},
        {"id": "color-contrast", "impact": "critical", "help": "Contrast",
         "helpUrl": "u2", "count": 2,
         "nodes": [{"target": [".btn"], "failureSummary": "fix"}]},
    ]
    out = screenshot.summarize_a11y(violations)
    # critical must be listed before minor.
    assert out.index("color-contrast") < out.index("minor-thing")
    assert "[critical]" in out and "[minor]" in out
    assert ".btn" in out


# --- audit_a11y skip path ---------------------------------------------------


def test_audit_a11y_skips_when_bundle_missing(monkeypatch):
    monkeypatch.setattr(screenshot, "_load_axe_js", lambda: None)
    violations, warnings = screenshot.audit_a11y(
        "http://localhost:5173", Target(name="home", route="/"), {}
    )
    assert violations == []
    assert any("axe-core bundle not found" in w for w in warnings)
