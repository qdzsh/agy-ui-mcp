#!/usr/bin/env bash
#
# agy-ui-mcp installer (run from a clone of this repo).
#
# Installs the MCP server, installs the Playwright Chromium browser used for
# web/accessibility captures, and offers to register the server with Claude Code.
# Idempotent: safe to re-run.
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if [[ ! -f pyproject.toml ]]; then
  echo "error: run this from a clone of the agy-ui-mcp repo (pyproject.toml not found)." >&2
  exit 1
fi

say() { printf '\n==> %s\n' "$1"; }

# --- 1. Install the package -------------------------------------------------
# Prefer pipx (isolated, exposes the `agy-ui-mcp` command on PATH). Fall back to
# pip into the active environment.
PLAYWRIGHT_BIN=""
if command -v pipx >/dev/null 2>&1; then
  say "Installing agy-ui-mcp with pipx"
  pipx install --force "$REPO_ROOT"
  VENVS="$(pipx environment --value PIPX_LOCAL_VENVS 2>/dev/null || echo "$HOME/.local/pipx/venvs")"
  PLAYWRIGHT_BIN="$VENVS/agy-ui-mcp/bin/playwright"
  RUN_CMD="agy-ui-mcp"
else
  say "pipx not found; installing with pip into the current Python environment"
  python3 -m pip install --upgrade "$REPO_ROOT"
  PLAYWRIGHT_BIN="$(command -v playwright || true)"
  RUN_CMD="python3 -m agy_ui_mcp"
fi

# --- 2. Install the Chromium browser (web / a11y captures) ------------------
say "Installing Playwright Chromium (for web + accessibility captures)"
if [[ -n "$PLAYWRIGHT_BIN" && -x "$PLAYWRIGHT_BIN" ]]; then
  "$PLAYWRIGHT_BIN" install chromium || echo "  (skipped: 'playwright install chromium' failed; install it later for web/a11y)"
elif command -v playwright >/dev/null 2>&1; then
  playwright install chromium || true
else
  echo "  (could not locate the playwright CLI; run 'playwright install chromium' yourself for web/a11y)"
fi

# --- 3. Register with Claude Code (optional) --------------------------------
if command -v claude >/dev/null 2>&1; then
  say "Registering with Claude Code (user scope)"
  if claude mcp add agy-ui --scope user -- $RUN_CMD; then
    echo "  registered. Verify with: claude mcp list"
  else
    echo "  (registration skipped/failed — it may already exist. Run manually:"
    echo "     claude mcp add agy-ui --scope user -- $RUN_CMD )"
  fi
else
  say "Claude Code CLI not found; register manually with:"
  echo "     claude mcp add agy-ui --scope user -- $RUN_CMD"
fi

# --- 4. Codex snippet -------------------------------------------------------
say "For Codex, add this to ~/.codex/config.toml:"
if [[ "$RUN_CMD" == *" "* ]]; then
  printf '     [mcp_servers.agy-ui]\n     command = "python3"\n     args = ["-m", "agy_ui_mcp"]\n'
else
  printf '     [mcp_servers.agy-ui]\n     command = "agy-ui-mcp"\n'
fi

say "Done. Next: drop a .agy-ui-scope in your project (see .agy-ui-scope.example)."
echo "    Requires the 'agy' CLI installed and logged in."
