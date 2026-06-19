"""Tests for the ``_launch_chromium`` browser-launch helper in ``screenshot``.

These cover the env-driven launch options (channel / executable path) and the
first-run auto-install fallback entirely offline: a fake ``pw`` object stands in
for a real Playwright context, and ``subprocess.run`` is monkeypatched so no real
browser is ever downloaded.
"""

from __future__ import annotations

from typing import Any

import pytest

from agy_ui_mcp import screenshot


# A representative Playwright "browser binary missing" message. The helper matches
# case-insensitively on stable fragments of messages like this one.
_MISSING_MSG = (
    "Executable doesn't exist at /home/u/.cache/ms-playwright/chromium-1091/"
    "chrome-linux/chrome. Looks like Playwright was just installed or updated. "
    "Please run the following command to download new browsers: playwright install"
)


class _FakeChromium:
    """Records ``launch`` kwargs and replays a scripted sequence of results."""

    def __init__(self, results: list[Any]) -> None:
        #: Each entry is either an Exception (raised) or a value (returned).
        self._results = list(results)
        self.calls: list[dict[str, Any]] = []

    def launch(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        outcome = self._results.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _FakePw:
    """Minimal stand-in for a ``sync_playwright`` context."""

    def __init__(self, results: list[Any]) -> None:
        self.chromium = _FakeChromium(results)


@pytest.fixture(autouse=True)
def _reset_install_flag(monkeypatch):
    """Reset the once-per-process install guard so tests are order-independent."""
    monkeypatch.setattr(screenshot, "_CHROMIUM_INSTALL_ATTEMPTED", False)


@pytest.fixture(autouse=True)
def _clear_browser_env(monkeypatch):
    """Start each test from a clean env so host settings never leak in."""
    for name in (
        "AGY_UI_CHROME_CHANNEL",
        "AGY_UI_CHROME_EXECUTABLE",
        "AGY_UI_NO_BROWSER_AUTOINSTALL",
        "AGY_UI_DRY_RUN",
    ):
        monkeypatch.delenv(name, raising=False)


# --- channel ----------------------------------------------------------------


def test_channel_env_passed_to_launch(monkeypatch):
    monkeypatch.setenv("AGY_UI_CHROME_CHANNEL", "chrome")
    sentinel = object()
    pw = _FakePw([sentinel])

    result = screenshot._launch_chromium(pw)

    assert result is sentinel
    assert pw.chromium.calls == [{"headless": True, "channel": "chrome"}]


def test_no_channel_kwarg_without_env():
    sentinel = object()
    pw = _FakePw([sentinel])

    result = screenshot._launch_chromium(pw)

    assert result is sentinel
    assert pw.chromium.calls == [{"headless": True}]
    assert "channel" not in pw.chromium.calls[0]


# --- executable -------------------------------------------------------------


def test_executable_env_passed_to_launch(monkeypatch):
    monkeypatch.setenv("AGY_UI_CHROME_EXECUTABLE", "/path/chrome")
    sentinel = object()
    pw = _FakePw([sentinel])

    result = screenshot._launch_chromium(pw)

    assert result is sentinel
    assert pw.chromium.calls == [
        {"headless": True, "executable_path": "/path/chrome"}
    ]


# --- auto-install -----------------------------------------------------------


def test_auto_install_on_missing_browser(monkeypatch):
    recorded: list[list[str]] = []

    def _fake_run(cmd, *args, **kwargs):
        recorded.append(cmd)

    monkeypatch.setattr(screenshot.subprocess, "run", _fake_run)

    sentinel = object()
    pw = _FakePw([Exception(_MISSING_MSG), sentinel])

    result = screenshot._launch_chromium(pw)

    assert result is sentinel
    assert len(recorded) == 1
    cmd = recorded[0]
    assert "playwright" in cmd
    assert "install" in cmd
    assert "chromium" in cmd
    # Launch was attempted twice: the failing first try and the post-install retry.
    assert len(pw.chromium.calls) == 2


def test_auto_install_disabled_propagates(monkeypatch):
    monkeypatch.setenv("AGY_UI_NO_BROWSER_AUTOINSTALL", "1")
    recorded: list[Any] = []
    monkeypatch.setattr(
        screenshot.subprocess, "run", lambda *a, **k: recorded.append(a)
    )

    pw = _FakePw([Exception(_MISSING_MSG)])

    with pytest.raises(Exception, match="Executable doesn't exist"):
        screenshot._launch_chromium(pw)

    assert recorded == []


def test_non_install_error_propagates(monkeypatch):
    recorded: list[Any] = []
    monkeypatch.setattr(
        screenshot.subprocess, "run", lambda *a, **k: recorded.append(a)
    )

    pw = _FakePw([RuntimeError("boom")])

    with pytest.raises(RuntimeError, match="boom"):
        screenshot._launch_chromium(pw)

    assert recorded == []


def test_channel_skips_auto_install(monkeypatch):
    monkeypatch.setenv("AGY_UI_CHROME_CHANNEL", "chrome")
    recorded: list[Any] = []
    monkeypatch.setattr(
        screenshot.subprocess, "run", lambda *a, **k: recorded.append(a)
    )

    pw = _FakePw([Exception(_MISSING_MSG)])

    with pytest.raises(Exception, match="Executable doesn't exist"):
        screenshot._launch_chromium(pw)

    # A channel drives a system browser, so installing Chromium would not help.
    assert recorded == []


def test_executable_skips_auto_install(monkeypatch):
    monkeypatch.setenv("AGY_UI_CHROME_EXECUTABLE", "/path/to/missing-chrome")
    recorded: list[Any] = []
    monkeypatch.setattr(
        screenshot.subprocess, "run", lambda *a, **k: recorded.append(a)
    )

    pw = _FakePw([Exception(_MISSING_MSG)])

    with pytest.raises(Exception, match="Executable doesn't exist"):
        screenshot._launch_chromium(pw)

    # A user-specified executable cannot be fixed by a Chromium download (and the
    # retry would only reuse the same bad path), so install must NOT be invoked
    # and the original launch error propagates.
    assert recorded == []
    # Only the single failing launch was attempted (no post-install retry).
    assert len(pw.chromium.calls) == 1
