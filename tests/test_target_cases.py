"""Tests for the extended :class:`Target` design-case fields.

Covers the fields added to support the remaining web-design cases: RTL, print,
dark via ``prefers-color-scheme``, high-contrast (``forced-colors``), and
data/dynamic-content states seeded through ``local_storage``.

These import :mod:`agy_ui_mcp.scope` directly (pydantic + pyyaml only), so
nothing here spawns a browser or the agy CLI — Playwright stays uninvolved.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agy_ui_mcp.scope import AgyUiScope, Target, load_scope, resolve_targets

# --- Defaults (backward-compatible) -----------------------------------------


def test_new_fields_default_to_unset() -> None:
    """A bare Target leaves every new field at its inert default."""
    target = Target()
    assert target.color_scheme is None
    assert target.media is None
    assert target.forced_colors is None
    assert target.full_page is False
    assert target.local_storage == {}
    # Pre-existing fields keep their defaults too.
    assert target.rtl is False
    assert target.reduce_motion is True


def test_local_storage_default_is_independent() -> None:
    """The dict default must not be shared across Target instances."""
    a = Target()
    b = Target()
    a.local_storage["cart"] = "[]"
    assert b.local_storage == {}


# --- Valid Literal / typed values -------------------------------------------


@pytest.mark.parametrize("value", ["light", "dark", "no-preference"])
def test_color_scheme_accepts_valid(value: str) -> None:
    assert Target(color_scheme=value).color_scheme == value


@pytest.mark.parametrize("value", ["screen", "print"])
def test_media_accepts_valid(value: str) -> None:
    assert Target(media=value).media == value


@pytest.mark.parametrize("value", ["active", "none"])
def test_forced_colors_accepts_valid(value: str) -> None:
    assert Target(forced_colors=value).forced_colors == value


def test_full_page_and_local_storage_typed() -> None:
    target = Target(full_page=True, local_storage={"cart": "[]"})
    assert target.full_page is True
    assert target.local_storage == {"cart": "[]"}


# --- Invalid Literal values are rejected ------------------------------------


def test_color_scheme_invalid_rejected() -> None:
    with pytest.raises(ValueError):
        Target(color_scheme="midnight")  # not in the Literal set


def test_media_invalid_rejected() -> None:
    with pytest.raises(ValueError):
        Target(media="braille")  # not in the Literal set


def test_forced_colors_invalid_rejected() -> None:
    with pytest.raises(ValueError):
        Target(forced_colors="high")  # not in the Literal set


def test_local_storage_non_string_value_rejected() -> None:
    with pytest.raises(ValueError):
        Target(local_storage={"cart": ["not", "a", "string"]})


# --- Parsing a YAML scope that exercises every new case ---------------------

_CASES_YAML = """\
model: "gemini-3.5-flash"

serve:
  cmd: "npm run dev"
  url: "http://localhost:5173"

targets:
  - name: "invoice-print"
    route: "/invoice"
    media: "print"
    full_page: true
  - name: "home-dark"
    route: "/"
    color_scheme: "dark"
  - name: "home-hc"
    route: "/"
    forced_colors: "active"
  - name: "home-rtl"
    route: "/"
    rtl: true
  - name: "cart-empty"
    route: "/cart"
    local_storage:
      cart: "[]"
  - name: "cart-overflow"
    route: "/cart"
    full_page: true
    local_storage:
      cart: '[{"id":1,"name":"x"}]'
"""


def test_load_scope_parses_all_new_cases(tmp_path: Path) -> None:
    (tmp_path / ".agy-ui-scope").write_text(_CASES_YAML, encoding="utf-8")
    scope = load_scope(tmp_path)

    by_name = {t.name: t for t in scope.targets}
    assert set(by_name) == {
        "invoice-print",
        "home-dark",
        "home-hc",
        "home-rtl",
        "cart-empty",
        "cart-overflow",
    }

    # Print case: media + full_page.
    assert by_name["invoice-print"].media == "print"
    assert by_name["invoice-print"].full_page is True

    # Dark via prefers-color-scheme (not the theme attribute).
    assert by_name["home-dark"].color_scheme == "dark"
    assert by_name["home-dark"].theme is None

    # High-contrast.
    assert by_name["home-hc"].forced_colors == "active"

    # RTL.
    assert by_name["home-rtl"].rtl is True

    # Data states seeded through localStorage.
    assert by_name["cart-empty"].local_storage == {"cart": "[]"}
    assert by_name["cart-overflow"].local_storage["cart"].startswith("[{")
    assert by_name["cart-overflow"].full_page is True

    # Targets supersede viewports and resolve_targets returns them verbatim.
    assert resolve_targets(scope) == scope.targets


def test_example_scope_still_parses() -> None:
    """The shipped example (with the new commented cases) must still load."""
    repo_root = Path(__file__).resolve().parents[1]
    example = repo_root / ".agy-ui-scope.example"
    text = example.read_text(encoding="utf-8")
    # The new cases live in comments; uncommenting must not be required to load.
    scope = AgyUiScope.model_validate(
        __import__("yaml").safe_load(text)
    )
    assert isinstance(scope, AgyUiScope)
