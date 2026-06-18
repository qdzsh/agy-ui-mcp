"""Git worktree lifecycle for isolated, diff-gated agent runs.

Each run ideally happens inside a throwaway git worktree so the user's working
tree is never touched and the diff is trivial to collect. The diff-gate (see
:mod:`agy_ui_mcp.policy_builder`) reverts out-of-scope changes by comparing
against ``HEAD``, so the worktree is guaranteed to have a baseline commit.

When the project is not a git repository we fall back to operating in place and
surface a warning via :attr:`WorktreeHandle.in_place`.

All git interaction goes through ``subprocess`` so the module has no extra
dependency and imports offline.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class WorktreeHandle:
    """Handle to an isolated (or in-place) working directory for a run.

    Attributes:
        path: Directory the agent should operate in (a ``str`` for easy passing
            to ``cwd=``/``--add-dir``; also exposed as :attr:`path_str`).
        project_dir: The original project root.
        branch: The temporary branch name (empty when in-place).
        in_place: True when no git worktree could be created and the agent is
            operating directly on the project (a warning condition).
        base_ref: The git ref/commit the diff-gate and reverts compare against.
            ``"HEAD"`` for isolated worktrees; for in-place runs it is a dangling
            "baseline" commit capturing the project's TRACKED state *before* the
            run, so the gate only sees agy's edits and a revert restores the
            user's pre-run state (preserving their uncommitted work) not HEAD.
        prerun_untracked: Untracked files that already existed before the run
            (in-place only). They are excluded from the gate/revert so the user's
            pre-existing untracked files are never flagged or deleted — only
            untracked files agy *creates* are out-of-scope candidates.
        warnings: Any non-fatal warnings produced while setting up.
    """

    path: Path
    project_dir: Path
    branch: str = ""
    in_place: bool = False
    base_ref: str = "HEAD"
    prerun_untracked: tuple[str, ...] = ()
    warnings: list[str] = field(default_factory=list)

    @property
    def path_str(self) -> str:
        """The worktree path as a string."""
        return str(self.path)


def _run_git(
    cwd: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a git command in ``cwd``, capturing text output.

    ``env`` (when given) replaces the child environment — used to point
    ``GIT_INDEX_FILE`` at a throwaway index when snapshotting the working tree
    without disturbing the user's real index.
    """
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _is_git_repo(project_dir: Path) -> bool:
    """Return True if ``project_dir`` is inside a git work tree."""
    result = _run_git(project_dir, "rev-parse", "--is-inside-work-tree")
    return result.returncode == 0 and result.stdout.strip() == "true"


def _has_commit(project_dir: Path) -> bool:
    """Return True if HEAD points at a commit (repo is not empty)."""
    return _run_git(project_dir, "rev-parse", "--verify", "HEAD").returncode == 0


def _ensure_baseline_commit(project_dir: Path) -> None:
    """Make sure the repo has at least one commit to diff against.

    A freshly ``git init``-ed repo has no ``HEAD``; the diff-gate needs one.
    Best-effort: stages everything and commits it as the baseline.
    """
    if _has_commit(project_dir):
        return
    _run_git(project_dir, "add", "-A")
    _run_git(
        project_dir,
        "-c",
        "user.email=agy-ui@local",
        "-c",
        "user.name=agy-ui",
        "commit",
        "--allow-empty",
        "-m",
        "agy-ui baseline",
    )


class InPlaceSafetyError(RuntimeError):
    """Raised when an in-place run cannot be made safe.

    The diff-gate and scoped revert that protect backend/API/logic files are
    implemented with git. An in-place run (every native run, or a web run that
    cannot be isolated) therefore needs the project to be a **git repository**.
    A dirty or HEAD-less repo is fine — :func:`prepare_inplace` snapshots its
    current state into a baseline so the user's uncommitted work is preserved —
    but a *non-git* directory cannot be protected at all, so we fail closed.
    """


def _list_untracked(project_dir: Path) -> tuple[str, ...]:
    """Return currently-untracked, non-ignored paths (``ls-files --others``)."""
    result = _run_git(project_dir, "ls-files", "--others", "--exclude-standard")
    if result.returncode != 0:
        return ()
    return tuple(p for p in result.stdout.splitlines() if p.strip())


