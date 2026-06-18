"""Offline tests for the apply-back and convergence helpers in ``server``.

These exercise pure filesystem helpers only -- no agy, git, or Playwright is
invoked, so they run fully offline.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from agy_ui_mcp.server import _apply_changes, _run_reload, _shots_identical


def _write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def test_apply_changes_copies_files(tmp_path: Path) -> None:
    worktree = tmp_path / "wt"
    project = tmp_path / "proj"
    # Worktree has the edited files (one nested) the diff-gate kept.
    _write(worktree / "src" / "App.css", b"body { color: red; }")
    _write(worktree / "index.html", b"<html></html>")

    applied = _apply_changes(
        str(worktree), str(project), ["src/App.css", "index.html"]
    )

    assert applied == ["src/App.css", "index.html"]
    assert (project / "src" / "App.css").read_bytes() == b"body { color: red; }"
    assert (project / "index.html").read_bytes() == b"<html></html>"


def test_apply_changes_overwrites_existing(tmp_path: Path) -> None:
    worktree = tmp_path / "wt"
    project = tmp_path / "proj"
    _write(worktree / "a.css", b"new")
    _write(project / "a.css", b"old")

    applied = _apply_changes(str(worktree), str(project), ["a.css"])

    assert applied == ["a.css"]
    assert (project / "a.css").read_bytes() == b"new"


def test_apply_changes_skips_missing_source(tmp_path: Path) -> None:
    worktree = tmp_path / "wt"
    project = tmp_path / "proj"
    _write(worktree / "kept.css", b"kept")
    # "reverted.css" was cleaned by the diff-gate -> source absent -> skipped.

    applied = _apply_changes(
        str(worktree), str(project), ["kept.css", "reverted.css"]
    )

    assert applied == ["kept.css"]
    assert (project / "kept.css").read_bytes() == b"kept"
    assert not (project / "reverted.css").exists()


def test_apply_changes_empty_list(tmp_path: Path) -> None:
    assert _apply_changes(str(tmp_path / "wt"), str(tmp_path / "proj"), []) == []


def test_shots_identical_true(tmp_path: Path) -> None:
    a1 = tmp_path / "a1.png"
    a2 = tmp_path / "a2.png"
    b1 = tmp_path / "b1.png"
    b2 = tmp_path / "b2.png"
    a1.write_bytes(b"\x89PNG-one")
    a2.write_bytes(b"\x89PNG-two")
    b1.write_bytes(b"\x89PNG-one")
    b2.write_bytes(b"\x89PNG-two")

    assert _shots_identical([str(a1), str(a2)], [str(b1), str(b2)]) is True


def test_shots_identical_false_on_content_diff(tmp_path: Path) -> None:
    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    a.write_bytes(b"one")
    b.write_bytes(b"two")

    assert _shots_identical([str(a)], [str(b)]) is False


def test_shots_identical_false_on_length_mismatch(tmp_path: Path) -> None:
    a = tmp_path / "a.png"
    a.write_bytes(b"one")

    assert _shots_identical([str(a)], [str(a), str(a)]) is False


def test_shots_identical_false_on_read_error(tmp_path: Path) -> None:
    a = tmp_path / "missing.png"  # never created
    assert _shots_identical([str(a)], [str(a)]) is False


def test_shots_identical_empty_lists_true() -> None:
    assert _shots_identical([], []) is True


# --- _run_reload (subprocess.run monkeypatched; no real command runs) --------


def test_run_reload_invokes_command_with_cwd(monkeypatch: Any) -> None:
    """_run_reload shells out with the given cmd in the worktree cwd."""
    calls: list[dict[str, Any]] = []

    def fake_run(cmd: str, **kwargs: Any) -> None:
        calls.append({"cmd": cmd, **kwargs})

    monkeypatch.setattr(subprocess, "run", fake_run)
    _run_reload("flutter build web", "/tmp/worktree")

    assert len(calls) == 1
    assert calls[0]["cmd"] == "flutter build web"
    assert calls[0]["cwd"] == "/tmp/worktree"
    assert calls[0]["shell"] is True


def test_run_reload_swallows_timeout(monkeypatch: Any) -> None:
    """A rebuild timeout is best-effort: it must not raise."""

    def fake_run(cmd: str, **kwargs: Any) -> None:
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(subprocess, "run", fake_run)
    # Must not raise.
    _run_reload("flutter build web", "/tmp/worktree")


def test_run_reload_swallows_oserror(monkeypatch: Any) -> None:
    """A missing shell / OSError is swallowed too (best-effort)."""

    def fake_run(cmd: str, **kwargs: Any) -> None:
        raise OSError("no shell")

    monkeypatch.setattr(subprocess, "run", fake_run)
    _run_reload("flutter build web", "/tmp/worktree")
