"""Tests for the native Android emulator screenshot adapter (``adb``).

Fully OFFLINE: the single shell-out seam :meth:`AndroidAdapter._run` is
monkeypatched with a fake that records argv and replays canned
``CompletedProcess`` results, so no real ``adb``/emulator is needed. This mirrors
``tests/test_simulator.py`` for the iOS adapter.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agy_ui_mcp import screenshot
from agy_ui_mcp.scope import Device, Target


# --- helpers ----------------------------------------------------------------


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        args=["adb"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _fake_run(handler):
    """Build a fake ``_run(args, *, binary=False)`` recording argv.

    ``handler`` maps an argv list to a ``CompletedProcess`` (or returns None to
    fall through to a generic success). Returns ``(calls, fake)``.
    """
    calls: list[list[str]] = []

    def fake(args, *, binary=False, timeout=None):
        calls.append(list(args))
        result = handler(list(args), binary)
        return result if result is not None else _completed()

    return calls, fake


def _install(monkeypatch, fake):
    monkeypatch.setattr(
        screenshot.AndroidAdapter, "_run", staticmethod(fake)
    )


# --- _resolve_adb -----------------------------------------------------------


def test_resolve_adb_prefers_env_override(monkeypatch, tmp_path):
    adb = tmp_path / "adb"
    adb.write_text("#!/bin/sh\n")
    monkeypatch.setenv("ADB", str(adb))
    assert screenshot._resolve_adb() == str(adb)


def test_resolve_adb_uses_path(monkeypatch):
    monkeypatch.delenv("ADB", raising=False)
    monkeypatch.setattr(screenshot.shutil, "which", lambda _n: "/usr/bin/adb")
    assert screenshot._resolve_adb() == "/usr/bin/adb"


def test_resolve_adb_raises_when_missing(monkeypatch, tmp_path):
    monkeypatch.delenv("ADB", raising=False)
    monkeypatch.delenv("ANDROID_HOME", raising=False)
    monkeypatch.delenv("ANDROID_SDK_ROOT", raising=False)
    monkeypatch.setattr(screenshot.shutil, "which", lambda _n: None)
    # Point HOME at an empty dir so the SDK fallbacks do not resolve.
    monkeypatch.setattr(screenshot.Path, "home", staticmethod(lambda: tmp_path))
    with pytest.raises(RuntimeError, match="adb"):
        screenshot._resolve_adb()


# --- _running_serials / _avd_name -------------------------------------------


def test_running_serials_parses_device_lines(monkeypatch):
    out = "List of devices attached\nemulator-5554\tdevice\nemulator-5556\toffline\n"
    _, fake = _fake_run(lambda a, b: _completed(stdout=out) if a == ["devices"] else None)
    _install(monkeypatch, fake)
    assert screenshot.AndroidAdapter()._running_serials() == ["emulator-5554"]


def test_avd_name_reads_first_nonok_line(monkeypatch):
    def handler(a, b):
        if a[-3:] == ["emu", "avd", "name"]:
            return _completed(stdout="Pixel_9\nOK\n")
        return None

    _, fake = _fake_run(handler)
    _install(monkeypatch, fake)
    assert screenshot.AndroidAdapter()._avd_name("emulator-5554") == "Pixel_9"


# --- _resolve_serial --------------------------------------------------------


def test_resolve_serial_returns_udid_without_adb(monkeypatch):
    calls, fake = _fake_run(lambda a, b: None)
    _install(monkeypatch, fake)
    serial = screenshot.AndroidAdapter()._resolve_serial(Device(udid="emulator-5554"))
    assert serial == "emulator-5554"
    assert calls == []  # no adb call when udid is explicit


def test_resolve_serial_requires_udid_or_name(monkeypatch):
    _, fake = _fake_run(lambda a, b: None)
    _install(monkeypatch, fake)
    with pytest.raises(RuntimeError, match="udid.*name|name.*udid"):
        screenshot.AndroidAdapter()._resolve_serial(Device(width=400, height=800))


def test_resolve_serial_matches_avd_name(monkeypatch):
    def handler(a, b):
        if a == ["devices"]:
            return _completed(stdout="List of devices attached\nemulator-5554\tdevice\n")
        if a[-3:] == ["emu", "avd", "name"]:
            return _completed(stdout="Pixel_9\nOK\n")
        return None

    _, fake = _fake_run(handler)
    _install(monkeypatch, fake)
    serial = screenshot.AndroidAdapter()._resolve_serial(Device(name="Pixel_9"))
    assert serial == "emulator-5554"


def test_resolve_serial_auto_launches_when_none_running(monkeypatch):
    """No running emulator -> the named AVD is auto-launched, serial returned."""
    _, fake = _fake_run(
        lambda a, b: _completed(stdout="List of devices attached\n")
        if a == ["devices"] else None
    )
    _install(monkeypatch, fake)
    launched: list[str] = []

    def fake_launch(self, avd_name, timeout=180):
        launched.append(avd_name)
        return "emulator-5560"

    monkeypatch.setattr(screenshot.AndroidAdapter, "_launch_emulator", fake_launch)

    serial = screenshot.AndroidAdapter()._resolve_serial(Device(name="Pixel_9"))
    assert serial == "emulator-5560"
    assert launched == ["Pixel_9"]


def test_resolve_serial_auto_launches_when_running_mismatch(monkeypatch):
    """A running but DIFFERENT (readable) AVD -> launch the requested one."""
    def handler(a, b):
        if a == ["devices"]:
            return _completed(stdout="List of devices attached\nemulator-5554\tdevice\n")
        if a[-3:] == ["emu", "avd", "name"]:
            return _completed(stdout="OtherAvd\nOK\n")
        return None

    _, fake = _fake_run(handler)
    _install(monkeypatch, fake)
    monkeypatch.setattr(
        screenshot.AndroidAdapter,
        "_launch_emulator",
        lambda self, avd_name, timeout=180: "emulator-5562",
    )
    serial = screenshot.AndroidAdapter()._resolve_serial(Device(name="Pixel_9"))
    assert serial == "emulator-5562"


def test_resolve_serial_single_emulator_fallback(monkeypatch):
    """One running emulator whose AVD name is unreadable is used best-effort."""
    def handler(a, b):
        if a == ["devices"]:
            return _completed(stdout="List of devices attached\nemulator-5554\tdevice\n")
        if a[-3:] == ["emu", "avd", "name"]:
            return _completed(returncode=1, stderr="unreadable")
        return None

    _, fake = _fake_run(handler)
    _install(monkeypatch, fake)
    serial = screenshot.AndroidAdapter()._resolve_serial(Device(name="SomethingElse"))
    assert serial == "emulator-5554"


# --- _ensure_booted ---------------------------------------------------------


def test_ensure_booted_returns_when_boot_completed(monkeypatch):
    def handler(a, b):
        if a[-1] == "sys.boot_completed":
            return _completed(stdout="1\n")
        return None

    _, fake = _fake_run(handler)
    _install(monkeypatch, fake)
    # Should return without raising.
    screenshot.AndroidAdapter()._ensure_booted("emulator-5554", timeout=5)


def test_ensure_booted_times_out(monkeypatch):
    _, fake = _fake_run(
        lambda a, b: _completed(stdout="0\n") if a[-1] == "sys.boot_completed" else None
    )
    _install(monkeypatch, fake)
    monkeypatch.setattr(screenshot.time, "sleep", lambda *_: None)
    ticks = iter([0.0, 1.0, 100.0, 200.0])
    monkeypatch.setattr(screenshot.time, "monotonic", lambda: next(ticks))
    with pytest.raises(RuntimeError, match="did not finish booting"):
        screenshot.AndroidAdapter()._ensure_booted("emulator-5554", timeout=10)


# --- capture ----------------------------------------------------------------

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def test_capture_writes_png_bytes(monkeypatch, tmp_path):
    def handler(a, b):
        if a[-1] == "sys.boot_completed":
            return _completed(stdout="1\n")
        if a[-3:] == ["exec-out", "screencap", "-p"]:
            assert b is True  # binary capture
            return _completed(stdout=_PNG)
        return None

    calls, fake = _fake_run(handler)
    _install(monkeypatch, fake)

    out = tmp_path / "shot.png"
    path, warnings = screenshot.AndroidAdapter().capture(
        Target(name="home", device="emu"),
        {"emu": Device(udid="emulator-5554")},
        out,
    )
    assert path == str(out.resolve())
    assert warnings == []
    assert out.read_bytes() == _PNG
    assert any(c[-3:] == ["exec-out", "screencap", "-p"] for c in calls)


def test_capture_raises_on_screencap_failure(monkeypatch, tmp_path):
    def handler(a, b):
        if a[-1] == "sys.boot_completed":
            return _completed(stdout="1\n")
        if a[-3:] == ["exec-out", "screencap", "-p"]:
            return _completed(returncode=1, stderr=b"device offline")
        return None

    _, fake = _fake_run(handler)
    _install(monkeypatch, fake)
    with pytest.raises(RuntimeError, match="screencap"):
        screenshot.AndroidAdapter().capture(
            Target(name="home", device="emu"),
            {"emu": Device(udid="emulator-5554")},
            tmp_path / "x.png",
        )


def test_capture_raises_for_unknown_device(monkeypatch, tmp_path):
    _, fake = _fake_run(lambda a, b: None)
    _install(monkeypatch, fake)
    with pytest.raises(RuntimeError, match="ghost"):
        screenshot.AndroidAdapter().capture(
            Target(name="home", device="ghost"),
            {"emu": Device(udid="emulator-5554")},
            tmp_path / "x.png",
        )


def test_capture_raises_when_device_unset(monkeypatch, tmp_path):
    _, fake = _fake_run(lambda a, b: None)
    _install(monkeypatch, fake)
    with pytest.raises(RuntimeError, match="no `device`"):
        screenshot.AndroidAdapter().capture(
            Target(name="home"),
            {"emu": Device(udid="emulator-5554")},
            tmp_path / "x.png",
        )


# --- _resolve_emulator ------------------------------------------------------


def test_resolve_emulator_uses_path(monkeypatch):
    monkeypatch.delenv("ANDROID_EMULATOR", raising=False)
    monkeypatch.setattr(screenshot.shutil, "which", lambda _n: "/usr/bin/emulator")
    assert screenshot._resolve_emulator() == "/usr/bin/emulator"


def test_resolve_emulator_raises_when_missing(monkeypatch, tmp_path):
    monkeypatch.delenv("ANDROID_EMULATOR", raising=False)
    monkeypatch.delenv("ANDROID_HOME", raising=False)
    monkeypatch.delenv("ANDROID_SDK_ROOT", raising=False)
    monkeypatch.setattr(screenshot.shutil, "which", lambda _n: None)
    monkeypatch.setattr(screenshot.Path, "home", staticmethod(lambda: tmp_path))
    with pytest.raises(RuntimeError, match="emulator"):
        screenshot._resolve_emulator()


# --- _attached_serials / _launch_emulator -----------------------------------


def test_attached_serials_includes_offline(monkeypatch):
    out = "List of devices attached\nemulator-5554\tdevice\nemulator-5556\toffline\n"
    _, fake = _fake_run(lambda a, b: _completed(stdout=out) if a == ["devices"] else None)
    _install(monkeypatch, fake)
    assert screenshot.AndroidAdapter()._attached_serials() == [
        "emulator-5554",
        "emulator-5556",
    ]


def test_launch_emulator_returns_verified_new_serial(monkeypatch):
    """Binds only to a new, fully-connected serial whose AVD name matches."""
    monkeypatch.setattr(screenshot, "_resolve_emulator", lambda: "/fake/emulator")
    monkeypatch.setattr(screenshot.time, "sleep", lambda *_: None)
    argv_seen: dict = {}

    class _Proc:
        returncode = None

        def poll(self):
            return None

    def fake_popen(argv, **kwargs):
        argv_seen["argv"] = argv
        return _Proc()

    monkeypatch.setattr(screenshot.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(screenshot.AndroidAdapter, "_attached_serials", lambda self: [])
    # `device`-state serial appears on the 2nd poll; its AVD name matches.
    running = iter([[], ["emulator-5560"]])
    monkeypatch.setattr(
        screenshot.AndroidAdapter, "_running_serials", lambda self: next(running)
    )
    monkeypatch.setattr(
        screenshot.AndroidAdapter, "_avd_name", lambda self, s: "Pixel_9"
    )

    serial = screenshot.AndroidAdapter()._launch_emulator("Pixel_9", timeout=30)
    assert serial == "emulator-5560"
    assert argv_seen["argv"][:3] == ["/fake/emulator", "-avd", "Pixel_9"]


def test_launch_emulator_ignores_wrong_avd_then_binds_right_one(monkeypatch):
    """A concurrently-attached different emulator is skipped, not bound to."""
    monkeypatch.setattr(screenshot, "_resolve_emulator", lambda: "/fake/emulator")
    monkeypatch.setattr(screenshot.time, "sleep", lambda *_: None)

    class _Proc:
        returncode = None

        def poll(self):
            return None

    monkeypatch.setattr(screenshot.subprocess, "Popen", lambda argv, **kw: _Proc())
    monkeypatch.setattr(screenshot.AndroidAdapter, "_attached_serials", lambda self: [])
    # First a wrong-AVD emulator appears, then ours.
    running = iter([["emulator-5554"], ["emulator-5554", "emulator-5560"]])
    monkeypatch.setattr(
        screenshot.AndroidAdapter, "_running_serials", lambda self: next(running)
    )
    names = {"emulator-5554": "OtherAvd", "emulator-5560": "Pixel_9"}
    monkeypatch.setattr(
        screenshot.AndroidAdapter, "_avd_name", lambda self, s: names[s]
    )

    serial = screenshot.AndroidAdapter()._launch_emulator("Pixel_9", timeout=30)
    assert serial == "emulator-5560"


def test_launch_emulator_raises_when_process_exits(monkeypatch):
    monkeypatch.setattr(screenshot, "_resolve_emulator", lambda: "/fake/emulator")
    monkeypatch.setattr(screenshot.time, "sleep", lambda *_: None)

    class _Proc:
        returncode = 1

        def poll(self):
            return 1

    monkeypatch.setattr(screenshot.subprocess, "Popen", lambda argv, **kw: _Proc())
    monkeypatch.setattr(screenshot.AndroidAdapter, "_attached_serials", lambda self: [])
    monkeypatch.setattr(screenshot.AndroidAdapter, "_running_serials", lambda self: [])
    with pytest.raises(RuntimeError, match="exited"):
        screenshot.AndroidAdapter()._launch_emulator("Pixel_9", timeout=30)
