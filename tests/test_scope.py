"""Tests for parsing the ``.agy-ui-scope.example`` file."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from agy_ui_mcp.scope import AgyUiScope, ServeConfig, load_scope

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / ".agy-ui-scope.example"


@pytest.fixture()
def project_dir(tmp_path: Path) -> Path:
    """Copy the example scope into a temp project as the real config name."""
    shutil.copy(EXAMPLE, tmp_path / ".agy-ui-scope")
    return tmp_path


def test_load_scope_parses_example(project_dir: Path) -> None:
    scope = load_scope(project_dir)
    assert isinstance(scope, AgyUiScope)
    assert scope.model == "gemini-3.5-flash"


def test_allow_deny_ambiguous_present(project_dir: Path) -> None:
    scope = load_scope(project_dir)
    assert "src/**/*.tsx" in scope.allow
    assert "**/api/**" in scope.deny
    assert "src/main.tsx" in scope.ambiguous


def test_serve_block(project_dir: Path) -> None:
    scope = load_scope(project_dir)
    assert isinstance(scope.serve, ServeConfig)
    assert scope.serve.cmd == "npm run dev"
    assert scope.serve.url == "http://localhost:5173"
    assert scope.serve.ready_timeout == 30


def test_reload_cmd_defaults_to_none() -> None:
    """ServeConfig omits reload_cmd by default (HMR servers need no rebuild)."""
    serve = ServeConfig(cmd="npm run dev", url="http://localhost:5173")
    assert serve.reload_cmd is None


def test_reload_cmd_parses(tmp_path: Path) -> None:
    """A non-HMR scope (Flutter web) declares a reload_cmd that parses."""
    yaml_text = (
        'serve:\n'
        '  cmd: "python3 -m http.server 5000 --directory build/web"\n'
        '  url: "http://localhost:5000/"\n'
        '  reload_cmd: "flutter build web"\n'
    )
    (tmp_path / ".agy-ui-scope").write_text(yaml_text, encoding="utf-8")
    scope = load_scope(tmp_path)
    assert scope.serve is not None
    assert scope.serve.reload_cmd == "flutter build web"


def test_viewports(project_dir: Path) -> None:
    scope = load_scope(project_dir)
    assert scope.viewports == [1440, 768, 390]


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_scope(tmp_path)


def test_rejects_non_positive_viewport() -> None:
    with pytest.raises(ValueError):
        AgyUiScope(viewports=[0])
