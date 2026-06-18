"""Tests for path classification in :mod:`agy_ui_mcp.policy_builder`."""

from __future__ import annotations

import pytest

from agy_ui_mcp.policy_builder import PolicyBuilder, classify_path
from agy_ui_mcp.scope import AgyUiScope, ServeConfig


@pytest.fixture()
def scope() -> AgyUiScope:
    """A representative Vite/React scope mirroring the example config."""
    return AgyUiScope(
        model="gemini-3.5-flash",
        allow=[
            "src/**/*.css",
            "src/components/**",
            "src/**/*.tsx",
            "index.html",
        ],
        deny=[
            "**/api/**",
            "**/server/**",
            "**/*.server.*",
            "**/route.*",
        ],
        ambiguous=["src/main.tsx", "src/App.tsx"],
        serve=ServeConfig(cmd="npm run dev", url="http://localhost:5173"),
        viewports=[1440, 768, 390],
    )


def test_css_file_is_allowed(scope: AgyUiScope) -> None:
    assert classify_path(scope, "src/components/Button.css") == "allow"


def test_component_tsx_is_allowed(scope: AgyUiScope) -> None:
    assert classify_path(scope, "src/components/Card.tsx") == "allow"


def test_api_file_is_denied(scope: AgyUiScope) -> None:
    assert classify_path(scope, "src/api/client.ts") == "deny"
    assert classify_path(scope, "src/api/users/route.ts") == "deny"


def test_server_file_is_denied(scope: AgyUiScope) -> None:
    assert classify_path(scope, "src/data.server.ts") == "deny"


def test_main_tsx_is_ambiguous(scope: AgyUiScope) -> None:
    # main.tsx matches both `src/**/*.tsx` (allow) and the ambiguous list;
    # ambiguous must win over allow.
    assert classify_path(scope, "src/main.tsx") == "ambiguous"


def test_unlisted_path_defaults_to_deny(scope: AgyUiScope) -> None:
    assert classify_path(scope, "README.md") == "deny"


def test_deny_wins_over_allow_for_api_tsx(scope: AgyUiScope) -> None:
    # A .tsx under api/ matches allow (*.tsx) AND deny (api/**) -> deny wins.
    assert classify_path(scope, "src/api/Widget.tsx") == "deny"


def test_command_allow_serve(scope: AgyUiScope) -> None:
    builder = PolicyBuilder(scope)
    assert builder.is_command_allowed("npm run dev") is True
    assert builder.is_command_allowed("npm run build") is True


def test_command_deny_destructive(scope: AgyUiScope) -> None:
    builder = PolicyBuilder(scope)
    assert builder.is_command_allowed("rm -rf /") is False
    assert builder.is_command_allowed("git push origin main") is False


def test_dotfile_not_mangled() -> None:
    # Regression: a previous `lstrip("./")` ate leading '.'/'/' chars, turning
    # ".env" into "env". Verify ".env" is classified by its real name.
    dotscope = AgyUiScope(
        allow=["src/**"],
        deny=[".env", "**/.env"],
    )
    assert classify_path(dotscope, ".env") == "deny"
    # And a dotfile that is not denied falls through to default-deny (not
    # accidentally matched as "env").
    plain = AgyUiScope(allow=["src/**"])
    assert classify_path(plain, ".env") == "deny"


def test_dot_slash_prefix_is_stripped(scope: AgyUiScope) -> None:
    # A leading "./" must be normalized away so allow globs still match.
    assert classify_path(scope, "./src/components/Button.css") == "allow"
    assert classify_path(scope, "././src/components/Button.css") == "allow"


def test_dotfile_under_allowed_dir() -> None:
    # A dotfile living under an allowed tree should still match the allow glob.
    dotscope = AgyUiScope(allow=["src/**"])
    assert classify_path(dotscope, "src/.eslintrc.json") == "allow"
    assert classify_path(dotscope, "./src/.eslintrc.json") == "allow"