def _make_baseline_commit(project_dir: Path) -> str:
    """Capture the project's current TRACKED state as a dangling baseline commit.

    Returns a commit SHA whose tree is ``HEAD`` with the working tree's tracked
    modifications/deletions applied (``add -u``) — i.e. the tracked files exactly
    as they are right now, INCLUDING the user's uncommitted edits. Untracked
    files are deliberately excluded (they are tracked separately via
    :func:`_list_untracked` so a pre-existing untracked file is never mistaken for
    an agy creation and deleted). Built in a throwaway ``GIT_INDEX_FILE`` so the
    user's real index/HEAD/branches/working tree are untouched; the commit is
    unreferenced (git GC reclaims it later).

    Requires ``HEAD`` to exist (the caller enforces this).

    Raises:
        InPlaceSafetyError: If the snapshot could not be created (git error).
    """
    tmp_dir = tempfile.mkdtemp(prefix="agy-ui-idx-")
    tmp_index = os.path.join(tmp_dir, "index")  # must not pre-exist
    env = {**os.environ, "GIT_INDEX_FILE": tmp_index}

    def _step(label: str, *args: str) -> subprocess.CompletedProcess[str]:
        proc = _run_git(project_dir, *args, env=env)
        if proc.returncode != 0:
            raise InPlaceSafetyError(
                f"could not snapshot {project_dir} (git {label} failed: "
                f"{proc.stderr.strip()})."
            )
        return proc

    try:
        # Seed the temp index from HEAD, then apply tracked working-tree edits.
        _step("read-tree", "read-tree", "HEAD")
        _step("add", "add", "-u")
        tree = _step("write-tree", "write-tree")
        if not tree.stdout.strip():
            raise InPlaceSafetyError(f"could not snapshot {project_dir} (empty tree).")
        commit = _step(
            "commit-tree",
            "commit-tree",
            tree.stdout.strip(),
            "-p",
            "HEAD",
            "-m",
            "agy-ui in-place baseline",
        )
        if not commit.stdout.strip():
            raise InPlaceSafetyError(f"could not snapshot {project_dir} (no commit).")
        return commit.stdout.strip()
    finally:
        for p in (tmp_index, tmp_dir):
            try:
                os.unlink(p) if p == tmp_index else os.rmdir(p)
            except OSError:
                pass


def prepare_inplace(project_dir: str | Path) -> WorktreeHandle:
    """Build an in-place :class:`WorktreeHandle` with a pre-run baseline snapshot.

    Native runs operate on the real project (no worktree) to reuse the build
    cache. To stay safe WITHOUT forcing the user to commit/stash, we snapshot the
    project's current state (tracked baseline commit + the set of pre-existing
    untracked files); the diff-gate and reverts compare against it, so only agy's
    edits are gated/undone and the user's pre-existing uncommitted changes
    (modified tracked files AND untracked files) are preserved.

    Requirements (else fail closed): the project must be a git repo with a
    committed ``HEAD``. A dirty working tree is fine. A non-git or HEAD-less
    project cannot be snapshotted/protected, so it raises (the caller turns that
    into a friendly "blocked" result telling the user how to fix it).

    Args:
        project_dir: The project root the in-place run will operate on.

    Returns:
        An ``in_place=True`` handle carrying ``base_ref`` + ``prerun_untracked``.

    Raises:
        InPlaceSafetyError: If ``project_dir`` is not a git repo or has no HEAD.
    """
    root = Path(project_dir).resolve()
    if not _is_git_repo(root):
        raise InPlaceSafetyError(
            f"{root} is not a git repository. A native/in-place run edits the "
            f"real project directly, so scope enforcement (keeping agy off "
            f"backend/API files) needs git. Run `git init` and commit a baseline."
        )
    if not _has_commit(root):
        raise InPlaceSafetyError(
            f"{root} has no commits yet (no HEAD to anchor the safety snapshot). "
            f"Make one commit (`git add -A && git commit -m baseline`), then re-run."
        )
    prerun_untracked = _list_untracked(root)
    base_ref = _make_baseline_commit(root)
    return WorktreeHandle(
        path=root,
        project_dir=root,
        in_place=True,
        base_ref=base_ref,
        prerun_untracked=prerun_untracked,
        warnings=[
            "native platform: running in place (no worktree) to reuse the "
            "native build; your uncommitted changes are snapshotted and preserved"
        ],
    )


