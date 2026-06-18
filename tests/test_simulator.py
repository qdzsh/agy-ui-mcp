"""Tests for the native iOS Simulator screenshot adapter.

Every test here is fully OFFLINE: ``xcrun simctl`` is never run for real. The
single shell-out seam :meth:`SimulatorAdapter._run` is monkeypatched with a fake
that records the argv it was asked to run and returns a canned
``CompletedProcess``. This both keeps CI hermetic (no Xcode/simulator needed) and
lets each test assert the exact ``simctl`` command sequence the adapter issues.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agy_ui_mcp import screenshot
from agy_ui_mcp.scope import Device, Target


# --- helpers ----------------------------------------------------------------


def _completed(
    args: list[str], *, returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess:
    """Build a ``CompletedProcess`` mirroring ``subprocess.run`` output shape."""
    return subprocess.CompletedProcess(
        args=args, returncode=returncode, stdout=stdout, stderr=stderr
    )


def _list_json(*entries: dict) -> str:
    """Serialize a fake ``simctl list devices --json`` payload.

    Devices are grouped under a single arbitrary runtime key (the adapter scans
    every runtime, so the key value is irrelevant).
    """
    return json.dumps({"devices": {"com.apple.CoreSimulator.SimRuntime.iOS": list(entries)}})


def _record_runner(responses):
    """Build a fake ``_run`` that records argv and replays canned responses.

    Args:
        responses: Either a single ``CompletedProcess`` (reused for every call)
            or a callable ``argv -> CompletedProcess``.

    Returns:
        A ``(calls, fake_run)`` pair; ``calls`` accumulates each argv list.
    """
    calls: list[list[str]] = []

    def fake_run(args: list[str]) -> subprocess.CompletedProcess:
        calls.append(list(args))
        if callable(responses):
            return responses(args)
        return responses

    return calls, fake_run


# --- _resolve_udid ----------------------------------------------------------


def test_resolve_udid_returns_explicit_udid_without_simctl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A device with a ``udid`` resolves directly, issuing no simctl call."""
    calls, fake_run = _record_runner(_completed([], returncode=1))
    monkeypatch.setattr(screenshot.SimulatorAdapter, "_run", staticmethod(fake_run))

    adapter = screenshot.SimulatorAdapter()
    udid = adapter._resolve_udid(Device(udid="EXPLICIT-UDID-0001"))

    assert udid == "EXPLICIT-UDID-0001"
    assert calls == []  # no `simctl list` needed when udid is known


def test_resolve_udid_by_name_match(monkeypatch: pytest.MonkeyPatch) -> None:
    """A device ``name`` is resolved to a UDID via ``simctl list`` JSON."""
    payload = _list_json(
        {"name": "iPhone 16", "udid": "WRONG-1", "state": "Shutdown"},
        {"name": "iPhone 17", "udid": "RIGHT-17", "state": "Shutdown"},
    )
    calls, fake_run = _record_runner(_completed([], stdout=payload))
    monkeypatch.setattr(screenshot.SimulatorAdapter, "_run", staticmethod(fake_run))

    adapter = screenshot.SimulatorAdapter()
    udid = adapter._resolve_udid(Device(name="iPhone 17"))

    assert udid == "RIGHT-17"
    assert calls == [
        ["xcrun", "simctl", "list", "devices", "available", "--json"]
    ]


def test_resolve_udid_prefers_booted_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When several simulators share a name, a Booted one wins over the rest."""
    payload = _list_json(
        {"name": "iPhone 17", "udid": "SHUTDOWN-17", "state": "Shutdown"},
        {"name": "iPhone 17", "udid": "BOOTED-17", "state": "Booted"},
    )
    _calls, fake_run = _record_runner(_completed([], stdout=payload))
    monkeypatch.setattr(screenshot.SimulatorAdapter, "_run", staticmethod(fake_run))

    adapter = screenshot.SimulatorAdapter()
    assert adapter._resolve_udid(Device(name="iPhone 17")) == "BOOTED-17"


def test_resolve_udid_no_match_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """No simulator matching the name raises a clear RuntimeError."""
    payload = _list_json(
        {"name": "iPhone 16", "udid": "U-16", "state": "Shutdown"},
    )
    _calls, fake_run = _record_runner(_completed([], stdout=payload))
    monkeypatch.setattr(screenshot.SimulatorAdapter, "_run", staticmethod(fake_run))

    adapter = screenshot.SimulatorAdapter()
    with pytest.raises(RuntimeError, match="iPhone 17"):
        adapter._resolve_udid(Device(name="iPhone 17"))


def test_resolve_udid_simctl_failure_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-zero ``simctl list`` exit is surfaced as a RuntimeError."""
    _calls, fake_run = _record_runner(
        _completed([], returncode=1, stderr="boom")
    )
    monkeypatch.setattr(screenshot.SimulatorAdapter, "_run", staticmethod(fake_run))

    adapter = screenshot.SimulatorAdapter()
    with pytest.raises(RuntimeError, match="simctl list"):
        adapter._resolve_udid(Device(name="iPhone 17"))


