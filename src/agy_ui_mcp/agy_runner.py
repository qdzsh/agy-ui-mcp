"""Spawn and capture the ``agy`` CLI through a Python PTY.

The ``agy`` CLI loses its stdout when attached to a non-TTY (pipe/subprocess),
so we allocate a pseudo-terminal with :func:`pty.openpty`, hand the slave end to
``subprocess.Popen`` as stdin/stdout/stderr, and drain the master end until EOF.
The captured bytes are decoded and stripped of ANSI escape sequences and stray
carriage returns before being returned.

Key facts about the CLI surface (verified by spike), reflected here:

* No image flag — image *paths* are embedded in the prompt and agy uses its own
  ``read_file`` tool to view them. :func:`build_vision_prompt` does this.
* No ``--output json`` — changed files are recovered with ``git diff`` elsewhere
  (see :mod:`agy_ui_mcp.policy_builder` / :mod:`agy_ui_mcp.worktree`).
* ``--dangerously-skip-permissions`` lets agy write inside its cwd without
  prompting; scope is enforced afterwards by the diff-gate, not by agy.

Everything here is stdlib (``pty``, ``os``, ``subprocess``, ``re``, ``select``)
so the module imports offline with no third-party dependency.
"""

from __future__ import annotations

import os
import re
import select
import subprocess
from dataclasses import dataclass

#: Matches CSI / OSC ANSI escape sequences emitted by the agy TUI.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)")