def create_worktree(project_dir: str | Path) -> WorktreeHandle:
    """Create an isolated git worktree for an agent run.

    Ensures the source repo has a baseline commit (so the diff-gate has a
    ``HEAD`` to compare against), then adds a temporary worktree on a new branch.

    Fails closed: if ``project_dir`` is not a git repo, or ``git worktree add``
    fails, it raises :class:`InPlaceSafetyError` instead of silently falling back
    to an UNISOLATED in-place run. The old fallback ran agy directly on the real
    project with a git-based diff-gate that then no-ops on a non-git repo, so
    out-of-scope (backend) edits could survive. Isolation is mandatory.

    Args:
        project_dir: The project root to branch from.

    Returns:
        A :class:`WorktreeHandle` describing where the agent should work.

    Raises:
        InPlaceSafetyError: If the project is not a git repo, or a worktree
            could not be created (so isolation cannot be guaranteed).
    """
    root = Path(project_dir).resolve()

    if not _is_git_repo(root):
        raise InPlaceSafetyError(
            f"{root} is not a git repository. agy-ui isolates each run in a git "
            f"worktree so the diff-gate can keep agy off backend/API files; that "
            f"needs git. Run `git init` and commit a baseline first."
        )

    _ensure_baseline_commit(root)

    suffix = uuid.uuid4().hex[:8]
    branch = f"agy-ui/{suffix}"
    wt_path = Path(tempfile.gettempdir()) / f"agy-ui-worktree-{suffix}"

    result = _run_git(root, "worktree", "add", "-b", branch, str(wt_path), "HEAD")
    if result.returncode != 0:
        raise InPlaceSafetyError(
            f"git worktree add failed for {root}: {result.stderr.strip()}. "
            f"Refusing to run unisolated in place (the diff-gate could not "
            f"protect backend files). Resolve the worktree error and retry."
        )

    return WorktreeHandle(path=wt_path, project_dir=root, branch=branch)


def link_dependencies(
    handle: WorktreeHandle, names: tuple[str, ...] = ("node_modules", ".venv", "vendor")
) -> None:
    """Symlink heavy gitignored dependency dirs from the project into the worktree.

    A fresh worktree has none of ``node_modules``/``.venv``/etc. (they are
    gitignored and not copied), so a dev server started there would fail to boot.
    Best-effort symlink so ``scope.serve.cmd`` can run. Skipped for in-place runs
    (which already have them). These dirs are gitignored, so they never appear in
    the diff-gate.

    Args:
        handle: The worktree handle returned by :func:`create_worktree`.
        names: Top-level dependency directory names to link if present.
    """
    if handle.in_place:
        return
    for name in names:
        src = handle.project_dir / name
        dst = handle.path / name
        if src.exists() and not dst.exists():
            try:
                dst.symlink_to(src, target_is_directory=src.is_dir())
            except OSError as exc:  # pragma: no cover - filesystem dependent
                handle.warnings.append(f"could not link {name}: {exc}")


def list_changed(handle: WorktreeHandle) -> list[str]:
    """List paths changed in the worktree relative to its ``base_ref``.

    Includes tracked modifications/additions (vs ``base_ref``) and untracked new
    files — but EXCLUDES :attr:`WorktreeHandle.prerun_untracked` so an in-place
    run never flags the user's pre-existing untracked files (only files agy
    created this run).

    Args:
        handle: The worktree handle returned by :func:`create_worktree` /
            :func:`prepare_inplace`.

    Returns:
        Repo-relative paths (empty if nothing changed or non-git in-place).
    """
    if handle.in_place and not _is_git_repo(handle.path):
        return []

    paths: list[str] = []
    diff = _run_git(handle.path, "diff", "--name-only", handle.base_ref)
    if diff.returncode == 0:
        paths += [p for p in diff.stdout.splitlines() if p.strip()]
    untracked = _run_git(
        handle.path, "ls-files", "--others", "--exclude-standard"
    )
    prerun = set(handle.prerun_untracked)
    if untracked.returncode == 0:
        paths += [
            p
            for p in untracked.stdout.splitlines()
            if p.strip() and p not in prerun
        ]

    seen: set[str] = set()
    ordered: list[str] = []
    for p in paths:
        if p not in seen:
            seen.add(p)
            ordered.append(p)
    return ordered


