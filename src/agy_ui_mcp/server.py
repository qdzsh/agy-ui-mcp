"""MCP server definition and tool orchestration for agy-ui-mcp.

Exposes two tools to MCP clients (Claude Code / Codex):

* ``ui_implement`` — drive a vision loop: screenshot the app, prompt the ``agy``
  CLI to edit CSS/components toward the design, diff-gate the result, and repeat
  until convergence or ``max_iters``.
* ``ui_review`` — screenshot the app and have agy critique it read-only (any
  edit it makes is reverted by the diff-gate).

The orchestrator (this module) holds the loop; agy is a single-shot
``agy -p`` worker. Scope is enforced *after* each turn by the diff-gate rather
than inside agy.

Nothing here runs at import time. The heavy side effects (spawning agy, launching
Playwright, running git, starting a dev server) live inside the tool functions
and are additionally guarded by :data:`DRY_RUN` so the module is import- and
compile-safe with no agy/playwright/git available.
"""

from __future__ import annotations

import hashlib
import os
import pty
import select
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

import yaml

from . import agy_runner, screenshot, scope as scope_mod, worktree
from .policy_builder import apply_diff_gate
from .screenshot import NATIVE_PLATFORMS
from .scope import (
    SCOPE_FILENAME,
    AgyUiScope,
    Device,
    Target,
    load_scope,
    resolve_targets,
)

#: When truthy (env ``AGY_UI_DRY_RUN``), tools skip all external side effects and
#: return a stub payload. Lets the server be exercised without agy/playwright.
DRY_RUN: bool = bool(os.environ.get("AGY_UI_DRY_RUN"))

#: The shared FastMCP server instance. ``__main__`` runs this.
mcp = FastMCP("agy-ui-mcp")


def _load_scope_or_error(project_dir: str) -> AgyUiScope:
    """Load scope, converting filesystem errors into clear messages."""
    try:
        return load_scope(project_dir)
    except (FileNotFoundError, ValueError) as exc:
        raise ValueError(str(exc)) from exc


def _load_or_synthesize_scope(
    project_dir: str,
) -> tuple[AgyUiScope, list[str]]:
    """Load ``.agy-ui-scope`` if present, else auto-detect a zero-config scope.

    Zero-config support for non-technical users: when no scope file exists we
    synthesize one from the project's manifests (see
    :func:`scope.synthesize_scope`) and return the warnings explaining the guess.
    A malformed *existing* file still raises ``ValueError`` (re-raised as today)
    so the user sees the parse error rather than a silently-guessed scope.

    Returns:
        A ``(scope, warnings)`` tuple. ``warnings`` is empty when a real scope
        file was loaded, non-empty when a zero-config scope was synthesized.
    """
    try:
        return load_scope(project_dir), []
    except FileNotFoundError:
        return scope_mod.synthesize_scope(project_dir)
    except ValueError as exc:  # malformed existing file -> surface as today
        raise ValueError(str(exc)) from exc


def _preflight(scope: AgyUiScope, *, web: bool) -> str | None:
    """Return a friendly 'why this can't run yet' message, or None when ready.

    Catches the two setup gaps non-technical users hit before any external call
    is made, so the tool returns a clear ``blocked`` result instead of a raw
    crash deep in agy/Playwright:

    * The ``agy`` CLI is not on PATH (not installed / not logged in).
    * Web captures but the ``playwright`` Python package is not importable.

    Skipped entirely under :data:`DRY_RUN` (callers guard on DRY_RUN before
    invoking, but this is defensive).

    Args:
        scope: The loaded/synthesized scope (unused today; reserved for future
            per-platform checks and to keep the call site uniform).
        web: True when the platform is captured over HTTP with Playwright.

    Returns:
        A human-readable reason string, or None if all preflight checks pass.
    """
    if DRY_RUN:
        return None
    if shutil.which("agy") is None:
        return (
            "The `agy` CLI is not on your PATH. agy-ui-mcp delegates the actual "
            "UI edits to Antigravity's `agy` CLI: install it and log in (it uses "
            "your Antigravity subscription auth), then retry. See the "
            "Requirements section of the README for setup."
        )
    if web:
        try:
            import playwright  # noqa: F401 - import is the availability probe
        except ImportError:
            return (
                "The `playwright` Python package is required to screenshot web "
                "apps but is not installed. Install it with `pip install "
                "playwright` and download the browser with `playwright install "
                "chromium`, then retry."
            )
    return None


def _blocked_implement(reason: str) -> dict[str, Any]:
    """A structured 'blocked' ui_implement result (not an exception).

    Returned instead of raising when a run can't be made safe (a non-git or
    HEAD-less in-place project). ``status: "blocked"`` + the actionable
    ``blocked_reason`` let the calling agent relay a clear message and offer to
    fix it (e.g. ``git init``), rather than the user seeing a raw tool error.
    """
    return {
        "status": "blocked",
        "blocked_reason": reason,
        "files_changed": [],
        "diff": "",
        "escalations": [],
        "iterations": 0,
        "shots_before": [],
        "shots_after": [],
        "targets": [],
        "applied": False,
        "applied_files": [],
        "match_score": None,
        "match_gaps": "",
        "warnings": [reason],
    }


def _blocked_review(reason: str) -> dict[str, Any]:
    """A structured 'blocked' ui_review result (see :func:`_blocked_implement`)."""
    return {
        "status": "blocked",
        "blocked_reason": reason,
        "critique": "",
        "shots": [],
        "targets": [],
        "a11y": {},
        "warnings": [reason],
    }


def _allow_hint(scope: AgyUiScope) -> str:
    """Human-readable summary of the allow globs for the prompt."""
    return ", ".join(scope.allow) if scope.allow else "(nothing configured)"


def _deny_hint(scope: AgyUiScope) -> str:
    """Human-readable summary of the deny globs for the prompt."""
    return ", ".join(scope.deny) if scope.deny else "(nothing configured)"


def _target_label(target: Target, scope: AgyUiScope) -> str:
    """Human-readable label for a target, enriching the name with key context.

    Combines the target's name with its device/route/theme so the per-target
    section of the prompt is self-describing (e.g. ``"mobile / iPhone 13 /
    dark"``).
    """
    parts: list[str] = [target.name or "target"]
    if target.device:
        device = scope.devices.get(target.device)
        device_name = device.name if device and device.name else target.device
        parts.append(str(device_name))
    elif target.viewport_width:
        parts.append(f"{target.viewport_width}px")
    if target.route:
        parts.append(target.route)
    if target.theme:
        parts.append(target.theme)
    return " / ".join(parts)


