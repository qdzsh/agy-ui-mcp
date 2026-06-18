"""Scope classification and the diff-gate that enforces it.

Scope is *not* enforced inside agy (we deliberately do not use agy's global
permission engine or touch ``~/.gemini/...``). Instead, after each agy turn we
inspect the worktree diff and revert any path that is not explicitly allowed.
This module owns both halves of that contract:

* :func:`classify_path` / :class:`PolicyBuilder` — classify a workspace-relative
  path as ``allow`` / ``deny`` / ``ambiguous`` with precedence
  ``deny > ambiguous > allow > default-deny`` using gitignore-style globs.
* :func:`apply_diff_gate` — list changed files in a git repo, classify each,
  ``git checkout --`` (revert) anything not allowed, and report what was kept,
  reverted, and escalated.

Only ``pathspec`` (and stdlib ``subprocess``) is required, so this imports
offline with no agy/SDK dependency.
"""

from __future__ import annotations

import shlex
import subprocess
from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal

import pathspec

from .scope import AgyUiScope

Classification = Literal["allow", "deny", "ambiguous"]

#: Command prefixes that are always denied by :meth:`is_command_allowed`.
_DENIED_COMMAND_PATTERNS: Final[tuple[str, ...]] = (
    "rm",
    "git push",
    "git reset",
    "git clean",
    "sudo",
    "curl",
    "wget",
    ":(){",  # fork-bomb guard
)


def _compile(patterns: list[str]) -> pathspec.PathSpec:
    """Compile gitignore-style globs into a reusable matcher.

    In current ``pathspec`` (0.12+) the non-deprecated factory name is
    ``"gitwildmatch"``; the ``"gitignore"`` alias is deprecated and emits a
    ``DeprecationWarning``. We therefore prefer ``"gitwildmatch"`` and only fall
    back to ``"gitignore"`` if a future release drops the former. Both share the
    same gitignore-style matching semantics, so behavior is identical.
    """
    try:
        return pathspec.PathSpec.from_lines("gitwildmatch", patterns)
    except (ValueError, KeyError, LookupError):
        return pathspec.PathSpec.from_lines("gitignore", patterns)


class PolicyBuilder:
    """Stateful helper that classifies paths against a scope.

    Compiling the globs once and reusing the matcher keeps per-path
    classification cheap during the diff-gate sweep.
    """

    def __init__(self, scope: AgyUiScope) -> None:
        self._scope = scope
        self._allow = _compile(scope.allow)
        self._deny = _compile(scope.deny)
        self._ambiguous = _compile(scope.ambiguous)

    def classify_path(self, path: str) -> Classification:
        """Classify a workspace-relative path.

        Precedence: ``deny`` > ``ambiguous`` > ``allow`` > default ``deny``.

        Args:
            path: A path relative to the project root (forward slashes).

        Returns:
            One of ``"allow"``, ``"deny"``, or ``"ambiguous"``.
        """
        normalized = path.replace("\\", "/")
        # Strip a leading "./" prefix only (repeat for "././..."); must NOT use
        # str.lstrip("./") which would eat any leading '.'/'/' characters and
        # mangle dotfiles like ".env" into "env".
        while normalized.startswith("./"):
            normalized = normalized.removeprefix("./")
        if self._deny.match_file(normalized):
            return "deny"
        if self._ambiguous.match_file(normalized):
            return "ambiguous"
        if self._allow.match_file(normalized):
            return "allow"
        # Default-deny: anything not explicitly allowed is out of scope.
        return "deny"

    def is_command_allowed(self, command: str) -> bool:
        """Return True if ``command`` is the serve/build command and is safe.

        The check is conservative: the command must not start with (or contain
        as a word) any known destructive prefix, and — when a serve command is
        configured — it must share that command's executable.

        Args:
            command: The full shell command to validate.

        Returns:
            True if the command may run, otherwise False.
        """
        stripped = command.strip()
        lowered = stripped.lower()
        for bad in _DENIED_COMMAND_PATTERNS:
            if lowered.startswith(bad) or f" {bad} " in f" {lowered} ":
                return False

        serve = self._scope.serve
        if serve is None:
            return False

        try:
            wanted = shlex.split(serve.cmd)
            got = shlex.split(stripped)
        except ValueError:
            return False
        return bool(got) and bool(wanted) and got[0] == wanted[0]


def classify_path(scope: AgyUiScope, path: str) -> Classification:
    """Convenience wrapper: classify a single path against a scope.

    Args:
        scope: The validated scope config.
        path: A workspace-relative path.

    Returns:
        ``"allow"``, ``"deny"``, or ``"ambiguous"``.
    """
    return PolicyBuilder(scope).classify_path(path)


