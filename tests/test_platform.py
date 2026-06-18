"""Tests for the ``platform`` field and the screenshot platform adapter.

These tests stay fully offline: the only web-target capture path is exercised
by monkeypatching :func:`screenshot.capture_target` with a stub, so no real
Playwright/browser is launched.
"""

from __future__ import annotations

import pytest

from agy_ui_mcp import screenshot
from agy_ui_mcp.scope import AgyUiScope, Device, Target

WEB_PLATFORMS = ["web", "expo-web", "flutter-web", "ionic"]
NATIVE_PLATFORMS = ["ios-sim", "android-emu"]


# --- scope.platform field ---------------------------------------------------


def test_platform_defaults_to_web() -> None:
    """An unspecified platform falls back to ``web`` (backward-compatible)."""
    scope = AgyUiScope()
    assert scope.platform == "web"


@pytest.mark.parametrize("platform", WEB_PLATFORMS + NATIVE_PLATFORMS)
def test_valid_platforms_parse(platform: str) -> None:
    """Every declared platform value validates and round-trips."""
    scope = AgyUiScope(platform=platform)
    assert scope.platform == platform


def test_invalid_platform_rejected() -> None:
    """An unknown platform string fails Pydantic validation."""
    with pytest.raises(ValueError):
        AgyUiScope(platform="react-native-native")


# --- capture_for_platform dispatch ------------------------------------------


@pytest.mark.parametrize("platform", WEB_PLATFORMS)
def test_web_target_dispatches_to_capture_target(
    platform: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Web-targets pass straight through to ``capture_target`` unchanged."""
    calls: list[tuple] = []

    def fake_capture_target(
        base_url, target, devices, out_path, *, settle_ms=1500
    ) -> tuple[str, list[str]]:
        calls.append((base_url, target, devices, out_path, settle_ms))
        return ("x.png", [])

    monkeypatch.setattr(screenshot, "capture_target", fake_capture_target)

    target = Target(name="home", route="/")
    result = screenshot.capture_for_platform(
        platform,
        "http://localhost:5173",
        target,
        {},
        "/tmp/out.png",
        settle_ms=999,
    )

    assert result == ("x.png", [])
    assert len(calls) == 1
    base_url, fwd_target, devices, out_path, settle_ms = calls[0]
    assert base_url == "http://localhost:5173"
    assert fwd_target is target
    assert devices == {}
    assert out_path == "/tmp/out.png"
    assert settle_ms == 999


def test_android_platform_dispatches_to_android_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """android-emu routes to the AndroidAdapter, never to ``capture_target``.

    Both native platforms are now implemented (ios-sim via simctl, android-emu
    via adb); web-only ``capture_target`` must not run for either.
    """

    def fail_capture_target(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("capture_target must not be called for native")

    monkeypatch.setattr(screenshot, "capture_target", fail_capture_target)

    captured: list[tuple] = []

    def fake_capture(target, devices, out_path):
        captured.append((target, devices, out_path))
        return ("android.png", [])

    monkeypatch.setattr(screenshot._ANDROID_ADAPTER, "capture", fake_capture)

    target = Target(name="home", device="sim")
    devices = {"sim": Device(udid="emulator-5554")}
    result = screenshot.capture_for_platform(
        "android-emu",
        "http://localhost:5173",  # ignored for native
        target,
        devices,
        "/tmp/out.png",
    )

    assert result == ("android.png", [])
    assert captured == [(target, devices, "/tmp/out.png")]


def test_unknown_platform_raises_value_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unrecognized platform string is a configuration error (ValueError)."""

    def fail_capture_target(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("capture_target must not be called")

    monkeypatch.setattr(screenshot, "capture_target", fail_capture_target)

    with pytest.raises(ValueError):
        screenshot.capture_for_platform(
            "windows-uwp",
            "http://localhost:5173",
            Target(name="home"),
            {},
            "/tmp/out.png",
        )


def test_simulator_adapter_capture_offline(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    """The native adapter now captures via simctl (mocked, fully offline).

    The detailed behavior lives in ``tests/test_simulator.py``; this is a smoke
    check that ``SimulatorAdapter`` is no longer an unconditional stub and that
    its single subprocess seam is the only thing standing between it and a real
    ``xcrun simctl`` call.
    """
    out = tmp_path / "shot.png"

    def fake_run(args):
        import subprocess

        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(
        screenshot.SimulatorAdapter, "_run", staticmethod(fake_run)
    )

    adapter = screenshot.SimulatorAdapter()
    path, warnings = adapter.capture(
        Target(name="home", device="sim"),
        {"sim": Device(udid="UDID-1")},
        out,
    )
    assert path == str(out.resolve())
    assert warnings == []
