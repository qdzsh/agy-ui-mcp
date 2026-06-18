"""Tests for the targets/devices/pre-steps model and ``resolve_targets``.

These import :mod:`agy_ui_mcp.scope` directly. That module is offline-safe
(pydantic + pyyaml only), so nothing here spawns a browser or the agy CLI.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agy_ui_mcp.scope import (
    AgyUiScope,
    Device,
    PreStep,
    Target,
    load_scope,
    resolve_targets,
)

# --- resolve_targets --------------------------------------------------------


def test_resolve_targets_from_viewports() -> None:
    """Empty targets + viewports -> one target per width, named w{width}."""
    scope = AgyUiScope(viewports=[1440, 768, 390])
    targets = resolve_targets(scope)
    assert [t.name for t in targets] == ["w1440", "w768", "w390"]
    assert [t.viewport_width for t in targets] == [1440, 768, 390]
    # No device emulation is implied by the viewports path.
    assert all(t.device is None for t in targets)


def test_resolve_targets_explicit_kept_and_auto_named() -> None:
    """Explicit targets are returned as-is; missing names get a t{idx} default."""
    scope = AgyUiScope(
        targets=[
            Target(viewport_width=1440),  # no name -> t0
            Target(name="mobile", device="phone"),  # name kept
            Target(name="", route="/about"),  # empty name -> t2
        ]
    )
    targets = resolve_targets(scope)
    assert [t.name for t in targets] == ["t0", "mobile", "t2"]
    # Non-name fields survive intact.
    assert targets[1].device == "phone"
    assert targets[2].route == "/about"


def test_resolve_targets_default_when_both_empty() -> None:
    """No targets and no viewports -> a single 1440px desktop target."""
    scope = AgyUiScope(viewports=[])
    targets = resolve_targets(scope)
    assert len(targets) == 1
    assert targets[0].name == "desktop"
    assert targets[0].viewport_width == 1440


def test_resolve_targets_always_nonempty() -> None:
    """resolve_targets must always yield at least one target."""
    assert len(resolve_targets(AgyUiScope())) >= 1
    assert len(resolve_targets(AgyUiScope(viewports=[]))) >= 1


def test_resolve_targets_does_not_mutate_originals() -> None:
    """Auto-naming uses model_copy and must not mutate the scope's targets."""
    original = Target(route="/x")
    scope = AgyUiScope(targets=[original])
    resolved = resolve_targets(scope)
    assert resolved[0].name == "t0"
    assert original.name is None  # untouched


# --- Device validator -------------------------------------------------------


def test_device_name_only_ok() -> None:
    device = Device(name="iPhone 13")
    assert device.name == "iPhone 13"
    assert device.width is None


def test_device_width_height_ok() -> None:
    device = Device(width=390, height=844, is_mobile=True, has_touch=True)
    assert device.width == 390
    assert device.height == 844
    assert device.is_mobile is True
    assert device.has_touch is True
    assert device.device_scale_factor == 1


def test_device_requires_name_or_dimensions() -> None:
    with pytest.raises(ValueError):
        Device()


def test_device_width_without_height_rejected() -> None:
    with pytest.raises(ValueError):
        Device(width=390)


def test_device_height_without_width_rejected() -> None:
    with pytest.raises(ValueError):
        Device(height=844)


# --- Device.name `device` alias ---------------------------------------------


def test_device_alias_device_sets_name() -> None:
    """`{ device: "iPhone 13" }` parses into Device(name="iPhone 13")."""
    device = Device(device="iPhone 13")
    assert device.name == "iPhone 13"
    assert device.width is None


def test_device_name_field_still_accepted() -> None:
    """The canonical `name` field keeps working (populate_by_name)."""
    device = Device(name="iPhone 13")
    assert device.name == "iPhone 13"


def test_device_alias_and_name_are_equivalent() -> None:
    """Both spellings produce an identical Device."""
    assert Device(device="iPhone 13") == Device(name="iPhone 13")


def test_device_alias_still_honors_validator() -> None:
    """An empty device (no name alias, no dimensions) is still rejected."""
    with pytest.raises(ValueError):
        Device.model_validate({})


def test_load_scope_device_alias_in_registry(tmp_path: Path) -> None:
    """A devices entry written with `device:` parses to the same name."""
    yaml_text = (
        'serve:\n'
        '  cmd: "npm run dev"\n'
        '  url: "http://localhost:5173"\n'
        'devices:\n'
        '  mobile:\n'
        '    device: "iPhone 13"\n'
    )
    (tmp_path / ".agy-ui-scope").write_text(yaml_text, encoding="utf-8")
    scope = load_scope(tmp_path)
    assert scope.devices["mobile"].name == "iPhone 13"


# --- PreStep ----------------------------------------------------------------


def test_pre_step_defaults() -> None:
    step = PreStep(action="wait_for", selector="nav")
    assert step.state == "visible"
    assert step.timeout_ms == 5000
    assert step.value is None
    assert step.attr is None


def test_pre_step_invalid_action_rejected() -> None:
    with pytest.raises(ValueError):
        PreStep(action="scroll")  # not in the Literal set


# --- load_scope: a YAML scope with devices + targets + pre_steps ------------

_SCOPE_YAML = """\
model: "gemini-3.5-flash"
allow:
  - "src/**/*.css"
deny:
  - "**/api/**"

serve:
  cmd: "npm run dev"
  url: "http://localhost:5173"
  ready_timeout: 30

devices:
  desktop:
    width: 1440
    height: 900
  mobile:
    name: "iPhone 13"

targets:
  - name: "home-desktop"
    route: "/"
    device: "desktop"
    design_ref: "./mockups/home-desktop.png"
  - name: "home-mobile-dark"
    route: "/"
    device: "mobile"
    theme: "dark"
    design_ref: "./mockups/home-mobile-dark.png"
    pre_steps:
      - action: "click"
        selector: "button.menu-toggle"
      - action: "wait_for"
        selector: "nav.mobile-drawer"
        state: "visible"
        timeout_ms: 8000
"""


def test_load_scope_with_devices_and_targets(tmp_path: Path) -> None:
    (tmp_path / ".agy-ui-scope").write_text(_SCOPE_YAML, encoding="utf-8")
    scope = load_scope(tmp_path)

    # Devices parse into Device models.
    assert set(scope.devices) == {"desktop", "mobile"}
    assert scope.devices["desktop"].width == 1440
    assert scope.devices["mobile"].name == "iPhone 13"

    # Targets parse, preserving order and per-target fields.
    assert [t.name for t in scope.targets] == ["home-desktop", "home-mobile-dark"]
    mobile = scope.targets[1]
    assert mobile.device == "mobile"
    assert mobile.theme == "dark"
    assert mobile.theme_attr == "data-theme"  # default applied
    assert mobile.design_ref == "./mockups/home-mobile-dark.png"

    # Pre-steps parse into PreStep models with the right shape.
    assert len(mobile.pre_steps) == 2
    assert mobile.pre_steps[0].action == "click"
    assert mobile.pre_steps[0].selector == "button.menu-toggle"
    assert mobile.pre_steps[1].action == "wait_for"
    assert mobile.pre_steps[1].state == "visible"
    assert mobile.pre_steps[1].timeout_ms == 8000

    # Targets supersede viewports, but viewports still hold their default.
    assert resolve_targets(scope) == scope.targets
    assert scope.viewports == [1440, 768, 390]