# --- capture (full command sequence) ----------------------------------------


def test_capture_issues_boot_then_screenshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """capture resolves -> boots -> screenshots and returns the abspath."""
    out = tmp_path / "shots" / "home.png"

    def respond(args: list[str]) -> subprocess.CompletedProcess:
        return _completed(args)  # everything succeeds (returncode 0)

    calls, fake_run = _record_runner(respond)
    monkeypatch.setattr(screenshot.SimulatorAdapter, "_run", staticmethod(fake_run))

    adapter = screenshot.SimulatorAdapter()
    target = Target(name="home", device="sim")
    devices = {"sim": Device(udid="UDID-XYZ")}

    path, warnings = adapter.capture(target, devices, out)

    assert path == str(out.resolve())
    assert warnings == []
    # udid was explicit -> no `simctl list`; just bootstatus then io screenshot.
    assert calls == [
        ["xcrun", "simctl", "bootstatus", "UDID-XYZ", "-b"],
        [
            "xcrun",
            "simctl",
            "io",
            "UDID-XYZ",
            "screenshot",
            "--type=png",
            str(out),
        ],
    ]


def test_capture_resolves_name_before_screenshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A name-only device triggers a `simctl list` lookup before the shot."""
    out = tmp_path / "home.png"
    payload = _list_json(
        {"name": "iPhone 17", "udid": "RESOLVED-17", "state": "Shutdown"},
    )

    def respond(args: list[str]) -> subprocess.CompletedProcess:
        if args[:4] == ["xcrun", "simctl", "list", "devices"]:
            return _completed(args, stdout=payload)
        return _completed(args)

    calls, fake_run = _record_runner(respond)
    monkeypatch.setattr(screenshot.SimulatorAdapter, "_run", staticmethod(fake_run))

    adapter = screenshot.SimulatorAdapter()
    target = Target(name="home", device="sim")
    devices = {"sim": Device(name="iPhone 17")}

    path, warnings = adapter.capture(target, devices, out)

    assert path == str(out.resolve())
    assert warnings == []
    assert calls[0] == [
        "xcrun", "simctl", "list", "devices", "available", "--json"
    ]
    assert calls[-1] == [
        "xcrun", "simctl", "io", "RESOLVED-17", "screenshot",
        "--type=png", str(out),
    ]


def test_capture_screenshot_failure_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A failed `simctl io screenshot` is fatal (RuntimeError)."""
    out = tmp_path / "home.png"

    def respond(args: list[str]) -> subprocess.CompletedProcess:
        if "screenshot" in args:
            return _completed(args, returncode=1, stderr="device not booted")
        return _completed(args)

    _calls, fake_run = _record_runner(respond)
    monkeypatch.setattr(screenshot.SimulatorAdapter, "_run", staticmethod(fake_run))

    adapter = screenshot.SimulatorAdapter()
    target = Target(name="home", device="sim")
    devices = {"sim": Device(udid="UDID-XYZ")}

    with pytest.raises(RuntimeError, match="screenshot"):
        adapter.capture(target, devices, out)