#: Default per-run timeout (seconds). agy turns that touch the model are slow.
DEFAULT_TIMEOUT: int = 600


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences and carriage returns from ``text``.

    Args:
        text: Raw terminal output captured from the PTY.

    Returns:
        Cleaned text with escape sequences gone and ``\\r`` stripped (CRLF and
        bare CR both collapse so line content survives intact).
    """
    cleaned = _ANSI_RE.sub("", text)
    # Drop carriage returns: CRLF -> LF, and bare CR (cursor-rewrite) -> nothing.
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "")
    return cleaned


@dataclass
class AgyResult:
    """Result of a single ``agy -p`` invocation.

    Attributes:
        text: Cleaned output (ANSI/CR stripped). The human-readable transcript.
        raw: The undecoded-but-joined raw output, before stripping. Useful for
            debugging escape-sequence issues.
        returncode: The CLI process exit code (``-1`` if it was killed on
            timeout, ``None`` only transiently before the process exits).
    """

    text: str
    raw: str
    returncode: int | None = None


def _build_argv(
    prompt: str,
    *,
    model: str | None,
    continue_: bool,
    add_dirs: list[str],
    skip_permissions: bool,
) -> list[str]:
    """Assemble the ``agy`` argv. ``-p <prompt>`` always comes last."""
    argv: list[str] = ["agy"]
    if skip_permissions:
        argv.append("--dangerously-skip-permissions")
    if model:
        argv += ["--model", model]
    if continue_:
        # Keep the conversation context across rounds of the vision loop.
        argv.append("--continue")
    for d in add_dirs:
        # Let agy read files outside its cwd (e.g. screenshots in a temp dir).
        argv += ["--add-dir", d]
    argv += ["-p", prompt]
    return argv


def run_agy(
    prompt: str,
    cwd: str,
    *,
    model: str | None = None,
    continue_: bool = False,
    add_dirs: list[str] | None = None,
    skip_permissions: bool = True,
    timeout: int = DEFAULT_TIMEOUT,
) -> AgyResult:
    """Run ``agy -p <prompt>`` in ``cwd`` through a PTY and capture its output.

    A pseudo-terminal is allocated so agy believes it is attached to a real
    terminal (otherwise its stdout is silently dropped). The slave fd becomes
    the child's stdin/stdout/stderr; the master fd is drained until EOF.

    Args:
        prompt: The full instruction string passed to ``-p``.
        cwd: Working directory for the child (typically the git worktree).
        model: Optional model id for ``--model``.
        continue_: When True, pass ``--continue`` to retain prior context.
        add_dirs: Extra directories for ``--add-dir`` (repeatable). Used so agy
            can read screenshot files that live outside the project dir.
        skip_permissions: Pass ``--dangerously-skip-permissions`` (default True;
            scope is enforced by the diff-gate, not agy).
        timeout: Seconds before the child is killed and the partial output is
            returned with ``returncode == -1``.

    Returns:
        An :class:`AgyResult` with cleaned ``text``, joined ``raw``, and the
        process ``returncode``.
    """
    import pty  # local import: POSIX-only, keeps Windows import from failing.

    argv = _build_argv(
        prompt,
        model=model,
        continue_=continue_,
        add_dirs=list(add_dirs or []),
        skip_permissions=skip_permissions,
    )

    master_fd, slave_fd = pty.openpty()
    chunks: list[bytes] = []
    proc: subprocess.Popen[bytes] | None = None
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
        )
        # The child owns the slave end now; close ours so EOF propagates when
        # the child exits, otherwise the read loop would block forever.
        os.close(slave_fd)
        slave_fd = -1

        deadline = _now() + timeout
        while True:
            remaining = deadline - _now()
            if remaining <= 0:
                proc.kill()
                break
            ready, _, _ = select.select([master_fd], [], [], min(remaining, 1.0))
            if master_fd in ready:
                try:
                    data = os.read(master_fd, 65536)
                except OSError:
                    # Master raises EIO when the slave side has fully closed.
                    break
                if not data:
                    break  # EOF: child closed the terminal.
                chunks.append(data)
            elif proc.poll() is not None:
                # Child exited and the pipe has no more buffered data.
                break

        returncode = proc.wait(timeout=5) if proc.poll() is None else proc.returncode
    finally:
        if slave_fd != -1:
            os.close(slave_fd)
        os.close(master_fd)

    raw = b"".join(chunks).decode("utf-8", errors="replace")
    return AgyResult(text=strip_ansi(raw), raw=raw, returncode=returncode)


def _now() -> float:
    """Monotonic clock seconds (wrapped so tests can patch it if needed)."""
    import time

    return time.monotonic()


def build_vision_prompt(
    task: str,
    current_shots: list[str] | None = None,
    design_refs: list[str] | None = None,
    allow_hint: str = "",
    deny_hint: str = "",
    notes: str = "",
    request_score: bool = False,
    shot_pairs: list[dict] | None = None,
) -> str:
    """Build the English prompt that drives one vision-loop turn.

    The prompt instructs agy to ``read_file`` the current screenshots (and any
    design references), then make CSS/component changes within the allowed
    surface only. Image *paths* are listed inline because the CLI has no image
    flag — agy opens them with its own file-reading tool.

    Two input modes:

    * **Per-target** (``shot_pairs`` given): each entry is a dict
      ``{"label": str, "current": [paths], "design": [paths]}`` describing one
      capture target (e.g. mobile vs desktop, light vs dark). The prompt lists
      each target's current shots beside the mockup(s) it must match, and tells
      agy these are one codebase — so it should reach the per-target designs
      with responsive CSS / media queries / theme rules rather than diverging
      code paths.
    * **Flat** (``shot_pairs`` is None): the legacy single-surface form using
      ``current_shots`` and ``design_refs`` directly.

    Args:
        task: The natural-language UI goal.
        current_shots: Absolute paths to the latest screenshot files (flat mode).
        design_refs: Absolute paths to design reference images (flat mode).
        allow_hint: Human-readable description of files agy may edit.
        deny_hint: Human-readable description of files agy must not touch.
        notes: Optional extra context appended verbatim (e.g. prior escalations).
        request_score: When True (enabled whenever any design reference is
            present), ask agy to end its reply with two machine-readable lines
            reporting its estimated match score against the mockup(s) and the
            remaining visual gaps. Parsed back by :func:`parse_match`.
        shot_pairs: Per-target capture pairs (see above). When given, supersedes
            ``current_shots``/``design_refs``.

    Returns:
        A single prompt string ready for ``run_agy(prompt=...)``.
    """
    current_shots = list(current_shots or [])
    design_refs = list(design_refs or [])
    lines: list[str] = []
    lines.append("You are a frontend UI engineer working in this project.")
    lines.append("")
    lines.append(f"Task: {task}")
    lines.append("")

    if shot_pairs is not None:
        lines.append(
            "You are matching several capture targets (different routes, "
            "devices, themes, or component states) of the SAME codebase. For "
            "each target below, open the current screenshot(s) and the design "
            "mockup(s) with your read_file tool and edit the UI so every "
            "target matches its corresponding design. Because this is one "
            "codebase, achieve per-target differences with responsive CSS "
            "(media queries), theme rules, and state styling — not divergent "
            "logic."
        )
        lines.append("")
        for pair in shot_pairs:
            label = pair.get("label") or "target"
            lines.append(f'Target "{label}":')
            current = pair.get("current") or []
            design = pair.get("design") or []
            if current:
                lines.append("  current screenshot(s):")
                for path in current:
                    lines.append(f"    - {path}")
            else:
                lines.append("  current screenshot(s): (none)")
            if design:
                lines.append("  design mockup(s) to match:")
                for path in design:
                    lines.append(f"    - {path}")
            else:
                lines.append("  design mockup(s) to match: (none)")
            lines.append("")
    else:
        if current_shots:
            lines.append(
                "Current state screenshots (open each with your read_file tool to "
                "see the rendered UI):"
            )
            for path in current_shots:
                lines.append(f"  - {path}")
            lines.append("")

        if design_refs:
            lines.append(
                "Design reference images to match (open each with read_file):"
            )
            for path in design_refs:
                lines.append(f"  - {path}")
            lines.append("")

    lines.append("Scope rules (enforced after your turn by a diff-gate):")
    lines.append(f"  - You MAY edit: {allow_hint}")
    lines.append(f"  - You MUST NOT touch: {deny_hint}")
    lines.append(
        "  - Only change styling and presentational components (CSS, styles, "
        "and UI component markup). Do not change backend, API, routing, data "
        "fetching, or business logic. Any out-of-scope edit will be reverted."
    )
    lines.append("")
    lines.append(
        "Compare the current screenshots against the design intent and edit the "
        "files to close the gap. Make the smallest set of changes that improves "
        "fidelity. Do not run the dev server or git commands yourself."
    )

    if notes:
        lines.append("")
        lines.append("Additional notes:")
        lines.append(notes)

    if request_score:
        lines.append("")
        lines.append(
            "After you finish editing, estimate how closely the resulting UI "
            "now matches the design reference image(s). A score of 100 means a "
            "perfect, pixel-faithful match to the mockup; 0 means no "
            "resemblance at all. End your reply with EXACTLY these two lines, "
            "and nothing after them:"
        )
        lines.append("MATCH_SCORE: <integer 0-100>")
        lines.append(
            "GAPS: <short comma-separated list of remaining visual differences, "
            "or NONE>"
        )

    return "\n".join(lines)


#: Captures a ``MATCH_SCORE:`` line and grabs the first integer that follows.
_MATCH_SCORE_RE = re.compile(r"^\s*MATCH_SCORE:\s*(-?\d+)", re.MULTILINE)

#: Captures a ``GAPS:`` line and grabs the rest of that line.
_GAPS_RE = re.compile(r"^\s*GAPS:\s*(.*?)\s*$", re.MULTILINE)


def parse_match(text: str) -> tuple[int | None, str]:
    """Extract the self-reported match score and remaining gaps from agy output.

    Scans ``text`` for the ``MATCH_SCORE:`` and ``GAPS:`` lines that
    :func:`build_vision_prompt` (with ``request_score=True``) asks agy to emit.
    The parsing is robust to surrounding noise (transcript chatter, repeated
    lines): it takes the *last* matching line of each kind, since agy may
    restate the value, and the final statement is the authoritative one.

    Args:
        text: The cleaned agy transcript (``AgyResult.text``).

    Returns:
        A ``(score, gaps)`` tuple. ``score`` is an int clamped to ``0..100``,
        or ``None`` when no ``MATCH_SCORE:`` line is present. ``gaps`` is the
        stripped remainder of the last ``GAPS:`` line; an empty string when the
        line is missing, blank, or literally ``NONE`` (case-insensitive).
    """
    score: int | None = None
    score_matches = _MATCH_SCORE_RE.findall(text or "")
    if score_matches:
        raw = int(score_matches[-1])
        score = max(0, min(100, raw))

    gaps = ""
    gaps_matches = _GAPS_RE.findall(text or "")
    if gaps_matches:
        candidate = gaps_matches[-1].strip()
        if candidate and candidate.upper() != "NONE":
            gaps = candidate

    return score, gaps
