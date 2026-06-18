"""Tests for the in-place safety model (snapshot-restore + soft block).

In-place runs (every native run) mutate the real project directly. Instead of
forcing a clean tree, :func:`worktree.prepare_inplace` snapshots the project's
pre-run state into a baseline so the diff-gate and reverts compare against it —
only agy's edits are gated/undone and the user's uncommitted work (modified
tracked files AND pre-existing untracked files) is preserved. A non-git/HEAD-less
project cannot be snapshotted, so it raises ``InPlaceSafetyError`` (the tool turns
that into a structured 'blocked' result).

The integration tests here use REAL git repos to prove no data loss.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from agy_ui_mcp import worktree
from agy_ui_mcp.policy_builder import apply_diff_gate
from agy_ui_mcp.scope import AgyUiScope, ServeConfig
from agy_ui_mcp.worktree import InPlaceSafetyError, prepare_inplace

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git is not installed"
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _repo(tmp_path: Path) -> Path:
    """A git repo with a committed baseline: an allowed CSS file + a deny api file."""
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t.local")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "src" / "components").mkdir(parents=True)
    (tmp_path / "src" / "api").mkdir(parents=True)
    (tmp_path / "src" / "components" / "Button.css").write_text("a{color:red}\n")
    (tmp_path / "src" / "api" / "client.ts").write_text("export const x = 1;\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "baseline")
    return tmp_path


def _scope() -> AgyUiScope:
    return AgyUiScope(
        allow=["src/**/*.css", "src/components/**"],
        deny=["**/api/**"],
        serve=ServeConfig(cmd="x", url="http://localhost:1"),
    )


# --- prepare_inplace: requirements ------------------------------------------


def test_prepare_inplace_raises_on_non_git(tmp_path: Path) -> None:
    with pytest.raises(InPlaceSafetyError, match="not a git repository"):
        prepare_inplace(tmp_path)


def test_prepare_inplace_raises_without_head(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    with pytest.raises(InPlaceSafetyError, match="no commits|HEAD"):
        prepare_inplace(tmp_path)


def test_prepare_inplace_clean_repo(tmp_path: Path) -> None:
    handle = prepare_inplace(_repo(tmp_path))
    assert handle.in_place is True
    assert len(handle.base_ref) == 40  # a real commit SHA, not "HEAD"
    assert handle.prerun_untracked == ()


def test_prepare_inplace_dirty_repo_records_state(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "src" / "components" / "Button.css").write_text("a{color:GREEN}\n")
    (repo / "notes.txt").write_text("my scratch notes\n")  # untracked
    handle = prepare_inplace(repo)
    assert handle.in_place is True
    assert len(handle.base_ref) == 40
    assert "notes.txt" in handle.prerun_untracked


# --- snapshot-restore safety (the critical data-loss guards) ----------------


def test_gate_reverts_agy_edit_to_USER_dirty_version_not_head(
    tmp_path: Path,
) -> None:
    """agy edits a pre-existing-dirty DENY file -> restored to the user's WIP."""
    repo = _repo(tmp_path)
    deny = repo / "src" / "api" / "client.ts"
    deny.write_text("export const x = USER_WIP;\n")  # user's uncommitted work
    handle = prepare_inplace(repo)  # snapshot captures USER_WIP

    deny.write_text("export const x = AGY_EDIT;\n")  # agy clobbers it
    result = apply_diff_gate(
        str(repo), _scope(), handle.base_ref, handle.prerun_untracked
    )

    assert "src/api/client.ts" in result.reverted
    # Restored to the USER's pre-run version, NOT committed HEAD, NOT agy's.
    assert deny.read_text() == "export const x = USER_WIP;\n"


def test_gate_preserves_untouched_user_dirty_file(tmp_path: Path) -> None:
    """A pre-existing-dirty file agy never touches is left exactly as the user had."""
    repo = _repo(tmp_path)
    deny = repo / "src" / "api" / "client.ts"
    deny.write_text("export const x = USER_WIP;\n")
    handle = prepare_inplace(repo)

    # agy only edits the allowed CSS; the deny file is its pre-run (dirty) self.
    (repo / "src" / "components" / "Button.css").write_text("a{color:blue}\n")
    result = apply_diff_gate(
        str(repo), _scope(), handle.base_ref, handle.prerun_untracked
    )

    assert "src/api/client.ts" not in result.reverted
    assert deny.read_text() == "export const x = USER_WIP;\n"  # untouched
    assert "src/components/Button.css" in result.kept


def test_gate_removes_agy_new_file_but_keeps_user_untracked(
    tmp_path: Path,
) -> None:
    """agy's NEW untracked deny file is removed; the user's untracked file survives."""
    repo = _repo(tmp_path)
    user_untracked = repo / "my_notes.txt"
    user_untracked.write_text("important user notes\n")  # pre-existing untracked
    handle = prepare_inplace(repo)

    agy_new = repo / "src" / "api" / "secret.server.ts"
    agy_new.write_text("danger\n")  # agy creates an out-of-scope file
    result = apply_diff_gate(
        str(repo), _scope(), handle.base_ref, handle.prerun_untracked
    )

    # agy's new out-of-scope file removed...
    assert "src/api/secret.server.ts" in result.reverted
    assert not agy_new.exists()
    # ...but the user's pre-existing untracked file is NEVER touched.
    assert user_untracked.exists()
    assert user_untracked.read_text() == "important user notes\n"


def test_revert_paths_restores_user_dirty_version(tmp_path: Path) -> None:
    """apply=False scoped revert of an allowed file restores the user's WIP."""
    repo = _repo(tmp_path)
    css = repo / "src" / "components" / "Button.css"
    css.write_text("a{color:USERWIP}\n")  # user's uncommitted edit
    handle = prepare_inplace(repo)

    css.write_text("a{color:AGY}\n")  # agy edits it (allowed, kept)
    worktree.revert_paths(handle, ["src/components/Button.css"])

    # Restored to the user's pre-run version, not HEAD ("red"), not agy's.
    assert css.read_text() == "a{color:USERWIP}\n"


# --- create_worktree (web) still fail-closed on non-git ---------------------


def test_create_worktree_raises_on_non_git(tmp_path: Path) -> None:
    with pytest.raises(InPlaceSafetyError, match="not a git repository"):
        worktree.create_worktree(tmp_path)


def test_create_worktree_isolates_dirty_repo(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "src" / "components" / "Button.css").write_text("a{color:dirty}\n")
    handle = worktree.create_worktree(repo)
    try:
        assert handle.in_place is False
    finally:
        worktree.cleanup(handle)
