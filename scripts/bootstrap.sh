#!/usr/bin/env bash
#
# agy-ui-mcp remote bootstrap installer.
#
# Designed to be run directly from the internet (NOT from a clone):
#
#   curl -fsSL https://raw.githubusercontent.com/qdzsh/agy-ui-mcp/main/scripts/bootstrap.sh | bash
#
# It installs the MCP server (preferring pipx, isolated), tries to install the
# Playwright Chromium browser, and offers to register the server with Claude
# Code. The Chromium browser also auto-installs on first use, so step 2 is
# optional. Idempotent: safe to re-run.
#
set -euo pipefail

PKG_PYPI="agy-ui-mcp"
PKG_GIT="git+https://github.com/qdzsh/agy-ui-mcp"

say() { printf '\n==> %s\n' "$1"; }

# --- 0. OS sanity check -----------------------------------------------------
# This server is POSIX-only (PTY + process-group calls). Native Windows is not
# supported; users must run it under WSL2.
case "$(uname -s 2>/dev/null || echo unknown)" in
  Linux | Darwin | *BSD | *bsd)
    : # supported POSIX host
    ;;
  MINGW* | MSYS* | CYGWIN* | Windows_NT)
    echo "error: native Windows is not supported (the server uses POSIX PTY/process-group calls)." >&2
    echo "       Install and run agy-ui-mcp inside WSL2 (Windows Subsystem for Linux) instead." >&2
    exit 1
    ;;
  *)
    # Unknown but likely POSIX (e.g. some CI images); proceed, but warn.
    echo "warning: unrecognized OS '$(uname -s 2>/dev/null)'; assuming POSIX and continuing." >&2
    ;;
esac

# --- 1. Ensure python3 ------------------------------------------------------
if ! command -v python3 >/dev/null 2>&1; then
  echo "error: python3 was not found on PATH." >&2
  echo "       Install Python >= 3.10 first:" >&2
  echo "         - macOS:  brew install python   (or https://www.python.org/downloads/)" >&2
  echo "         - Linux:  sudo apt install python3 python3-pip python3-venv  (Debian/Ubuntu)" >&2
  echo "                   or use your distro's package manager" >&2
  exit 1
fi

# --- 2. Ensure an installer (prefer pipx) -----------------------------------
USE_PIPX=0
if command -v pipx >/dev/null 2>&1; then
  USE_PIPX=1
else
  say "pipx not found; attempting to install it (recommended, isolated install)"
  if python3 -m pip install --user pipx >/dev/null 2>&1 && python3 -m pipx ensurepath >/dev/null 2>&1; then
    # ensurepath edits shell rc files; make pipx usable in THIS session too.
    export PATH="$HOME/.local/bin:$PATH"
    if command -v pipx >/dev/null 2>&1 || python3 -m pipx --version >/dev/null 2>&1; then
      USE_PIPX=1
    fi
  fi
  if [[ "$USE_PIPX" -eq 0 ]]; then
    echo "  (could not install pipx; falling back to 'pip install --user')"
  fi
fi

# pipx may only be importable as a module right after ensurepath.
pipx_run() {
  if command -v pipx >/dev/null 2>&1; then
    pipx "$@"
  else
    python3 -m pipx "$@"
  fi
}

# --- 3. Install the package -------------------------------------------------
# Try PyPI first; fall back to installing straight from the GitHub repo (useful
# before the package is published, or for the latest main).
PLAYWRIGHT_BIN=""
RUN_CMD="agy-ui-mcp"

if [[ "$USE_PIPX" -eq 1 ]]; then
  say "Installing $PKG_PYPI with pipx"
  if pipx_run install --force "$PKG_PYPI"; then
    :
  else
    echo "  (PyPI install failed; trying the GitHub repo instead)"
    pipx_run install --force "$PKG_GIT"
  fi
  VENVS="$(pipx_run environment --value PIPX_LOCAL_VENVS 2>/dev/null || echo "$HOME/.local/pipx/venvs")"
  PLAYWRIGHT_BIN="$VENVS/agy-ui-mcp/bin/playwright"
  RUN_CMD="agy-ui-mcp"
else
  say "Installing $PKG_PYPI with pip (--user)"
  if python3 -m pip install --user --upgrade "$PKG_PYPI"; then
    :
  else
    echo "  (PyPI install failed; trying the GitHub repo instead)"
    python3 -m pip install --user --upgrade "$PKG_GIT"
  fi
  PLAYWRIGHT_BIN="$(command -v playwright || true)"
  # The user-site bin may not be on PATH yet; prefer the console script if found.
  if command -v agy-ui-mcp >/dev/null 2>&1; then
    RUN_CMD="agy-ui-mcp"
  else
    RUN_CMD="python3 -m agy_ui_mcp"
  fi
fi

# --- 4. Install the Chromium browser (optional) -----------------------------
# The server auto-installs Chromium on first use, so this is a best-effort
# warm-up only.
say "Installing Playwright Chromium (optional warm-up; auto-installed on first use otherwise)"
if [[ -n "$PLAYWRIGHT_BIN" && -x "$PLAYWRIGHT_BIN" ]]; then
  "$PLAYWRIGHT_BIN" install chromium || echo "  (skipped: it will auto-install on first use, or set AGY_UI_CHROME_CHANNEL=chrome to reuse Chrome)"
elif command -v playwright >/dev/null 2>&1; then
  playwright install chromium || echo "  (skipped: it will auto-install on first use)"
else
  echo "  (could not locate the playwright CLI; that's fine - the server auto-installs Chromium on first use,"
  echo "   or set AGY_UI_CHROME_CHANNEL=chrome to reuse an already-installed Chrome/Edge)"
fi

# --- 5. Register with Claude Code (optional) --------------------------------
if command -v claude >/dev/null 2>&1; then
  say "Registering with Claude Code (user scope)"
  if claude mcp add agy-ui --scope user -- $RUN_CMD; then
    echo "  registered. Verify with: claude mcp list"
  else
    echo "  (registration skipped/failed - it may already exist. Run manually:"
    echo "     claude mcp add agy-ui --scope user -- $RUN_CMD )"
  fi
else
  say "Claude Code CLI not found; register manually with:"
  echo "     claude mcp add agy-ui --scope user -- $RUN_CMD"
fi

# --- 6. Codex snippet -------------------------------------------------------
say "For Codex, add this to ~/.codex/config.toml:"
if [[ "$RUN_CMD" == *" "* ]]; then
  printf '     [mcp_servers.agy-ui]\n     command = "python3"\n     args = ["-m", "agy_ui_mcp"]\n'
else
  printf '     [mcp_servers.agy-ui]\n     command = "agy-ui-mcp"\n'
fi

# --- 7. Final reminder ------------------------------------------------------
say "Done."
echo "  - You must have the 'agy' CLI installed and logged in (Antigravity subscription) for the server to work."
echo "  - No .agy-ui-scope file is required: the server auto-detects your stack (zero-config)."
echo "    Run the ui_init tool once to generate and inspect a real scope file when you want to customize it."
echo "  - The Chromium browser auto-installs on first use; set AGY_UI_CHROME_CHANNEL=chrome to reuse an installed Chrome."
