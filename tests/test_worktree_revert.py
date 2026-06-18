"""Offline tests for scoped reverts in ``worktree.revert_paths``.

These create a throwaway git repo in ``tmp_path`` and exercise the real git
plumbing — no agy/flutter/simulator is involved, so the suite stays offline.

The bug these guard against: an in-place native run used to ``revert_all`` the
whole working tree, wiping the user's *other* uncommitted changes. ``revert_paths``
must touch ONLY the listed paths and leave everything else alone.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from agy_ui_mcp import worktree
from agy_ui_mcp.worktree import WorktreeHandle


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def _init_repo(root: Path) -> None:
    """Init a repo with committed a.txt and b.txt."""
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@local")
    _git(root, "config", "user.name", "test")
    (root / "a.txt").write_text("a-original\n")
    (root / "b.txt").write_text("b-original\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "baseline")


def _handle(root: Path) -> WorktreeHandle:
    """An in-place handle pointing at a real git repo (the native case)."""
    return WorktreeHandle(path=root, project_dir=root, in_place=True)


def test_revert_paths_scoped_leaves_others_untouched(tmp_path: Path) -> None:
    """Revert only a.txt + untracked c.txt; b.txt's change MUST survive."""
    root = tmp_path / "proj"
    _init_repo(root)

    # Mutate both tracked files and add one untracked file.
    (root / "a.txt").write_text("a-modified\n")
    (root / "b.txt").write_text("b-modified\n")
    (root / "c.txt").write_text("c-untracked\n")

    worktree.revert_paths(_handle(root), ["a.txt", "c.txt"])

    # a.txt restored to HEAD, c.txt removed, b.txt's change kept intact.
    assert (root / "a.txt").read_text() == "a-original\n"
    assert not (root / "c.txt").exists()
    assert (root / "b.txt").read_text() == "b-modified\n"


def test_revert_paths_reverts_staged_change(tmp_path: Path) -> None:
    """A staged modification (as after collect_diff's `git add -A`) is reverted."""
    root = tmp_path / "proj"
    _init_repo(root)

    (root / "a.txt").write_text("a-modified\n")
    _git(root, "add", "a.txt")  # stage it, mimicking collect_diff

    worktree.revert_paths(_handle(root), ["a.txt"])

    assert (root / "a.txt").read_text() == "a-original\n"
    # And it is no longer staged.
    status = subprocess.run(
        ["git", "status", "--porcelain", "a.txt"],
        cwd=str(root),
        capture_output=True,
        text=True,
    )
    assert status.stdout.strip() == ""


def test_revert_paths_empty_list_is_noop(tmp_path: Path) -> None:
    """An empty path list changes nothing."""
    root = tmp_path / "proj"
    _init_repo(root)
    (root / "a.txt").write_text("a-modified\n")

    worktree.revert_paths(_handle(root), [])

    assert (root / "a.txt").read_text() == "a-modified\n"


def test_revert_paths_noop_when_in_place_non_git(tmp_path: Path) -> None:
    """In-place run on a non-git dir is a no-op (no crash, file untouched)."""
    root = tmp_path / "plain"
    root.mkdir()
    (root / "a.txt").write_text("a-modified\n")
    handle = WorktreeHandle(path=root, project_dir=root, in_place=True)

    worktree.revert_paths(handle, ["a.txt"])

    assert (root / "a.txt").read_text() == "a-modified\n"


def test_revert_paths_nested_path(tmp_path: Path) -> None:
    """A nested tracked file reverts; a sibling change is preserved."""
    root = tmp_path / "proj"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@local")
    _git(root, "config", "user.name", "test")
    (root / "src").mkdir()
    (root / "src" / "App.css").write_text("color: red;\n")
    (root / "keep.txt").write_text("keep-original\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "baseline")

    (root / "src" / "App.css").write_text("color: blue;\n")
    (root / "keep.txt").write_text("keep-modified\n")

    worktree.revert_paths(_handle(root), ["src/App.css"])

    assert (root / "src" / "App.css").read_text() == "color: red;\n"
    assert (root / "keep.txt").read_text() == "keep-modified\n"