def _effective_targets(scope: AgyUiScope, target_route: str | None) -> list[Target]:
    """Resolve targets, back-filling an empty ``route`` with ``target_route``.

    The ``target_route`` tool parameter is a legacy single-route knob. When a
    target carries no route of its own and a ``target_route`` was supplied, the
    target inherits it; targets with their own route are left untouched.
    """
    targets = resolve_targets(scope)
    if not target_route:
        return targets
    route = target_route.strip()
    if not route:
        return targets
    return [
        t if t.route else t.model_copy(update={"route": route})
        for t in targets
    ]


def _start_dev_server(
    scope: AgyUiScope, cwd: str
) -> tuple[subprocess.Popen[bytes] | None, str | None]:
    """Start the configured dev server in ``cwd`` and return ``(proc, log_path)``.

    Returns ``(None, None)`` when no serve command is configured. The process is
    started in its own process group so the whole tree can be signalled on
    shutdown.

    For NATIVE platforms (``scope.platform in NATIVE_PLATFORMS``) the child's
    stdout+stderr are redirected into a temporary log file so the readiness gate
    (:func:`_wait_native_ready`) can scan the ``flutter run`` output for an
    app-launched marker — frame-stability alone false-positives on a static home
    screen while the app is still relaunching/building. ``log_path`` is the path
    to that file. Web platforms keep ``DEVNULL`` and return ``log_path=None``.
    """
    if scope.serve is None:
        return None, None

    is_native = scope.platform in NATIVE_PLATFORMS
    if is_native:
        # Capture flutter run output so readiness can detect the app launch.
        log_file = tempfile.NamedTemporaryFile(
            prefix="agy-ui-native-log-", suffix=".log", delete=False
        )
        log_path = log_file.name
        try:
            proc = subprocess.Popen(
                scope.serve.cmd,
                shell=True,
                cwd=cwd,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        finally:
            # The child inherited the fd; the parent no longer needs its handle.
            log_file.close()
        return proc, log_path

    proc = subprocess.Popen(
        scope.serve.cmd,
        shell=True,
        cwd=cwd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return proc, None


def _run_reload(cmd: str, cwd: str) -> None:
    """Run the configured reload/rebuild command in ``cwd`` (best-effort).

    Used for non-HMR frameworks (e.g. Flutter web) where a static server serves
    a build directory and the app must be rebuilt after each edit before the
    next screenshot reflects the change. HMR dev servers (Vite) leave
    ``reload_cmd`` unset and never reach this code.

    Errors are swallowed: a non-zero exit, a timeout, or a missing shell are all
    treated as non-fatal so a flaky rebuild never aborts the vision loop. The
    call blocks until the rebuild finishes (or the timeout fires) so the
    subsequent screenshot sees fresh output.
    """
    try:
        subprocess.run(
            cmd,
            shell=True,
            cwd=cwd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=300,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):  # pragma: no cover - defensive
        # Best-effort: a failed/slow rebuild must not break the loop.
        return


def _stop_dev_server(proc: subprocess.Popen[bytes] | None) -> None:
    """Terminate the dev server process group (best-effort)."""
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:  # pragma: no cover - defensive
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except OSError:
            proc.kill()


def _cleanup_native_log(log_path: str | None) -> None:
    """Delete the temporary native log file (best-effort)."""
    if not log_path:
        return
    try:
        os.unlink(log_path)
    except OSError:  # pragma: no cover - already gone / permission; non-fatal
        return


@dataclass
class _NativeProc:
    """A long-lived native ``flutter run`` process driven through a pty.

    Native frameworks (Flutter on ios-sim) have no HTTP dev server and no
    headless hot-reload signal we can trust from outside. The reliable trigger is
    Flutter's own interactive ``r`` key command — but that is only enabled when
    ``flutter run`` sees a real terminal on stdin. So we launch it attached to a
    pty slave and keep the master fd: writing ``b"r"`` to the master performs a
    hot reload (Flutter handles ``reloadSources`` + ``reassemble`` internally),
    and writing ``b"q"`` quits gracefully so Flutter releases its startup
    lockfile cleanly. This avoids the relaunch-per-iteration that previously left
    stale ``dart``/``frontend_server`` children holding the cache lockfile and
    hanging the next launch.

    A daemon thread drains the pty master into ``log_path`` so the existing
    log-marker readiness gate (:func:`_wait_native_ready`) and the reload
    confirmation check can scan it without the pty buffer filling up and blocking
    Flutter.

    Attributes:
        proc: The ``flutter run`` subprocess (the pty slave is its controlling tty).
        master_fd: The pty master file descriptor; write key commands here.
        log_path: File the drain thread appends Flutter's output to.
        reader: The daemon thread draining ``master_fd`` into ``log_path``.
        stop: Event that tells ``reader`` to exit.
    """

    proc: subprocess.Popen[bytes]
    master_fd: int
    log_path: str
    reader: threading.Thread
    stop: threading.Event


def _drain_pty(master_fd: int, log_path: str, stop: threading.Event) -> None:
    """Append everything readable from ``master_fd`` to ``log_path`` until ``stop``.

    Runs on a daemon thread. Reads must keep draining or the pty's kernel buffer
    fills and ``flutter run`` blocks on its next write. All fd errors end the
    loop quietly (the process is shutting down). The log file is opened
    unbuffered so readiness/reload checks see markers promptly.
    """
    try:
        out = open(log_path, "ab", buffering=0)  # noqa: SIM115 - closed below
    except OSError:  # pragma: no cover - log path vanished; nothing to drain into
        return
    try:
        while not stop.is_set():
            try:
                ready, _, _ = select.select([master_fd], [], [], 0.5)
            except (OSError, ValueError):  # fd closed under us
                break
            if not ready:
                continue
            try:
                data = os.read(master_fd, 65536)
            except OSError:
                break
            if not data:  # EOF: flutter exited
                break
            try:
                out.write(data)
            except OSError:  # pragma: no cover - log unwritable; stop draining
                break
    finally:
        try:
            out.close()
        except OSError:  # pragma: no cover - already closed
            pass


def _start_native(scope: AgyUiScope, cwd: str) -> _NativeProc | None:
    """Launch the native ``serve.cmd`` under a pty and start draining its output.

    Returns None when no serve command is configured. The command is split with
    :func:`shlex.split` (not run through a shell) so ``flutter run`` is the direct
    pty session leader and receives the ``r``/``q`` key commands we write to the
    master — a wrapping shell could swallow them. See :class:`_NativeProc`.
    """
    if scope.serve is None:
        return None
    log_file = tempfile.NamedTemporaryFile(
        prefix="agy-ui-native-log-", suffix=".log", delete=False
    )
    log_path = log_file.name
    log_file.close()
    # Open the pty + launch inside a guard so a failure (bad serve.cmd / missing
    # flutter / shlex error) leaks neither the pty master fd nor the temp log.
    master_fd: int | None = None
    slave_fd: int | None = None
    try:
        master_fd, slave_fd = pty.openpty()
        proc = subprocess.Popen(
            shlex.split(scope.serve.cmd),
            cwd=cwd,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            start_new_session=True,
        )
    except BaseException:
        for fd in (slave_fd, master_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        try:
            os.unlink(log_path)
        except OSError:
            pass
        raise
    os.close(slave_fd)  # the child holds its own copy
    stop = threading.Event()
    reader = threading.Thread(
        target=_drain_pty, args=(master_fd, log_path, stop), daemon=True
    )
    reader.start()
    return _NativeProc(
        proc=proc, master_fd=master_fd, log_path=log_path, reader=reader, stop=stop
    )


#: Flutter prints a START line the instant it consumes the key (before the work),
#: then a DONE line when it completes. We require the DONE marker to appear AFTER
#: a START marker so we can never latch onto a *previous* command's completion
#: line that happened to be flushed in the race window around the keystroke.
_NATIVE_RELOAD_START: tuple[str, ...] = ("performing hot reload",)
#: Substrings confirming a hot reload COMPLETED (e.g. "Reloaded 1 of 754 ...").
_NATIVE_RELOAD_MARKERS: tuple[str, ...] = ("reloaded ",)

_NATIVE_RESTART_START: tuple[str, ...] = ("performing hot restart",)
#: Substrings confirming a hot RESTART COMPLETED (e.g. "Restarted application ...").
_NATIVE_RESTART_MARKERS: tuple[str, ...] = ("restarted application", "restarted in")


def _native_key_command(
    native: _NativeProc | None,
    key: bytes,
    done_markers: tuple[str, ...],
    timeout: int,
    start_markers: tuple[str, ...] = (),
) -> bool:
    """Send a single Flutter interactive ``key`` over the pty; await confirmation.

    Shared core for hot reload (``r``) and hot restart (``R``). Snapshots the log
    size *before* writing so only output produced *after* the keystroke is
    scanned — the startup banner already contains the "hot reload"/"hot restart"
    help text, which would otherwise false-positive.

    To avoid latching onto a *prior* command's completion line that may be flushed
    in the tiny race window between the size snapshot and the write, when
    ``start_markers`` is given we only accept a ``done_markers`` match that occurs
    AFTER the last start marker in the new output (Flutter prints "Performing hot
    reload/restart..." the moment it consumes the key). If no start marker is seen
    (e.g. a future Flutter changes its wording), we fall back to matching the done
    marker anywhere in the new output so we never produce a false negative.

    Returns True once a (post-start) done marker appears (then settles a frame),
    False on timeout or a dead process (the caller still re-screenshots; a stale
    frame self-corrects).
    """
    if native is None or native.proc.poll() is not None:
        return False
    try:
        mark = os.path.getsize(native.log_path)
    except OSError:
        mark = 0
    try:
        os.write(native.master_fd, key)
    except OSError:  # pragma: no cover - pty gone; treat as failed command
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with open(native.log_path, "rb") as fh:
                fh.seek(mark)
                tail = fh.read().decode("utf-8", "replace").lower()
        except OSError:  # pragma: no cover - log locked/rotated; keep polling
            tail = ""
        # Only search for completion AFTER this command's start line (when known),
        # so a prior command's done line cannot be mistaken for this one's.
        region = tail
        start_at = max((tail.rfind(s) for s in start_markers), default=-1)
        if start_at != -1:
            region = tail[start_at:]
        if any(m in region for m in done_markers):
            time.sleep(_NATIVE_FIRST_FRAME_SETTLE_S)
            return True
        time.sleep(1)
    # No explicit confirmation: still settle so the next screenshot isn't mid-paint.
    time.sleep(_NATIVE_FIRST_FRAME_SETTLE_S)
    return False


def _hot_reload_native(native: _NativeProc | None, timeout: int = 60) -> bool:
    """Hot-reload a running native app by sending ``r`` to its pty; await confirm.

    Returns True once Flutter prints a reload-complete marker, False on timeout
    or a dead process. See :func:`_native_key_command`.
    """
    return _native_key_command(
        native, b"r", _NATIVE_RELOAD_MARKERS, timeout, _NATIVE_RELOAD_START
    )


def _hot_restart_native(native: _NativeProc | None, timeout: int = 90) -> bool:
    """Hot-restart a running native app by sending ``R`` to its pty; await confirm.

    The fallback when a kept edit shows no visual change after a hot reload —
    some Dart changes (``main()``, app/widget state, ``const`` fields) only take
    effect on a full restart, which re-runs ``main()`` and rebuilds from scratch.
    Allows a longer ``timeout`` than reload (a restart rebuilds more). Returns
    True once Flutter prints a restart-complete marker. See
    :func:`_native_key_command`.
    """
    return _native_key_command(
        native, b"R", _NATIVE_RESTART_MARKERS, timeout, _NATIVE_RESTART_START
    )


def _stop_native(native: _NativeProc | None) -> None:
    """Quit the native app gracefully (``q``) so Flutter releases its lockfile.

    A graceful quit is what fixes the stale-lock bug: SIGKILL/SIGTERM left
    ``dart``/``frontend_server`` children holding Flutter's cache lockfile, which
    hung the next ``flutter run``. We escalate to the process group only if the
    graceful quit does not exit in time. Always stops the drain thread and closes
    the master fd.
    """
    if native is None:
        return
    proc = native.proc
    if proc.poll() is None:
        try:
            os.write(native.master_fd, b"q")
        except OSError:  # pragma: no cover - pty already gone
            pass
        try:
            proc.wait(timeout=12)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except OSError:  # pragma: no cover - already reaped
                pass
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except OSError:
                    pass
    native.stop.set()
    native.reader.join(timeout=3)
    try:
        os.close(native.master_fd)
    except OSError:  # pragma: no cover - already closed
        pass


#: Case-insensitive substrings emitted by ``flutter run`` once the app has
#: actually launched on the device (as opposed to the simulator merely sitting on
#: its home screen). Seeing ANY of these in the captured log proves the app is up,
#: which frame-stability alone cannot (a static home screen is also "stable").
_NATIVE_LAUNCH_MARKERS: tuple[str, ...] = (
    "syncing files to device",
    "flutter run key commands",
    "dart vm service",
    "is available at",
    "the flutter devtools",
)

#: Seconds to wait after a launch marker appears so the first frame can render
#: before the caller screenshots.
_NATIVE_FIRST_FRAME_SETTLE_S: float = 6.0

#: Minimum seconds the frame-stable FALLBACK must wait before trusting that two
#: identical frames mean "ready" — guards against latching onto a static home
#: screen captured before the app has even started relaunching.
_NATIVE_FALLBACK_MIN_WAIT_S: float = 20.0


def _wait_native_ready(
    scope: AgyUiScope,
    target: Target,
    devices: dict[str, Device],
    log_path: str | None,
    timeout: int = 180,
) -> bool:
    """Wait until the native app has launched (or time out).

    Native apps launched by ``serve.cmd`` (``flutter run -d <udid>``) expose no
    HTTP endpoint, so :func:`screenshot.wait_ready` cannot be used. Two
    strategies, in order of preference:

    * **Log-based (preferred):** when ``log_path`` points at a readable
      ``flutter run`` log, poll it every ~2s for any
      :data:`_NATIVE_LAUNCH_MARKERS` substring (case-insensitive). A marker
      proves the app actually launched — unlike frame-stability, which
      false-positives on a static simulator home screen while the app is still
      relaunching/building. Once a marker is seen, sleep
      :data:`_NATIVE_FIRST_FRAME_SETTLE_S` for the first paint, then return True.
      If the timeout elapses with no marker, return False.

    * **Frame-stable (fallback):** when ``log_path`` is None (no log available),
      fall back to comparing consecutive simulator screenshots by SHA-256, but
      require at least three captures AND a minimum elapsed wait of
      :data:`_NATIVE_FALLBACK_MIN_WAIT_S` before trusting that two identical
      frames mean "ready" (reducing the chance of latching onto a home screen).

    All file-read and capture errors are swallowed and the loop keeps polling —
    they are expected while the simulator is still booting. ``time.sleep`` is
    acceptable here (this is a blocking readiness gate, not the hot capture loop).

    Args:
        scope: The loaded scope (provides ``platform``).
        target: A capture target identifying the device/simulator to poll.
        devices: The scope's named device registry.
        log_path: Path to the ``flutter run`` log, or None to use the fallback.
        timeout: Maximum seconds to wait for the app to launch.

    Returns:
        True once the app is detected as launched; False on timeout.
    """
    start = time.monotonic()
    deadline = start + timeout

    # Preferred path: scan the flutter run log for an app-launched marker.
    if log_path and os.path.exists(log_path):
        while time.monotonic() < deadline:
            try:
                with open(log_path, "rb") as fh:
                    text = fh.read().decode("utf-8", "replace").lower()
            except OSError:  # pragma: no cover - log vanished/locked; keep polling
                text = ""
            if any(marker in text for marker in _NATIVE_LAUNCH_MARKERS):
                # App launched: give the first frame a moment to render.
                time.sleep(_NATIVE_FIRST_FRAME_SETTLE_S)
                return True
            time.sleep(2)
        return False

    # Fallback path: no log to read — compare consecutive screenshots, but only
    # trust stability after >=3 frames and a minimum elapsed wait.
    prev_hash: str | None = None
    frames = 0
    with tempfile.TemporaryDirectory(prefix="agy-ui-native-ready-") as tmp:
        probe = Path(tmp) / "probe.png"
        while time.monotonic() < deadline:
            try:
                screenshot.capture_for_platform(
                    scope.platform, "", target, devices, probe
                )
                cur_hash = hashlib.sha256(probe.read_bytes()).hexdigest()
            except Exception:  # noqa: BLE001 - app not ready yet; keep polling
                cur_hash = None
            if cur_hash is not None:
                frames += 1
                elapsed = time.monotonic() - start
                if (
                    prev_hash is not None
                    and cur_hash == prev_hash
                    and frames >= 3
                    and elapsed >= _NATIVE_FALLBACK_MIN_WAIT_S
                ):
                    return True
                prev_hash = cur_hash
            time.sleep(4)
    return False


def _apply_changes(
    worktree_path: str, project_dir: str, rel_files: list[str]
) -> list[str]:
    """Copy in-scope changed files from the worktree back into the project.

    For each repo-relative path, copy ``worktree_path/rel`` over
    ``project_dir/rel`` (creating parent directories as needed) so the user can
    inspect the changes with ``git diff`` in their real working tree. Sources
    that no longer exist (e.g. reverted/cleaned by the diff-gate) are skipped.

    Args:
        worktree_path: The isolated worktree the edits were made in.
        project_dir: The original project root to write the changes into.
        rel_files: Repo-relative paths to copy.

    Returns:
        The repo-relative paths that were successfully copied.
    """
    wt_root = Path(worktree_path)
    proj_root = Path(project_dir)
    applied: list[str] = []
    for rel in rel_files:
        src = wt_root / rel
        if not src.exists():
            # Diff-gate may have reverted/cleaned it; nothing to copy.
            continue
        dst = proj_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        applied.append(rel)
    return applied


def _dirty_paths(project_dir: str) -> set[str]:
    """Return repo-relative paths with uncommitted changes in ``project_dir``.

    Best-effort: parses ``git status --porcelain``. Returns an empty set when
    git is unavailable or the directory is not a repo.
    """
    try:
        result = subprocess.run(
            ["git", "-C", project_dir, "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:  # pragma: no cover - git missing
        return set()
    if result.returncode != 0:
        return set()
    dirty: set[str] = set()
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        # Porcelain v1: 2 status chars + space, then the path (or "old -> new").
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        dirty.add(path.strip('"'))
    return dirty


def _shots_identical(a: list[str], b: list[str]) -> bool:
    """Return True if two screenshot lists are byte-identical, pairwise.

    Used as a convergence signal: if a round leaves the rendered UI unchanged,
    the edits had no visual effect and the loop can stop. Any read error or a
    length mismatch returns False (treat as "changed / cannot prove equal").

    Args:
        a: First list of file paths.
        b: Second list of file paths.

    Returns:
        True only if both lists are the same length and every corresponding
        pair of files hashes identically.
    """
    if len(a) != len(b):
        return False
    try:
        for pa, pb in zip(a, b):
            ha = hashlib.sha256(Path(pa).read_bytes()).hexdigest()
            hb = hashlib.sha256(Path(pb).read_bytes()).hexdigest()
            if ha != hb:
                return False
    except OSError:
        return False
    return True


@mcp.tool()
def ui_implement(
    project_dir: str,
    task: str,
    design_refs: list[str] | None = None,
    target_route: str | None = None,
    max_iters: int = 4,
    apply: bool = True,
    match_threshold: int = 90,
) -> dict[str, Any]:
    """Implement/modify UI for ``task`` via an iterative, diff-gated vision loop.

    Workflow:
        1. Load ``.agy-ui-scope`` and create an isolated git worktree.
        2. Start the dev server (from ``scope.serve``) inside the worktree and
           wait for it to be ready.
        3. Loop up to ``max_iters``: screenshot the route across viewports,
           build a vision prompt listing the shot + design-ref paths, run
           ``agy -p`` in the worktree, diff-gate the result, re-screenshot.
           Stop early once a round produces no new in-scope changes.
        4. Collect the final diff, stop the server, clean up the worktree.

    Args:
        project_dir: Absolute path to the project root (must contain the scope).
        task: Natural-language description of the UI change.
        design_refs: Optional absolute paths to design reference images.
        target_route: Optional route appended to ``scope.serve.url`` (e.g.
            ``"/login"``); defaults to the base URL.
        max_iters: Maximum vision-loop iterations.
        apply: When True (default), copy the in-scope changed files from the
            worktree back into ``project_dir`` so the user can ``git diff`` and
            review/commit them. When False, the worktree is collected and torn
            down without writing anything to the project.
        match_threshold: Only used when ``design_refs`` is non-empty. agy is
            asked to self-report a 0-100 match score against the mockup each
            round; the loop stops early once that score reaches this threshold.

    Returns:
        A dict with ``files_changed``, ``diff``, ``escalations``, ``iterations``,
        ``shots_before``, ``shots_after``, ``warnings``, ``applied`` (whether
        apply was requested), ``applied_files`` (paths written to the project),
        ``match_score`` (the last self-reported 0-100 score, or ``None`` when no
        design refs were supplied), and ``match_gaps`` (the last reported
        remaining differences, or ``""``).
    """
    design_refs = list(design_refs or [])
    # Zero-config: load .agy-ui-scope if present, else auto-detect from the
    # project's manifests (runs for both DRY_RUN and the real path). A malformed
    # existing file still raises and surfaces via the existing error path.
    scope, synth_warnings = _load_or_synthesize_scope(project_dir)

    if DRY_RUN:
        return {
            "files_changed": [],
            "diff": "",
            "escalations": [],
            "iterations": 0,
            "shots_before": [],
            "shots_after": [],
            "targets": [],
            "warnings": (
                synth_warnings
                + ["AGY_UI_DRY_RUN set: no agy/playwright/git executed."]
            ),
            "applied": False,
            "applied_files": [],
            "match_score": None,
            "match_gaps": "",
        }

    # Friendly preflight: block with an actionable reason before any external
    # call when the agy CLI / Playwright aren't set up.
    web = scope.platform not in NATIVE_PLATFORMS
    pf = _preflight(scope, web=web)
    if pf:
        return _blocked_implement(pf)

    # Native platforms (ios-sim) build into a simulator: too heavy for a throwaway
    # git worktree, so they run IN-PLACE on the project (reusing the build cache).
    # Web platforms keep the isolated-worktree flow unchanged.
    is_native = scope.platform in NATIVE_PLATFORMS
    # In-place safety: a native run mutates the real project directly (no
    # worktree), so we snapshot its pre-run state into a baseline; the diff-gate
    # and reverts compare against that baseline, so only agy's edits are gated and
    # the user's uncommitted work is preserved. A non-git/HEAD-less project can't
    # be snapshotted -> return a structured 'blocked' result (don't raise).
    try:
        if is_native:
            handle = worktree.prepare_inplace(project_dir)
        else:
            handle = worktree.create_worktree(project_dir)
            worktree.link_dependencies(handle)
    except worktree.InPlaceSafetyError as exc:
        return _blocked_implement(str(exc))
    warnings: list[str] = list(synth_warnings) + list(handle.warnings)
    # Native captures have no HTTP URL (the app runs in a simulator); the web
    # path keeps appending target routes to the configured serve URL.
    base_url = "" if is_native else (scope.serve.url if scope.serve else "")
    targets = _effective_targets(scope, target_route)
    target_names = [t.name or "" for t in targets]

    # Directories agy must be able to read: the shots dir plus the parent of
    # every design reference (global and per-target).
    design_dirs: list[str] = [str(Path(r).parent) for r in design_refs]
    for t in targets:
        if t.design_ref:
            design_dirs.append(str(Path(t.design_ref).parent))

    shots_dir = Path(tempfile.mkdtemp(prefix="agy-ui-shots-"))
    server_proc: subprocess.Popen[bytes] | None = None
    native: _NativeProc | None = None
    native_log: str | None = None

    files_changed: list[str] = []
    escalations: list[str] = []
    shots_before: list[str] = []
    shots_after: list[str] = []
    applied_files: list[str] = []
    iterations = 0
    # Match-driven convergence: active whenever any design reference (global or
    # per-target) is available across the resolved targets.
    use_score = bool(design_refs) or any(t.design_ref for t in targets)
    match_score: int | None = None
    match_gaps: str = ""
    current_gaps: str = ""

    def _capture_all(subdir: str) -> tuple[dict[str, str], list[str]]:
        """Capture every target into ``shots_dir/subdir``; return paths+warnings.

        Returns a ``{target_name: path}`` map (preserving target order) and a
        flat list of capture/pre-step warnings.
        """
        out_dir = shots_dir / subdir
        captured: dict[str, str] = {}
        capture_warnings: list[str] = []
        for t in targets:
            path, warns = screenshot.capture_for_platform(
                scope.platform, base_url, t, scope.devices, out_dir / f"{t.name}.png"
            )
            captured[t.name or ""] = path
            for w in warns:
                capture_warnings.append(f"[{t.name}] {w}")
        return captured, capture_warnings

    reload_cmd = scope.serve.reload_cmd if scope.serve else None

    ready_timeout = scope.serve.ready_timeout if scope.serve else 120

    try:
        if is_native:
            # Native: long-lived `flutter run` under a pty so we can hot-reload
            # via the `r` key command between iterations (no relaunch).
            native = _start_native(scope, handle.path_str)
            native_log = native.log_path if native else None
        else:
            server_proc, native_log = _start_dev_server(scope, handle.path_str)
        # Non-HMR frameworks: build once up front so the static server has
        # content to serve before the first screenshot/readiness check.
        if reload_cmd and not is_native:
            _run_reload(reload_cmd, handle.path_str)
        if is_native:
            # Native: no HTTP url to poll — wait until the flutter run log shows
            # the app actually launched (falling back to frame-stability).
            if targets and not _wait_native_ready(
                scope, targets[0], scope.devices, native_log, ready_timeout
            ):
                warnings.append(
                    f"Native app did not stabilize within {ready_timeout}s; "
                    f"screenshots may be blank or mid-launch."
                )
        elif scope.serve and not screenshot.wait_ready(
            base_url, scope.serve.ready_timeout
        ):
            warnings.append(
                f"Dev server at {base_url} did not become ready within "
                f"{scope.serve.ready_timeout}s; screenshots may be blank."
            )

        for i in range(max_iters):
            iterations = i + 1
            current_map, cap_warns = _capture_all(f"iter{i}")
            warnings.extend(cap_warns)
            current_shots = list(current_map.values())
            if i == 0:
                shots_before = current_shots

            # Build per-target pairs: each target's current shot beside the
            # mockup it must match (its own design_ref, else the global ones).
            shot_pairs: list[dict] = []
            for t in targets:
                design = [t.design_ref] if t.design_ref else list(design_refs)
                shot_pairs.append(
                    {
                        "label": _target_label(t, scope),
                        "current": [current_map[t.name or ""]],
                        "design": design,
                    }
                )

            # Build the notes block: prior escalations, plus the remaining gaps
            # reported by agy in the previous round (only when scoring is on).
            note_parts: list[str] = []
            if escalations:
                note_parts.append(
                    f"Previously escalated (do not retry): {escalations}"
                )
            if use_score and current_gaps:
                note_parts.append(f"Remaining gaps to fix: {current_gaps}")
            prompt = agy_runner.build_vision_prompt(
                task=task,
                shot_pairs=shot_pairs,
                allow_hint=_allow_hint(scope),
                deny_hint=_deny_hint(scope),
                notes="\n".join(note_parts),
                request_score=use_score,
            )
            result = agy_runner.run_agy(
                prompt,
                cwd=handle.path_str,
                model=scope.model,
                continue_=(i > 0),
                add_dirs=[str(shots_dir)] + design_dirs,
            )

            # Parse the self-reported match score/gaps (only meaningful when we
            # asked for them). Score feeds the threshold check below; gaps are
            # carried into next round's notes.
            score: int | None = None
            if use_score:
                score, current_gaps = agy_runner.parse_match(result.text)
                if score is not None:
                    match_score = score
                match_gaps = current_gaps

            gate = apply_diff_gate(
                handle.path_str,
                scope,
                base_ref=handle.base_ref,
                prerun_untracked=handle.prerun_untracked,
            )
            for p in gate.kept:
                if p not in files_changed:
                    files_changed.append(p)
            for p in gate.escalations:
                if p not in escalations:
                    escalations.append(p)

            # Reflect this round's kept edits before the re-screenshot:
            #  * Native: hot-reload the still-running `flutter run` via its `r`
            #    key command (no relaunch -> no stale lockfile). Flutter reloads
            #    from disk, where agy's in-place edit already landed.
            #  * Non-HMR web frameworks: rebuild via reload_cmd.
            #  * HMR web dev servers (reload_cmd None): hot-reload on their own.
            if is_native:
                if gate.kept and not _hot_reload_native(native):
                    warnings.append(
                        f"Hot reload not confirmed at iteration {iterations}; "
                        f"after-screenshot may lag the edit."
                    )
            elif reload_cmd:
                _run_reload(reload_cmd, handle.path_str)

            # Re-screenshot reflects the kept changes. Convergence signals
            # (all evaluated; any one stops the loop):
            #  1. Match score reached the threshold (only when scoring is on).
            #  2. The round kept nothing new (no in-scope edits survived).
            #  3. The UI is byte-identical before/after this round across ALL
            #     targets — keep iterating is futile.
            after_map, after_warns = _capture_all(f"iter{i}-after")
            warnings.extend(after_warns)
            shots_after = list(after_map.values())

            # Hot-restart fallback (native): a kept in-scope edit that left the
            # UI byte-identical after a hot reload may need a full hot restart
            # (e.g. edits to main()/app state/const fields the reload can't
            # patch). Try a restart once and re-capture before treating the
            # unchanged frame as convergence — otherwise a restart-only change
            # would be misread as "no visual change, stop".
            if (
                is_native
                and gate.kept
                and _shots_identical(current_shots, shots_after)
                and _hot_restart_native(native)
            ):
                warnings.append(
                    f"Iteration {iterations}: hot reload showed no visual "
                    f"change; applied a hot restart and re-captured."
                )
                after_map, restart_warns = _capture_all(f"iter{i}-after-restart")
                warnings.extend(restart_warns)
                shots_after = list(after_map.values())

            if use_score and score is not None and score >= match_threshold:
                warnings.append(
                    f"Matched mockup at score {score} (iteration {i + 1}); "
                    f"stopping."
                )
                break
            if not gate.kept:
                break
            if _shots_identical(current_shots, shots_after):
                warnings.append(
                    f"Converged at iteration {iterations}: edits produced no "
                    f"visual change; stopping early."
                )
                break

        diff = worktree.collect_diff(handle)

        # Apply the surviving in-scope changes back to the real working tree so
        # the user can review them via `git diff`. Must run before `finally`
        # removes the worktree.
        if apply and files_changed and not handle.in_place:
            dirty_before = _dirty_paths(str(handle.project_dir))
            for f in files_changed:
                if f in dirty_before:
                    warnings.append(f"overwrote uncommitted changes in {f}")
            applied_files = _apply_changes(
                handle.path_str, str(handle.project_dir), files_changed
            )
        elif apply and handle.in_place:
            # In-place runs (native) already mutated the project directly.
            applied_files = list(files_changed)
        else:
            applied_files = []
            if handle.in_place:
                # Native ran IN-PLACE on the real project, so a no-apply run must
                # actively undo the edits it made — there is no isolated worktree
                # to throw away. (Diff was already collected above.)
                #
                # SCOPED revert: only the files THIS run kept in-scope
                # (`files_changed`). The diff-gate already reverted any
                # deny/ambiguous edits while running, so they need no touching.
                # Using revert_all here would also wipe the user's OTHER
                # uncommitted changes (e.g. an un-committed .agy-ui-scope) — a
                # data-loss bug. Web (worktree) runs hit the branches above and
                # discard the throwaway worktree instead, never touching the
                # real project.
                worktree.revert_paths(handle, files_changed)
    finally:
        if native is not None:
            _stop_native(native)
        else:
            _stop_dev_server(server_proc)
        _cleanup_native_log(native_log)
        worktree.cleanup(handle)

    return {
        "files_changed": files_changed,
        "diff": diff,
        "escalations": escalations,
        "iterations": iterations,
        "shots_before": shots_before,
        "shots_after": shots_after,
        "targets": target_names,
        "warnings": warnings,
        "applied": bool(apply),
        "applied_files": applied_files,
        "match_score": match_score,
        "match_gaps": match_gaps,
    }


@mcp.tool()
def ui_review(
    project_dir: str,
    target_route: str | None = None,
    against_design: list[str] | None = None,
    a11y: bool = True,
) -> dict[str, Any]:
    """Serve the app, screenshot it, and have agy critique it read-only.

    Any file agy edits during the critique is reverted by the diff-gate, so this
    tool never mutates the project.

    When ``a11y`` is set (and the platform is a web target), each target is also
    audited with axe-core; the violations ground agy's critique and are returned
    structurally under ``a11y``. Native platforms have no DOM, so the audit is
    skipped there with a note.

    Args:
        project_dir: Absolute path to the project root (must contain the scope).
        target_route: Optional route appended to ``scope.serve.url``.
        against_design: Optional absolute paths to design reference images to
            critique against.
        a11y: Run axe-core accessibility checks on web targets (default True).

    Returns:
        A dict with ``critique`` (agy's text), ``shots`` (captured paths),
        ``targets`` (the reviewed target names), ``a11y`` (a ``{target:
        [violations]}`` map; empty when disabled/native), and ``warnings``.
    """
    against_design = list(against_design or [])
    # Zero-config: load .agy-ui-scope if present, else auto-detect (see
    # ui_implement). Runs for both DRY_RUN and the real path.
    scope, synth_warnings = _load_or_synthesize_scope(project_dir)

    if DRY_RUN:
        return {
            "critique": "",
            "shots": [],
            "targets": [],
            "a11y": {},
            "warnings": (
                synth_warnings
                + ["AGY_UI_DRY_RUN set: no agy/playwright/git executed."]
            ),
        }

    # Friendly preflight before any external call (see ui_implement).
    web = scope.platform not in NATIVE_PLATFORMS
    pf = _preflight(scope, web=web)
    if pf:
        return _blocked_review(pf)

    # Native platforms run IN-PLACE (no worktree) and reuse the build cache, just
    # like ui_implement. The review is still read-only: a SCOPED revert against
    # the pre-run baseline undoes any edit agy made, leaving the user's other
    # uncommitted changes intact.
    is_native = scope.platform in NATIVE_PLATFORMS
    # In-place safety: snapshot the project's pre-run state into a baseline (see
    # ui_implement). A non-git/HEAD-less project can't be snapshotted -> return a
    # structured 'blocked' result instead of raising.
    try:
        if is_native:
            handle = worktree.prepare_inplace(project_dir)
        else:
            handle = worktree.create_worktree(project_dir)
            worktree.link_dependencies(handle)
    except worktree.InPlaceSafetyError as exc:
        return _blocked_review(str(exc))
    warnings: list[str] = list(synth_warnings) + list(handle.warnings)
    base_url = "" if is_native else (scope.serve.url if scope.serve else "")
    targets = _effective_targets(scope, target_route)
    target_names = [t.name or "" for t in targets]

    shots_dir = Path(tempfile.mkdtemp(prefix="agy-ui-review-"))
    server_proc: subprocess.Popen[bytes] | None = None
    native: _NativeProc | None = None
    native_log: str | None = None
    critique = ""
    shots: list[str] = []
    # Per-target axe-core violations (web only); empty when a11y is off/native.
    a11y_results: dict[str, list[dict]] = {}
    run_a11y = a11y and not is_native

    reload_cmd = scope.serve.reload_cmd if scope.serve else None
    ready_timeout = scope.serve.ready_timeout if scope.serve else 120

    try:
        if is_native:
            # Native: launch `flutter run` under a pty (single read-only capture,
            # so no reload needed — but the same graceful-quit teardown applies).
            native = _start_native(scope, handle.path_str)
            native_log = native.log_path if native else None
        else:
            server_proc, native_log = _start_dev_server(scope, handle.path_str)
        # Non-HMR frameworks: build once so the static server has content to
        # serve before the readiness check and screenshot.
        if reload_cmd and not is_native:
            _run_reload(reload_cmd, handle.path_str)
        if is_native:
            # Native: no HTTP url — wait for the flutter run log to show the app
            # launched (falling back to frame-stability).
            if targets and not _wait_native_ready(
                scope, targets[0], scope.devices, native_log, ready_timeout
            ):
                warnings.append(
                    f"Native app did not stabilize within {ready_timeout}s; "
                    f"screenshots may be blank or mid-launch."
                )
        elif scope.serve and not screenshot.wait_ready(
            base_url, scope.serve.ready_timeout
        ):
            warnings.append(
                f"Dev server at {base_url} did not become ready within "
                f"{scope.serve.ready_timeout}s."
            )

        # Capture each target and build a per-target section for the prompt.
        target_blocks: list[str] = []
        for t in targets:
            path, warns = screenshot.capture_for_platform(
                scope.platform, base_url, t, scope.devices, shots_dir / f"{t.name}.png"
            )
            shots.append(path)
            for w in warns:
                warnings.append(f"[{t.name}] {w}")
            design = [t.design_ref] if t.design_ref else list(against_design)
            block = [f'Target "{_target_label(t, scope)}":', f"  - {path}"]
            if design:
                block.append("  design reference(s) to compare against:")
                block.extend(f"    - {d}" for d in design)
            if run_a11y:
                # Ground the review in concrete WCAG findings: audit the same
                # DOM that was just screenshotted (non-fatal — warnings only).
                violations, a11y_warns = screenshot.audit_a11y(
                    base_url, t, scope.devices
                )
                a11y_results[t.name or ""] = violations
                for w in a11y_warns:
                    warnings.append(f"[{t.name}] a11y: {w}")
                block.append("  accessibility (axe-core):")
                block.extend(
                    f"    {line}"
                    for line in screenshot.summarize_a11y(violations).splitlines()
                )
            target_blocks.append("\n".join(block))

        design_dirs = [str(Path(r).parent) for r in against_design]
        for t in targets:
            if t.design_ref:
                design_dirs.append(str(Path(t.design_ref).parent))

        a11y_note = (
            " Each block may include axe-core accessibility findings; fold any "
            "real WCAG violations into your prioritized issues (color contrast, "
            "labels/names, roles, focus order)."
            if run_a11y
            else ""
        )
        prompt = (
            "You are a senior frontend reviewer. Each block below is one "
            "capture target (a route, device, theme, or component state) of "
            "the same app. Open each screenshot with your read_file tool and "
            "critique the UI: layout, spacing, typography, color, and "
            "responsive behavior. Where a design reference is given, judge the "
            "target against it." + a11y_note + "\n\n"
            + "\n\n".join(target_blocks)
            + "\n\nThis is a READ-ONLY review: do not edit any files. Report a "
            "prioritized list of concrete issues and suggested fixes, noting "
            "which target each issue applies to."
        )
        result = agy_runner.run_agy(
            prompt,
            cwd=handle.path_str,
            model=scope.model,
            add_dirs=[str(shots_dir)] + design_dirs,
        )
        critique = result.text

        # Read-only guarantee: revert everything agy touched this session
        # (allowed or not), despite the read-only instruction.
        #
        # SCOPED revert: only the paths changed *during* this review session
        # (captured now, after agy ran), never the whole working tree. For an
        # in-place native run that means the user's OTHER uncommitted changes
        # (e.g. an un-committed .agy-ui-scope) survive; revert_all would have
        # destroyed them. We use the same scoped revert for the web worktree so
        # the behavior is uniform.
        changed = worktree.list_changed(handle)
        worktree.revert_paths(handle, changed)
    finally:
        if native is not None:
            _stop_native(native)
        else:
            _stop_dev_server(server_proc)
        _cleanup_native_log(native_log)
        worktree.cleanup(handle)

    return {
        "critique": critique,
        "shots": shots,
        "targets": target_names,
        "a11y": a11y_results,
        "warnings": warnings,
    }


#: Comment header prepended to a generated ``.agy-ui-scope`` so the user knows
#: it was auto-generated and how to extend it.
_SCOPE_HEADER: str = (
    "# .agy-ui-scope — auto-generated by the `ui_init` tool.\n"
    "#\n"
    "# This file was guessed from your project's stack. Edit it freely: tweak\n"
    "# the allow/deny globs, the serve command/url, or add per-screen `targets`\n"
    "# with a `design_ref` pointing at a mockup image, e.g.\n"
    "#\n"
    "#   targets:\n"
    "#     - name: login\n"
    "#       route: /login\n"
    "#       design_ref: ./design/login.png\n"
    "#\n"
    "# See `.agy-ui-scope.example` for the full set of options.\n"
)

#: Candidate design-asset directories `ui_init` probes for (relative to root).
_DESIGN_DIRS: tuple[str, ...] = ("design", "designs", "mockups")


def _scope_to_yaml(scope: AgyUiScope) -> str:
    """Serialize the core of a scope to YAML for a generated ``.agy-ui-scope``.

    Emits ``model``, ``platform``, the ``serve`` block, and the allow/deny/
    ambiguous globs — the fields :func:`scope.synthesize_scope` populates. The
    comment header (added by the caller) covers ``targets``/``devices``.
    """
    data: dict[str, Any] = {
        "model": scope.model,
        "platform": scope.platform,
    }
    if scope.serve is not None:
        data["serve"] = {
            "cmd": scope.serve.cmd,
            "url": scope.serve.url,
            "ready_timeout": scope.serve.ready_timeout,
        }
    data["allow"] = list(scope.allow)
    data["deny"] = list(scope.deny)
    data["ambiguous"] = list(scope.ambiguous)
    return yaml.safe_dump(data, sort_keys=False, default_flow_style=False)


@mcp.tool()
def ui_init(project_dir: str = ".", overwrite: bool = False) -> dict[str, Any]:
    """Auto-detect the stack and write a starter ``.agy-ui-scope`` config.

    Zero-to-config helper for non-technical users: inspects the project's
    manifests (package.json / pubspec.yaml / lockfiles), guesses the framework,
    serve command, and edit-scope globs, and writes a ``.agy-ui-scope`` YAML at
    the project root that the user can then tweak by hand. After this, the
    ``ui_implement`` / ``ui_review`` tools run with no further setup.

    Args:
        project_dir: Path to the project root to scan and write into.
        overwrite: When False (default) an existing ``.agy-ui-scope`` is left
            untouched and ``status: "exists"`` is returned. Pass True to
            regenerate it (clobbering the existing file).

    Returns:
        A dict describing the outcome: ``status`` (``ok``/``exists``/``error``),
        ``scope_path`` (absolute), ``written`` (whether the file was written),
        ``detected`` (framework/platform/serve/package-manager), ``allow`` and
        ``deny`` globs, ``design_dir_found``, ``next_steps``, and ``warnings``.
    """
    root = Path(project_dir).resolve()
    scope_path = root / SCOPE_FILENAME

    framework = scope_mod.detect_framework(root)
    pm = (
        scope_mod._detect_package_manager(root)
        if (root / "package.json").is_file()
        else "none"
    )
    design_dir_found = any((root / d).is_dir() for d in _DESIGN_DIRS)

    try:
        scope, warnings = scope_mod.synthesize_scope(root)
    except OSError as exc:  # pragma: no cover - unreadable project dir
        return {
            "status": "error",
            "scope_path": str(scope_path),
            "written": False,
            "detected": {
                "framework": framework,
                "platform": "",
                "serve_cmd": "",
                "serve_url": "",
                "package_manager": pm,
            },
            "allow": [],
            "deny": [],
            "design_dir_found": design_dir_found,
            "next_steps": [],
            "warnings": [f"Could not scan {root}: {exc}"],
        }

    serve_cmd = scope.serve.cmd if scope.serve else ""
    serve_url = scope.serve.url if scope.serve else ""
    detected = {
        "framework": framework,
        "platform": scope.platform,
        "serve_cmd": serve_cmd,
        "serve_url": serve_url,
        "package_manager": pm,
    }

    # Never clobber an existing config unless explicitly asked to.
    if scope_path.exists() and not overwrite:
        return {
            "status": "exists",
            "scope_path": str(scope_path),
            "written": False,
            "detected": detected,
            "allow": list(scope.allow),
            "deny": list(scope.deny),
            "design_dir_found": design_dir_found,
            "next_steps": [
                f"A {SCOPE_FILENAME} already exists; it was left unchanged.",
                "Call ui_init again with overwrite=True to regenerate it from "
                "the detected stack.",
            ],
            "warnings": warnings,
        }

    try:
        scope_path.write_text(
            _SCOPE_HEADER + "\n" + _scope_to_yaml(scope), encoding="utf-8"
        )
    except OSError as exc:
        return {
            "status": "error",
            "scope_path": str(scope_path),
            "written": False,
            "detected": detected,
            "allow": list(scope.allow),
            "deny": list(scope.deny),
            "design_dir_found": design_dir_found,
            "next_steps": [],
            "warnings": warnings + [f"Could not write {scope_path}: {exc}"],
        }

    next_steps: list[str] = []
    if not design_dir_found:
        next_steps.append(
            "Create a ./design/ folder and drop your screen mockups (PNG/JPG) "
            "into it so the tools can match the UI against them."
        )
    else:
        next_steps.append(
            "Found a design folder — reference a mockup per screen via "
            "`design_ref` in the targets you add, or pass `design_refs` to "
            "ui_implement."
        )
    next_steps.append(
        f"Review the generated {SCOPE_FILENAME} (serve command, url, and the "
        "allow/deny globs) and adjust anything the auto-detector got wrong."
    )
    next_steps.append(
        "Then call ui_implement with your task (and optional design_refs) to "
        "start the vision loop."
    )

    return {
        "status": "ok",
        "scope_path": str(scope_path),
        "written": True,
        "detected": detected,
        "allow": list(scope.allow),
        "deny": list(scope.deny),
        "design_dir_found": design_dir_found,
        "next_steps": next_steps,
        "warnings": warnings,
    }


def get_server() -> FastMCP:
    """Return the configured FastMCP server instance."""
    return mcp