def test_capture_already_booted_is_not_fatal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An 'already booted' bootstatus exit is swallowed, not raised."""
    out = tmp_path / "home.png"

    def respond(args: list[str]) -> subprocess.CompletedProcess:
        if "bootstatus" in args:
            return _completed(args, returncode=149, stderr="Unable to boot device in current state: Booted")
        return _completed(args)

    _calls, fake_run = _record_runner(respond)
    monkeypatch.setattr(screenshot.SimulatorAdapter, "_run", staticmethod(fake_run))

    adapter = screenshot.SimulatorAdapter()
    target = Target(name="home", device="sim")
    devices = {"sim": Device(udid="UDID-XYZ")}

    path, _warnings = adapter.capture(target, devices, out)
    assert path == str(out.resolve())


def test_capture_missing_device_key_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A target with no `device` is rejected with a clear error."""
    _calls, fake_run = _record_runner(_completed([]))
    monkeypatch.setattr(screenshot.SimulatorAdapter, "_run", staticmethod(fake_run))

    adapter = screenshot.SimulatorAdapter()
    with pytest.raises(RuntimeError, match="device"):
        adapter.capture(Target(name="home"), {}, tmp_path / "x.png")


def test_capture_unknown_device_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A target referencing a device absent from the registry raises."""
    _calls, fake_run = _record_runner(_completed([]))
    monkeypatch.setattr(screenshot.SimulatorAdapter, "_run", staticmethod(fake_run))

    adapter = screenshot.SimulatorAdapter()
    target = Target(name="home", device="ghost")
    with pytest.raises(RuntimeError, match="ghost"):
        adapter.capture(target, {"sim": Device(udid="U")}, tmp_path / "x.png")


# --- capture_for_platform dispatch ------------------------------------------


def test_capture_for_platform_dispatches_ios_sim(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`ios-sim` routes to the shared SimulatorAdapter, ignoring base_url."""
    out = tmp_path / "home.png"
    calls, fake_run = _record_runner(lambda args: _completed(args))
    monkeypatch.setattr(screenshot.SimulatorAdapter, "_run", staticmethod(fake_run))

    # Web path must NOT be taken for native.
    def fail_capture_target(*args, **kwargs):  # pragma: no cover
        raise AssertionError("capture_target must not run for ios-sim")

    monkeypatch.setattr(screenshot, "capture_target", fail_capture_target)

    target = Target(name="home", device="sim")
    devices = {"sim": Device(udid="UDID-XYZ")}

    path, warnings = screenshot.capture_for_platform(
        "ios-sim",
        "http://unused",  # base_url ignored by the native path
        target,
        devices,
        out,
        settle_ms=9999,  # ignored too
    )

    assert path == str(out.resolve())
    assert warnings == []
    # Proof the native command path actually ran.
    assert any("screenshot" in argv for argv in calls)


def test_capture_for_platform_dispatches_android_emu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`android-emu` routes to the AndroidAdapter (see tests/test_android.py)."""

    def fail_capture_target(*args, **kwargs):  # pragma: no cover
        raise AssertionError("capture_target must not run for android-emu")

    monkeypatch.setattr(screenshot, "capture_target", fail_capture_target)
    monkeypatch.setattr(
        screenshot._ANDROID_ADAPTER,
        "capture",
        lambda target, devices, out_path: ("android.png", []),
    )

    path, warnings = screenshot.capture_for_platform(
        "android-emu",
        "http://unused",
        Target(name="home", device="sim"),
        {"sim": Device(udid="emulator-5554")},
        "/tmp/out.png",
    )
    assert (path, warnings) == ("android.png", [])


# --- Device model validation ------------------------------------------------


def test_device_udid_only_is_valid() -> None:
    """A udid-only device is valid (no name/width/height required)."""
    device = Device(udid="ABCD-1234")
    assert device.udid == "ABCD-1234"
    assert device.name is None
    assert device.width is None and device.height is None


def test_device_name_only_is_valid() -> None:
    """A name-only device (simulator or Playwright registry) is valid."""
    device = Device(name="iPhone 17")
    assert device.name == "iPhone 17"
    assert device.udid is None


def test_device_width_height_only_is_valid() -> None:
    """An explicit width+height device remains valid (web-target path)."""
    device = Device(width=1440, height=900)
    assert device.width == 1440 and device.height == 900


def test_device_empty_is_invalid() -> None:
    """A device with no name, no udid, and no dimensions fails validation."""
    with pytest.raises(ValueError):
        Device()


def test_device_width_without_height_is_invalid() -> None:
    """Half a dimension pair (and no name/udid) is still invalid."""
    with pytest.raises(ValueError):
        Device(width=1440)
