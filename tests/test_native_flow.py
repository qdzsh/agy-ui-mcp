"""Tests for the native (ios-sim) wiring in the ui_implement / ui_review loop.

Every test here is fully OFFLINE: no agy/flutter/simctl/dev-server is ever run.
The two seams the native flow leans on are monkeypatched —
``server.screenshot.capture_for_platform`` (the screenshot poll) and the
``_start_dev_server`` / ``_stop_dev_server`` process helpers — so the orchestration
logic (log-marker readiness, frame-stable fallback, pty-driven hot reload) can be
asserted without a simulator. Web behavior is covered by the existing suites and
must stay green.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from agy_ui_mcp import server
from agy_ui_mcp.scope import AgyUiScope, Device, ServeConfig, Target


# --- helpers ----------------------------------------------------------------


def _ticks(values: list[float]):
    """A fake ``time.monotonic`` returning successive ``values`` on each call."""
    it = iter(values)
    return lambda: next(it)


def _native_scope(ready_timeout: int = 120) -> AgyUiScope:
    """Build a minimal ios-sim scope referencing one simulator device."""
    return AgyUiScope(
        platform="ios-sim",
        serve=ServeConfig(
            cmd="flutter run -d UDID",
            url="",
            ready_timeout=ready_timeout,
        ),
        devices={"sim": Device(udid="UDID-XYZ")},
        targets=[Target(name="home", device="sim")],
    )


# --- _wait_native_ready: log-based (preferred) ------------------------------


def test_wait_native_ready_true_when_log_has_launch_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A flutter-run log containing a launch marker signals the app is up."""
    scope = _native_scope()
    target = scope.targets[0]
    log = tmp_path / "flutter.log"
    # Mixed case proves the match is case-insensitive.
    log.write_text("Launching lib/main.dart...\nSyncing files to device iPhone\n")

    slept: list[float] = []
    monkeypatch.setattr(server.time, "sleep", lambda s=0, *a, **k: slept.append(s))
    # Capture must NOT be used on the log path — fail loudly if it is.
    monkeypatch.setattr(
        server.screenshot,
        "capture_for_platform",
        lambda *a, **k: pytest.fail("capture must not run when a marker is found"),
    )

    ok = server._wait_native_ready(
        scope, target, scope.devices, str(log), timeout=120
    )

    assert ok is True
    # The first-frame settle sleep fired (no real wait thanks to the stub).
    assert server._NATIVE_FIRST_FRAME_SETTLE_S in slept