@dataclass
class DiffGateResult:
    """Outcome of one diff-gate sweep over a worktree.

    Attributes:
        kept: Allowed paths that were left in place.
        reverted: Denied / default-deny paths that were checked out (reverted).
        escalations: Ambiguous paths that were reverted and flagged for a human.
    """

    kept: list[str] = field(default_factory=list)
    reverted: list[str] = field(default_factory=list)
    escalations: list[str] = field(default_factory=list)


def _run_git(repo_dir: str, *args: str) -> subprocess.CompletedProcess[str]:
    """Run ``git -C repo_dir <args>``, capturing text output."""
    return subprocess.run(
        ["git", "-C", repo_dir, *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _changed_paths(
    repo_dir: str,
    base_ref: str = "HEAD",
    prerun_untracked: "Collection[str]" = (),
) -> list[str]:
    """Return tracked+untracked changed paths relative to ``base_ref``.

    Tracked changes are diffed against ``base_ref`` (the pre-run snapshot for an
    in-place run, else ``HEAD``). Untracked files are included EXCEPT those in
    ``prerun_untracked`` — so the user's pre-existing untracked files are never
    flagged (only files agy created this run).
    """
    paths: list[str] = []
    diff = _run_git(repo_dir, "diff", "--name-only", base_ref)
    if diff.returncode == 0:
        paths += [p for p in diff.stdout.splitlines() if p.strip()]
    untracked = _run_git(
        repo_dir, "ls-files", "--others", "--exclude-standard"
    )
    prerun = set(prerun_untracked)
    if untracked.returncode == 0:
        paths += [
            p
            for p in untracked.stdout.splitlines()
            if p.strip() and p not in prerun
        ]
    # Stable, de-duplicated order.
    seen: set[str] = set()
    ordered: list[str] = []
    for p in paths:
        if p not in seen:
            seen.add(p)
            ordered.append(p)
    return ordered


def _revert_path(repo_dir: str, path: str, base_ref: str = "HEAD") -> None:
    """Revert ``path`` to its ``base_ref`` state, or delete if agy created it.

    Mirrors :func:`agy_ui_mcp.worktree.revert_paths`:

    * ``git checkout <base_ref> -- <path>`` restores the file from ``base_ref``,
      IGNORING the index — so a STAGED out-of-scope edit cannot survive (agy runs
      with ``--dangerously-skip-permissions`` and may ``git add``). For an
      in-place run ``base_ref`` is the pre-run snapshot, so this restores the
      user's pre-existing version rather than ``HEAD``.
    * ``git reset -q HEAD -- <path>`` then puts the index back to ``HEAD`` so the
      path's staged/modified status matches its pre-run state.
    * A path not present in ``base_ref`` (a file agy created, possibly STAGED) is
      unstaged and removed with ``git clean``.
    """
    # Restore content from base_ref (ignores the index, so a staged edit can't
    # shadow it). Then unstage unconditionally so the index returns to HEAD AND a
    # staged NEW file becomes untracked — otherwise `ls-files --error-unmatch`
    # would treat it as tracked and skip the clean below.
    _run_git(repo_dir, "checkout", base_ref, "--", path)
    _run_git(repo_dir, "reset", "-q", "HEAD", "--", path)
    if Path(repo_dir, path).exists():
        tracked = _run_git(repo_dir, "ls-files", "--error-unmatch", path)
        if tracked.returncode != 0:
            _run_git(repo_dir, "clean", "-fq", "--", path)


def apply_diff_gate(
    repo_dir: str,
    scope: AgyUiScope,
    base_ref: str = "HEAD",
    prerun_untracked: "Collection[str]" = (),
) -> DiffGateResult:
    """Enforce scope on a worktree by reverting out-of-scope changes.

    For every changed path: classify it; keep ``allow``; revert (and record)
    ``deny`` / default-deny; revert and *escalate* ``ambiguous``.

    Args:
        repo_dir: Path to the git worktree (or in-place project) to sweep.
        scope: The validated scope config driving classification.
        base_ref: The ref/commit to diff and revert against. ``"HEAD"`` for an
            isolated worktree; for an in-place run pass the pre-run baseline
            commit so only agy's edits are gated and reverts restore the user's
            pre-run state.
        prerun_untracked: Untracked paths that existed before the run (in-place);
            excluded so the user's pre-existing untracked files are never gated.

    Returns:
        A :class:`DiffGateResult` describing kept / reverted / escalated paths.
    """
    builder = PolicyBuilder(scope)
    result = DiffGateResult()

    for path in _changed_paths(repo_dir, base_ref, prerun_untracked):
        verdict = builder.classify_path(path)
        if verdict == "allow":
            result.kept.append(path)
        elif verdict == "ambiguous":
            _revert_path(repo_dir, path, base_ref)
            result.escalations.append(path)
        else:  # "deny" (explicit or default)
            _revert_path(repo_dir, path, base_ref)
            result.reverted.append(path)

    return result
