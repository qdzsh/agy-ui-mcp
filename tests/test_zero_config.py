"""Tests for zero-config scope synthesis, the ``ui_init`` tool, and preflight.

All tests build fake projects in pytest ``tmp_path`` and never need git, a real
``agy`` CLI, or a browser: the preflight gate (and ``AGY_UI_DRY_RUN``) block any
external call before the tools reach agy/Playwright/git.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from agy_ui_mcp import scope as scope_mod
from agy_ui_mcp import server
from agy_ui_mcp.scope import (
    DEFAULT_MODEL,
    SCOPE_FILENAME,
    AgyUiScope,
    detect_framework,
    load_scope,
    synthesize_scope,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / ".agy-ui-scope.example"


# --- fake-project builders ---------------------------------------------------


def _write_package_json(
    root: Path,
    deps: dict[str, str] | None = None,
    dev_deps: dict[str, str] | None = None,
    scripts: dict[str, str] | None = None,
) -> None:
    payload: dict[str, Any] = {"name": "fake", "version": "0.0.0"}
    if deps:
        payload["dependencies"] = deps
    if dev_deps:
        payload["devDependencies"] = dev_deps
    if scripts is not None:
        payload["scripts"] = scripts
    else:
        payload["scripts"] = {"dev": "vite"}
    (root / "package.json").write_text(json.dumps(payload), encoding="utf-8")


def _vite_project(root: Path) -> Path:
    _write_package_json(root, dev_deps={"vite": "^5.0.0", "react": "^18"})
    return root


def _next_project(root: Path) -> Path:
    _write_package_json(root, deps={"next": "^14", "react": "^18"})
    return root


def _expo_project(root: Path) -> Path:
    _write_package_json(root, deps={"expo": "^50", "react-native": "0.73"})
    return root


def _ionic_project(root: Path) -> Path:
    _write_package_json(root, deps={"@ionic/react": "^7", "react": "^18"})
    return root


def _cra_project(root: Path) -> Path:
    _write_package_json(
        root,
        deps={"react-scripts": "5.0.1", "react": "^18"},
        scripts={"start": "react-scripts start"},
    )
    return root


def _flutter_project(root: Path) -> Path:
    (root / "pubspec.yaml").write_text("name: fake_app\n", encoding="utf-8")
    return root


# --- synthesize_scope: per-framework profiles --------------------------------


def test_synthesize_vite(tmp_path: Path) -> None:
    _vite_project(tmp_path)
    scope, warnings = synthesize_scope(tmp_path)

    assert scope.platform == "web"
    assert scope.serve is not None
    assert "5173" in scope.serve.url
    assert any(g.endswith(".tsx") or "*.tsx" in g for g in scope.allow)
    assert "**/api/**" in scope.deny
    assert scope.model == DEFAULT_MODEL
    assert warnings  # zero-config always explains itself


def test_synthesize_next(tmp_path: Path) -> None:
    _next_project(tmp_path)
    scope, warnings = synthesize_scope(tmp_path)

    assert scope.platform == "web"
    assert scope.serve is not None
    assert "3000" in scope.serve.url
    assert "**/api/**" in scope.deny
    assert warnings


def test_synthesize_flutter(tmp_path: Path) -> None:
    _flutter_project(tmp_path)
    scope, warnings = synthesize_scope(tmp_path)

    assert scope.platform == "flutter-web"
    assert scope.serve is not None
    assert "5000" in scope.serve.url
    assert scope.serve.ready_timeout == 120
    assert "lib/**/*.dart" in scope.allow
    assert warnings


def test_synthesize_expo(tmp_path: Path) -> None:
    _expo_project(tmp_path)
    scope, warnings = synthesize_scope(tmp_path)

    assert scope.platform == "expo-web"
    assert scope.serve is not None
    assert "19006" in scope.serve.url
    assert scope.serve.cmd == "npx expo start --web"
    assert warnings


def test_synthesize_ionic(tmp_path: Path) -> None:
    _ionic_project(tmp_path)
    scope, warnings = synthesize_scope(tmp_path)

    assert scope.platform == "ionic"
    assert scope.serve is not None
    assert "8100" in scope.serve.url
    assert warnings


def test_synthesize_cra(tmp_path: Path) -> None:
    _cra_project(tmp_path)
    scope, warnings = synthesize_scope(tmp_path)

    assert scope.platform == "web"
    assert scope.serve is not None
    assert "3000" in scope.serve.url
    assert scope.serve.cmd == "npm run start"
    assert warnings


def test_synthesize_generic_empty_dir(tmp_path: Path) -> None:
    scope, warnings = synthesize_scope(tmp_path)

    assert scope.platform == "web"
    assert scope.serve is not None
    assert "5173" in scope.serve.url
    assert "**/api/**" in scope.deny
    assert warnings  # non-empty: notes that no manifest was found


def test_synthesize_generic_unknown_package(tmp_path: Path) -> None:
    # A package.json with no recognizable framework dep -> generic web profile.
    _write_package_json(tmp_path, deps={"lodash": "^4"})
    scope, warnings = synthesize_scope(tmp_path)

    assert scope.platform == "web"
    assert scope.serve is not None
    assert "5173" in scope.serve.url
    assert any("framework" in w for w in warnings)


def test_package_manager_detection(tmp_path: Path) -> None:
    _vite_project(tmp_path)
    (tmp_path / "pnpm-lock.yaml").write_text("", encoding="utf-8")
    scope, _ = synthesize_scope(tmp_path)
    assert scope.serve is not None
    assert scope.serve.cmd.startswith("pnpm run")


def test_yarn_omits_run_keyword(tmp_path: Path) -> None:
    _vite_project(tmp_path)
    (tmp_path / "yarn.lock").write_text("", encoding="utf-8")
    scope, _ = synthesize_scope(tmp_path)
    assert scope.serve is not None
    assert scope.serve.cmd == "yarn dev"


# --- detect_framework labels -------------------------------------------------


@pytest.mark.parametrize(
    ("builder", "label"),
    [
        (_vite_project, "vite-react"),
        (_next_project, "next"),
        (_expo_project, "expo"),
        (_ionic_project, "ionic"),
        (_cra_project, "cra"),
        (_flutter_project, "flutter"),
    ],
)
def test_detect_framework_labels(
    tmp_path: Path, builder: Any, label: str
) -> None:
    builder(tmp_path)
    assert detect_framework(tmp_path) == label


def test_detect_framework_unknown(tmp_path: Path) -> None:
    assert detect_framework(tmp_path) == "unknown"


def test_detect_framework_generic_web(tmp_path: Path) -> None:
    _write_package_json(tmp_path, deps={"lodash": "^4"})
    assert detect_framework(tmp_path) == "generic-web"


# --- _load_or_synthesize_scope ----------------------------------------------


def test_load_or_synthesize_falls_back_when_absent(tmp_path: Path) -> None:
    _vite_project(tmp_path)
    scope, warnings = server._load_or_synthesize_scope(str(tmp_path))
    assert isinstance(scope, AgyUiScope)
    assert scope.serve is not None
    assert "5173" in scope.serve.url
    assert warnings  # synthesized -> non-empty warnings


def test_load_or_synthesize_uses_existing_file(tmp_path: Path) -> None:
    shutil.copy(EXAMPLE, tmp_path / SCOPE_FILENAME)
    scope, warnings = server._load_or_synthesize_scope(str(tmp_path))
    assert isinstance(scope, AgyUiScope)
    assert warnings == []  # a real file loaded -> no synth warnings


def test_load_or_synthesize_propagates_malformed_file(tmp_path: Path) -> None:
    # A top-level YAML list is not a mapping -> ValueError (surfaced as today).
    (tmp_path / SCOPE_FILENAME).write_text("- not-a-mapping\n", encoding="utf-8")
    with pytest.raises(ValueError):
        server._load_or_synthesize_scope(str(tmp_path))


# --- ui_init -----------------------------------------------------------------


def test_ui_init_writes_loadable_scope(tmp_path: Path) -> None:
    _vite_project(tmp_path)
    result = server.ui_init(project_dir=str(tmp_path))

    assert result["status"] == "ok"
    assert result["written"] is True
    assert result["detected"]["framework"] == "vite-react"
    assert result["detected"]["platform"] == "web"
    assert "5173" in result["detected"]["serve_url"]
    assert result["detected"]["package_manager"] == "npm"
    assert result["allow"]
    assert result["deny"]
    assert result["next_steps"]

    written = Path(result["scope_path"])
    assert written.exists()
    # The generated file round-trips through the real loader without error.
    loaded = load_scope(tmp_path)
    assert loaded.platform == "web"
    assert loaded.serve is not None
    assert "5173" in loaded.serve.url


def test_ui_init_does_not_clobber_existing(tmp_path: Path) -> None:
    _vite_project(tmp_path)
    first = server.ui_init(project_dir=str(tmp_path))
    assert first["status"] == "ok"

    marker = "# user hand-edit\n"
    scope_path = Path(first["scope_path"])
    scope_path.write_text(scope_path.read_text(encoding="utf-8") + marker)

    second = server.ui_init(project_dir=str(tmp_path))
    assert second["status"] == "exists"
    assert second["written"] is False
    # The user's edit survived (no overwrite).
    assert marker in scope_path.read_text(encoding="utf-8")


def test_ui_init_overwrite_regenerates(tmp_path: Path) -> None:
    _vite_project(tmp_path)
    server.ui_init(project_dir=str(tmp_path))
    result = server.ui_init(project_dir=str(tmp_path), overwrite=True)
    assert result["status"] == "ok"
    assert result["written"] is True


def test_ui_init_detects_design_dir(tmp_path: Path) -> None:
    _vite_project(tmp_path)
    (tmp_path / "design").mkdir()
    result = server.ui_init(project_dir=str(tmp_path))
    assert result["design_dir_found"] is True


def test_ui_init_no_design_dir(tmp_path: Path) -> None:
    _vite_project(tmp_path)
    result = server.ui_init(project_dir=str(tmp_path))
    assert result["design_dir_found"] is False
    assert any("design" in step.lower() for step in result["next_steps"])


# --- _preflight --------------------------------------------------------------


def test_preflight_blocks_when_agy_missing(monkeypatch: Any) -> None:
    monkeypatch.setattr(server, "DRY_RUN", False)
    monkeypatch.setattr(server.shutil, "which", lambda _name: None)
    scope = AgyUiScope()
    reason = server._preflight(scope, web=True)
    assert reason is not None
    assert "agy" in reason.lower()


def test_preflight_passes_when_agy_present(monkeypatch: Any) -> None:
    monkeypatch.setattr(server, "DRY_RUN", False)
    monkeypatch.setattr(server.shutil, "which", lambda _name: "/usr/bin/agy")
    scope = AgyUiScope(platform="ios-sim")  # native -> no playwright probe
    assert server._preflight(scope, web=False) is None


def test_preflight_skipped_under_dry_run(monkeypatch: Any) -> None:
    monkeypatch.setattr(server, "DRY_RUN", True)
    monkeypatch.setattr(server.shutil, "which", lambda _name: None)
    assert server._preflight(AgyUiScope(), web=True) is None


# --- tool-level preflight: ui_implement blocks before any external call ------


def test_ui_implement_blocked_when_agy_missing(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """With a valid scope present and AGY_UI_DRY_RUN unset, a missing agy CLI
    yields a structured 'blocked' result (no agy/git/browser ever invoked)."""
    shutil.copy(EXAMPLE, tmp_path / SCOPE_FILENAME)
    monkeypatch.setattr(server, "DRY_RUN", False)
    monkeypatch.setattr(server.shutil, "which", lambda _name: None)

    result = server.ui_implement(project_dir=str(tmp_path), task="make it blue")

    assert result["status"] == "blocked"
    assert "agy" in result["blocked_reason"].lower()


def test_ui_review_blocked_when_agy_missing(
    tmp_path: Path, monkeypatch: Any
) -> None:
    shutil.copy(EXAMPLE, tmp_path / SCOPE_FILENAME)
    monkeypatch.setattr(server, "DRY_RUN", False)
    monkeypatch.setattr(server.shutil, "which", lambda _name: None)

    result = server.ui_review(project_dir=str(tmp_path))

    assert result["status"] == "blocked"
    assert "agy" in result["blocked_reason"].lower()
