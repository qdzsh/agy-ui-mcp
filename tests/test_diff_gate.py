"""Tests for the diff-gate in :mod:`agy_ui_mcp.policy_builder`.

A real temporary git repo is created in ``tmp_path``: we commit a baseline, then
simulate an agy turn by editing an *allowed* file, an explicitly *denied* file,
an *ambiguous* file, and creating a brand-new *untracked* out-of-scope file.
``apply_diff_gate`` must keep only the allowed change and revert the rest.

These tests are skipped when ``git`` is not on PATH.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from agy_ui_mcp.policy_builder import apply_diff_gate
from agy_ui_mcp.scope import AgyUiScope, ServeConfig

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git is not installed"
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A git repo with a committed baseline of FE + backend files."""
    r = tmp_path
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@t.local")
    _git(r, "config", "user.name", "t")

    (r / "src" / "components").mkdir(parents=True)
    (r / "src" / "api").mkdir(parents=True)
    (r / "src" / "components" / "Button.css").write_text("a{color:red}\n")
    (r / "src" / "api" / "client.ts").write_text("export const x = 1;\n")
    (r / "src" / "App.tsx").write_text("export const App = () => null;\n")

    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "baseline")
    return r


@pytest.fixture()
def scope() -> AgyUiScope:
    return AgyUiScope(
        allow=["src/**/*.css", "src/components/**"],
        deny=["**/api/**"],
        ambiguous=["src/App.tsx"],
        serve=ServeConfig(cmd="npm run dev", url="http://localhost:5173"),
    )


def test_diff_gate_keeps_allow_reverts_deny_and_escalates_ambiguous(
    repo: Path, scope: AgyUiScope
) -> None:
    # Allowed edit (CSS).
    (repo / "src" / "components" / "Button.css").write_text("a{color:blue}\n")
    # Denied edit (backend api).
    (repo / "src" / "api" / "client.ts").write_text("export const x = 2;\n")
    # Ambiguous edit (sensitive entry point).
    (repo / "src" / "App.tsx").write_text("export const App = () => 1;\n")
    # Untracked, out-of-scope new file (default-deny).
    (repo / "src" / "secret.server.ts").write_text("danger\n")

    result = apply_diff_gate(str(repo), scope)

    # Allowed change kept on disk and reported.
    assert "src/components/Button.css" in result.kept
    assert (repo / "src" / "components" / "Button.css").read_text() == "a{color:blue}\n"

    # Denied change reverted on disk and reported.
    assert "src/api/client.ts" in result.reverted
    assert (repo / "src" / "api" / "client.ts").read_text() == "export const x = 1;\n"

    # Ambiguous change reverted and escalated.
    assert "src/App.tsx" in result.escalations
    assert (
        repo / "src" / "App.tsx"
    ).read_text() == "export const App = () => null;\n"

    # Untracked default-deny file removed.
    assert "src/secret.server.ts" in result.reverted
    assert not (repo / "src" / "secret.server.ts").exists()


def test_diff_gate_noop_when_clean(repo: Path, scope: AgyUiScope) -> None:
    result = apply_diff_gate(str(repo), scope)
    assert result.kept == []
    assert result.reverted == []
    assert result.escalations == []


def test_diff_gate_reverts_staged_denied_file(
    repo: Path, scope: AgyUiScope
) -> None:
    """A STAGED out-of-scope edit must revert to HEAD, not survive (issue #3).

    Regression: without `git reset -q HEAD -- <path>` before `git checkout --`,
    a staged backend edit is restored from the index (the staged content) rather
    than HEAD — the gate would report it reverted while the edit silently
    survives. agy runs with --dangerously-skip-permissions and can `git add`.
    """
    (repo / "src" / "api" / "client.ts").write_text("export const x = 999;\n")
    _git(repo, "add", "src/api/client.ts")  # STAGE the denied edit

    result = apply_diff_gate(str(repo), scope)

    assert "src/api/client.ts" in result.reverted
    # Must be HEAD content, not the staged "999".
    assert (repo / "src" / "api" / "client.ts").read_text() == "export const x = 1;\n"
    # Nothing left staged for that path.
    staged = subprocess.run(
        ["git", "-C", str(repo), "diff", "--cached", "--name-only"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "src/api/client.ts" not in staged


def test_diff_gate_reverts_staged_untracked_denied_file(
    repo: Path, scope: AgyUiScope
) -> None:
    """A staged NEW out-of-scope file is unstaged and removed, not kept."""
    (repo / "src" / "secret.server.ts").write_text("danger\n")
    _git(repo, "add", "src/secret.server.ts")  # stage a brand-new denied file

    result = apply_diff_gate(str(repo), scope)

    assert "src/secret.server.ts" in result.reverted
    assert not (repo / "src" / "secret.server.ts").exists()