def collect_diff(handle: WorktreeHandle) -> str:
    """Collect the unified diff of changes made relative to ``base_ref``.

    For an isolated worktree we stage everything (so new files appear) and diff
    the index. For an IN-PLACE run we must NOT stage (that would mutate the user's
    real index), so we diff the working tree against ``base_ref`` directly — this
    omits brand-new untracked files from the textual diff, which is acceptable
    (they are still reported in ``files_changed``).

    Args:
        handle: The worktree handle.

    Returns:
        The unified diff as text (empty string if nothing changed or non-git).
    """
    if handle.in_place and not _is_git_repo(handle.path):
        return ""

    if handle.in_place:
        result = _run_git(handle.path, "diff", handle.base_ref)
    else:
        _run_git(handle.path, "add", "-A")
        result = _run_git(handle.path, "diff", "--cached", handle.base_ref)
    if result.returncode != 0:
        return ""
    return result.stdout


def revert_all(handle: WorktreeHandle) -> None:
    """Revert every change in the worktree back to its baseline ``HEAD``.

    Restores tracked files and removes untracked ones. Used by read-only flows
    (e.g. ``ui_review``) to guarantee nothing is mutated. No-op for non-git
    in-place runs.

    Args:
        handle: The worktree handle to reset.
    """
    if handle.in_place and not _is_git_repo(handle.path):
        return
    # Unstage first: a prior `collect_diff` runs `git add -A`, so without this
    # `checkout -- .` would restore the *staged* (modified) content instead of
    # HEAD. Harmless no-op when nothing is staged (read-only review path).
    _run_git(handle.path, "reset", "-q", "HEAD")
    _run_git(handle.path, "checkout", "--", ".")
    _run_git(handle.path, "clean", "-fdq")


def revert_paths(handle: WorktreeHandle, paths: list[str]) -> None:
    """Revert only the given ``paths`` back to their baseline ``HEAD`` state.

    Scoped counterpart to :func:`revert_all`: it touches **only** the listed
    paths and never the rest of the working tree. This is what in-place native
    runs use so a no-apply ``ui_implement`` / a read-only ``ui_review`` cannot
    wipe the user's *other* uncommitted changes (e.g. an un-committed
    ``.agy-ui-scope``).

    For each path:

    * ``git reset -q HEAD -- <path>`` unstages it (a prior ``collect_diff``
      stages everything, so without this ``checkout`` would restore the staged
      modification instead of ``HEAD``).
    * ``git checkout -- <path>`` restores a tracked file to ``HEAD``.
    * If the path still exists and is untracked (``ls-files --error-unmatch``
      fails), ``git clean -fq -- <path>`` removes the leftover untracked file.

    For each path we restore content from ``base_ref`` (``git checkout
    <base_ref> -- <path>``), which IGNORES the index — so a STAGED out-of-scope
    edit can never survive (agy may ``git add``). For an isolated worktree
    ``base_ref`` is ``HEAD``; for an in-place run it is the pre-run snapshot, so
    the restore brings back the user's pre-existing uncommitted version, not
    ``HEAD``. The index is then reset to ``HEAD`` so the file's staged/modified
    status matches its pre-run state. A path not present in ``base_ref`` (a file
    agy CREATED) is removed with ``git clean``.

    All git failures are swallowed (best-effort). No-op for non-git in-place
    runs or an empty ``paths`` list. Callers must only pass paths that changed
    THIS run (see :func:`list_changed`), so the user's pre-existing untracked
    files are never passed here and thus never deleted.

    Args:
        handle: The worktree handle to operate in.
        paths: Repo-relative paths to revert (nothing else is touched).
    """
    if not paths:
        return
    if handle.in_place and not _is_git_repo(handle.path):
        return
    base = handle.base_ref
    for path in paths:
        # Restore content from the baseline (ignores any staging), then unstage
        # unconditionally: the index returns to HEAD AND a staged NEW file becomes
        # untracked so the clean below can remove it.
        _run_git(handle.path, "checkout", base, "--", path)
        _run_git(handle.path, "reset", "-q", "HEAD", "--", path)
        # If the path still exists and is NOT tracked, it is a file agy created
        # this run (pre-existing untracked files are filtered out upstream).
        if (handle.path / path).exists():
            tracked = _run_git(handle.path, "ls-files", "--error-unmatch", path)
            if tracked.returncode != 0:
                _run_git(handle.path, "clean", "-fq", "--", path)


def cleanup(handle: WorktreeHandle) -> None:
    """Remove the temporary worktree and its branch.

    No-op for in-place runs. Failures are swallowed (best-effort cleanup).

    Args:
        handle: The worktree handle to tear down.
    """
    if handle.in_place:
        return

    _run_git(handle.project_dir, "worktree", "remove", "--force", str(handle.path))
    if handle.branch:
        _run_git(handle.project_dir, "branch", "-D", handle.branch)