def test_wait_native_ready_false_when_log_has_no_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A log without any launch marker times out to False (caller warns)."""
    scope = _native_scope()
    target = scope.targets[0]
    log = tmp_path / "flutter.log"
    log.write_text("Resolving dependencies...\nRunning Gradle task...\n")

    monkeypatch.setattr(server.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(
        server.screenshot,
        "capture_for_platform",
        lambda *a, **k: pytest.fail("capture must not run on the log path"),
    )

    # Tiny timeout: the monotonic deadline elapses almost immediately.
    ok = server._wait_native_ready(
        scope, target, scope.devices, str(log), timeout=1
    )

    assert ok is False


def test_wait_native_ready_log_swallows_read_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A transient log read error is swallowed; polling continues to the marker."""
    import builtins

    scope = _native_scope()
    target = scope.targets[0]
    log = tmp_path / "flutter.log"
    log.write_text("Dart VM Service is available at http://127.0.0.1:1234/\n")

    real_open = builtins.open
    state = {"n": 0}

    def flaky_open(path, *a, **k):  # noqa: ANN001
        # Only interfere with reads of the native log; leave everything else.
        if str(path) == str(log):
            state["n"] += 1
            if state["n"] == 1:
                raise OSError("log temporarily locked")
        return real_open(path, *a, **k)

    monkeypatch.setattr(server.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(builtins, "open", flaky_open)

    ok = server._wait_native_ready(
        scope, target, scope.devices, str(log), timeout=120
    )

    assert ok is True
    assert state["n"] >= 2  # first read raised, a later read succeeded


# --- _wait_native_ready: frame-stable fallback (no log) ---------------------


def test_wait_native_ready_fallback_true_after_min_wait_and_three_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no log, stability requires >=3 frames AND the minimum elapsed wait."""
    scope = _native_scope()
    target = scope.targets[0]
    calls: list[str] = []
    # frame-1 repeats from call 2 onward, so two identical frames are available
    # well before the third capture.
    payloads = [b"boot-0", b"frame-1", b"frame-1", b"frame-1"]

    def fake_capture(platform, base_url, t, devices, out_path, **kwargs):
        assert platform == "ios-sim"
        assert base_url == ""
        idx = len(calls)
        calls.append(platform)
        data = payloads[idx] if idx < len(payloads) else payloads[-1]
        Path(out_path).write_bytes(data)
        return str(out_path), []

    # Advance a fake clock so the min-wait gate is satisfied without real time.
    clock = {"t": 0.0}
    monkeypatch.setattr(server.time, "monotonic", lambda: clock["t"])

    def fake_sleep(*_a, **_k):
        clock["t"] += 10.0  # each poll advances the fake clock by 10s

    monkeypatch.setattr(server.time, "sleep", fake_sleep)
    monkeypatch.setattr(
        server.screenshot, "capture_for_platform", fake_capture
    )

    ok = server._wait_native_ready(
        scope, target, scope.devices, None, timeout=120
    )

    assert ok is True
    # Needs at least 3 captures (the early 2-identical frames are not trusted
    # until the frame count and min-wait gates both pass).
    assert len(calls) >= 3


def test_wait_native_ready_fallback_times_out_when_never_stable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no log and never-stable frames, the fallback returns False."""
    scope = _native_scope()
    target = scope.targets[0]
    counter = {"n": 0}

    def fake_capture(platform, base_url, t, devices, out_path, **kwargs):
        counter["n"] += 1
        Path(out_path).write_bytes(f"frame-{counter['n']}".encode())
        return str(out_path), []

    monkeypatch.setattr(server.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(
        server.screenshot, "capture_for_platform", fake_capture
    )

    ok = server._wait_native_ready(
        scope, target, scope.devices, None, timeout=1
    )

    assert ok is False


def test_wait_native_ready_fallback_swallows_capture_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A capture error (app not up yet) is ignored; polling continues to stable."""
    scope = _native_scope()
    target = scope.targets[0]
    state = {"n": 0}

    def fake_capture(platform, base_url, t, devices, out_path, **kwargs):
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("device not booted")
        Path(out_path).write_bytes(b"ready")
        return str(out_path), []

    clock = {"t": 0.0}
    monkeypatch.setattr(server.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(
        server.time, "sleep", lambda *_a, **_k: clock.__setitem__("t", clock["t"] + 10.0)
    )
    monkeypatch.setattr(
        server.screenshot, "capture_for_platform", fake_capture
    )

    ok = server._wait_native_ready(
        scope, target, scope.devices, None, timeout=120
    )

    assert ok is True


# --- _start_dev_server: native vs web redirect ------------------------------


def test_start_dev_server_native_redirects_to_log_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Native start returns a log path and pipes child output into that file."""
    scope = _native_scope()
    captured: dict[str, object] = {}

    class FakeProc:
        pass

    def fake_popen(cmd, **kwargs):  # noqa: ANN001
        captured["cmd"] = cmd
        captured["stdout"] = kwargs.get("stdout")
        captured["stderr"] = kwargs.get("stderr")
        return FakeProc()

    monkeypatch.setattr(server.subprocess, "Popen", fake_popen)

    proc, log_path = server._start_dev_server(scope, "/proj")

    assert isinstance(proc, FakeProc)
    assert log_path is not None and Path(log_path).exists()
    # stdout is a real writable file object (the open log), stderr merges into it.
    assert captured["stdout"] is not None
    assert captured["stderr"] == server.subprocess.STDOUT
    # Clean up the temp log the real helper created.
    server._cleanup_native_log(log_path)
    assert not Path(log_path).exists()


def test_start_dev_server_web_uses_devnull_and_no_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Web start keeps DEVNULL and reports no log path."""
    scope = AgyUiScope(
        platform="flutter-web",
        serve=ServeConfig(cmd="python -m http.server", url="http://localhost:8000"),
    )
    captured: dict[str, object] = {}

    def fake_popen(cmd, **kwargs):  # noqa: ANN001
        captured["stdout"] = kwargs.get("stdout")
        captured["stderr"] = kwargs.get("stderr")
        return object()

    monkeypatch.setattr(server.subprocess, "Popen", fake_popen)

    proc, log_path = server._start_dev_server(scope, "/proj")

    assert proc is not None
    assert log_path is None
    assert captured["stdout"] == server.subprocess.DEVNULL
    assert captured["stderr"] == server.subprocess.DEVNULL


def test_start_dev_server_returns_none_tuple_without_serve() -> None:
    """No serve command -> (None, None)."""
    scope = AgyUiScope(platform="ios-sim", serve=None)
    assert server._start_dev_server(scope, "/proj") == (None, None)


# --- pty-driven native session: _start_native / _hot_reload_native / _stop_native


class _FakeProc:
    """Minimal Popen stand-in for the native pty session helpers."""

    def __init__(self, poll_value: int | None = None, wait_raises: bool = False):
        self._poll = poll_value
        self._wait_raises = wait_raises
        self.pid = 999999
        self.wait_calls = 0

    def poll(self):
        return self._poll

    def wait(self, timeout=None):
        self.wait_calls += 1
        if self._wait_raises:
            raise subprocess.TimeoutExpired("flutter", timeout)
        return 0


def _finished_thread() -> threading.Thread:
    """A thread that has already run to completion (joins instantly)."""
    t = threading.Thread(target=lambda: None)
    t.start()
    t.join()
    return t


def _make_native(master_fd: int, log_path: str, proc: _FakeProc) -> server._NativeProc:
    return server._NativeProc(
        proc=proc,
        master_fd=master_fd,
        log_path=log_path,
        reader=_finished_thread(),
        stop=threading.Event(),
    )


def test_start_native_splits_cmd_and_returns_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`flutter run` is launched argv-split (no shell) under a pty; drain starts."""
    scope = _native_scope()
    captured: dict[str, object] = {}
    r_fd, w_fd = os.pipe()

    def fake_openpty():
        return w_fd, r_fd  # (master, slave); slave gets closed by _start_native

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return _FakeProc()

    monkeypatch.setattr(server.pty, "openpty", fake_openpty)
    monkeypatch.setattr(server.subprocess, "Popen", fake_popen)
    # Keep the drain thread inert so the test owns the fds.
    monkeypatch.setattr(server, "_drain_pty", lambda *a, **k: None)

    native = server._start_native(scope, "/proj")
    assert native is not None
    # argv-split, NOT a shell string — so flutter is the pty session leader.
    assert captured["argv"] == ["flutter", "run", "-d", "UDID"]
    assert captured["kwargs"]["stdin"] == r_fd
    assert captured["kwargs"]["start_new_session"] is True
    assert Path(native.log_path).exists()
    server._cleanup_native_log(native.log_path)
    for fd in (w_fd,):
        try:
            os.close(fd)
        except OSError:
            pass


def test_start_native_returns_none_without_serve() -> None:
    """No serve command -> no native session."""
    scope = AgyUiScope(platform="ios-sim", serve=None)
    assert server._start_native(scope, "/proj") is None


def test_hot_reload_native_sends_r_and_confirms(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Writes 'r' to the pty and returns True once a reload marker appears."""
    monkeypatch.setattr(server.time, "sleep", lambda *_: None)
    # Force mark=0 so the pre-written marker counts as fresh post-'r' output.
    monkeypatch.setattr(server.os.path, "getsize", lambda _p: 0)
    log = tmp_path / "n.log"
    log.write_text("Reloaded 1 of 754 libraries in 151ms.\n")
    r_fd, w_fd = os.pipe()
    native = _make_native(w_fd, str(log), _FakeProc(poll_value=None))

    assert server._hot_reload_native(native, timeout=5) is True
    assert os.read(r_fd, 8) == b"r"
    os.close(r_fd)
    os.close(w_fd)


def test_hot_reload_native_false_when_process_dead() -> None:
    """A dead flutter process -> no reload attempt, returns False."""
    r_fd, w_fd = os.pipe()
    native = _make_native(w_fd, "/nonexistent.log", _FakeProc(poll_value=0))
    assert server._hot_reload_native(native) is False
    os.close(r_fd)
    os.close(w_fd)


def test_hot_reload_native_none_session_returns_false() -> None:
    """No native session (web path) -> False without raising."""
    assert server._hot_reload_native(None) is False


def test_hot_restart_native_sends_R_and_confirms(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Writes 'R' to the pty and returns True once a restart marker appears."""
    monkeypatch.setattr(server.time, "sleep", lambda *_: None)
    monkeypatch.setattr(server.os.path, "getsize", lambda _p: 0)
    log = tmp_path / "n.log"
    log.write_text("Restarted application in 1,234ms.\n")
    r_fd, w_fd = os.pipe()
    native = _make_native(w_fd, str(log), _FakeProc(poll_value=None))

    assert server._hot_restart_native(native, timeout=5) is True
    assert os.read(r_fd, 8) == b"R"  # capital R = hot restart
    os.close(r_fd)
    os.close(w_fd)


def test_hot_restart_native_false_when_process_dead() -> None:
    """A dead flutter process -> no restart attempt, returns False."""
    r_fd, w_fd = os.pipe()
    native = _make_native(w_fd, "/nonexistent.log", _FakeProc(poll_value=0))
    assert server._hot_restart_native(native) is False
    os.close(r_fd)
    os.close(w_fd)


def test_hot_restart_native_none_session_returns_false() -> None:
    """No native session (web path) -> False without raising."""
    assert server._hot_restart_native(None) is False


def test_hot_reload_ignores_done_marker_before_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A stale 'Reloaded' BEFORE this command's 'Performing hot reload' is ignored."""
    monkeypatch.setattr(server.time, "sleep", lambda *_: None)
    monkeypatch.setattr(server.os.path, "getsize", lambda _p: 0)
    monkeypatch.setattr(server.time, "monotonic", _ticks([0.0, 0.5, 5.0]))
    log = tmp_path / "n.log"
    # Prior reload's completion, then THIS command's start with no new completion.
    log.write_text("Reloaded 1 of 5 libraries in 9ms.\nPerforming hot reload...\n")
    r_fd, w_fd = os.pipe()
    native = _make_native(w_fd, str(log), _FakeProc(poll_value=None))

    assert server._hot_reload_native(native, timeout=1) is False
    assert os.read(r_fd, 8) == b"r"
    os.close(r_fd)
    os.close(w_fd)


def test_hot_reload_accepts_done_marker_after_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A 'Reloaded' AFTER 'Performing hot reload' is the real confirmation."""
    monkeypatch.setattr(server.time, "sleep", lambda *_: None)
    monkeypatch.setattr(server.os.path, "getsize", lambda _p: 0)
    monkeypatch.setattr(server.time, "monotonic", _ticks([0.0, 0.5]))
    log = tmp_path / "n.log"
    log.write_text("Performing hot reload...\nReloaded 1 of 5 libraries in 9ms.\n")
    r_fd, w_fd = os.pipe()
    native = _make_native(w_fd, str(log), _FakeProc(poll_value=None))

    assert server._hot_reload_native(native, timeout=2) is True
    os.close(r_fd)
    os.close(w_fd)


def test_stop_native_graceful_quit_writes_q_and_closes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Graceful stop sends 'q', joins the drain thread, and closes the master fd."""
    r_fd, w_fd = os.pipe()
    proc = _FakeProc(poll_value=None, wait_raises=False)
    native = _make_native(w_fd, str(tmp_path / "n.log"), proc)

    server._stop_native(native)

    assert os.read(r_fd, 8) == b"q"  # graceful quit key
    assert proc.wait_calls == 1  # exited on the graceful wait; no kill escalation
    assert native.stop.is_set()
    with pytest.raises(OSError):  # master fd was closed
        os.close(w_fd)
    os.close(r_fd)


def test_stop_native_tolerates_none() -> None:
    """Stopping a None session (web path) is a no-op."""
    server._stop_native(None)


def test_stop_native_skips_signals_when_already_exited(tmp_path: Path) -> None:
    """An already-exited process is not signalled; fd still closed, thread joined."""
    r_fd, w_fd = os.pipe()
    proc = _FakeProc(poll_value=0)
    native = _make_native(w_fd, str(tmp_path / "n.log"), proc)

    server._stop_native(native)

    assert proc.wait_calls == 0  # poll() != None -> no wait/kill
    assert native.stop.is_set()
    with pytest.raises(OSError):
        os.close(w_fd)
    os.close(r_fd)


# --- _cleanup_native_log ----------------------------------------------------


def test_cleanup_native_log_removes_file(tmp_path: Path) -> None:
    """The helper deletes a real log file."""
    log = tmp_path / "native.log"
    log.write_text("output")
    server._cleanup_native_log(str(log))
    assert not log.exists()


def test_cleanup_native_log_tolerates_none_and_missing(tmp_path: Path) -> None:
    """None and an already-gone path are both no-ops (no exception)."""
    server._cleanup_native_log(None)
    server._cleanup_native_log(str(tmp_path / "does-not-exist.log"))


# --- native platform detection / in-place handle ----------------------------


def test_ios_sim_is_in_native_platforms() -> None:
    """The orchestrator's native branch keys off NATIVE_PLATFORMS membership."""
    assert "ios-sim" in server.NATIVE_PLATFORMS
    assert "android-emu" in server.NATIVE_PLATFORMS
    # Web stays out of the native branch -> keeps the worktree path.
    assert "web" not in server.NATIVE_PLATFORMS
    assert "flutter-web" not in server.NATIVE_PLATFORMS


# --- in-place safety gate wiring (issues #1/#2) -----------------------------

_NATIVE_SCOPE_YAML = (
    "platform: ios-sim\n"
    "serve: { cmd: 'flutter run -d UDID', url: '', ready_timeout: 60 }\n"
    "allow: ['lib/main.dart']\n"
    "devices: { sim: { udid: 'UDID-X' } }\n"
    "targets: [{ name: home, device: sim }]\n"
    "model: 'gemini-3.5-flash'\n"
)


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_ui_implement_native_blocks_non_git_project(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ui_implement returns a structured 'blocked' result (not a raise) for non-git."""
    monkeypatch.setattr(server, "DRY_RUN", False)
    (tmp_path / ".agy-ui-scope").write_text(_NATIVE_SCOPE_YAML)
    res = server.ui_implement(str(tmp_path), "make the header blue")
    assert res["status"] == "blocked"
    assert "not a git repository" in res["blocked_reason"]
    assert res["files_changed"] == [] and res["applied"] is False


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_ui_review_native_blocks_non_git_project(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ui_review returns a structured 'blocked' result (not a raise) for non-git."""
    monkeypatch.setattr(server, "DRY_RUN", False)
    (tmp_path / ".agy-ui-scope").write_text(_NATIVE_SCOPE_YAML)
    res = server.ui_review(str(tmp_path))
    assert res["status"] == "blocked"
    assert "not a git repository" in res["blocked_reason"]
