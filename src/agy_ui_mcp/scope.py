"""Parsing and validation of the ``.agy-ui-scope`` config file.

The scope file declares which files the Gemini agent may edit (``allow``),
must never touch (``deny``), and which require human confirmation
(``ambiguous``), plus how to serve the app and which viewports to capture.

All models are Pydantic v2. The single entry point is :func:`load_scope`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final, Literal

import yaml
from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

#: Default config filename looked up in a project directory.
SCOPE_FILENAME: Final[str] = ".agy-ui-scope"

#: Default model when the scope file omits ``model``.
DEFAULT_MODEL: Final[str] = "gemini-3.5-flash"


class ServeConfig(BaseModel):
    """How to launch the dev server so screenshots can be captured."""

    cmd: str = Field(description="Shell command that starts the dev server.")
    url: str = Field(description="Base URL the server listens on once ready.")
    ready_timeout: int = Field(
        default=30,
        ge=1,
        description="Seconds to wait for the URL to respond before giving up.",
    )
    reload_cmd: str | None = Field(
        default=None,
        description=(
            "Optional shell command run to rebuild/refresh the app after every "
            "edit the agent makes, executed in the worktree cwd. Required for "
            "non-HMR frameworks (e.g. Flutter web, where a static server serves "
            "`build/web` and needs `flutter build web` between iterations so the "
            "next screenshot reflects the change). Leave unset for HMR dev "
            "servers like Vite, which hot-reload automatically and need no "
            "explicit rebuild."
        ),
    )


class Device(BaseModel):
    """A capture device: either a Playwright registry name or explicit metrics.

    A device may be referenced from a :class:`Target` via ``target.device`` to
    emulate a real phone/tablet (viewport, DPR, touch, UA). Provide *either* a
    ``name`` from Playwright's built-in device registry (e.g. ``"iPhone 13"``)
    *or* an explicit ``width`` + ``height`` pair with optional overrides.

    The registry ``name`` may also be supplied under the alias ``device`` in
    YAML (``{ device: "iPhone 13" }`` and ``{ name: "iPhone 13" }`` are
    equivalent), because earlier docs/examples used ``device:`` for this key.
    Note this is unrelated to :attr:`Target.device`, which is the *key* into
    :attr:`AgyUiScope.devices` selecting which device a target emulates.

    For *native* captures (``platform: ios-sim``) the same model selects which
    iOS simulator to screenshot via ``xcrun simctl``. There the device is
    identified by its ``udid`` (e.g. ``"ABCD-1234"``) or, when ``udid`` is
    absent, by ``name`` interpreted as the *simulator* name (e.g.
    ``"iPhone 17"``) which the adapter resolves to a UDID at capture time.
    Native devices need neither ``width`` nor ``height`` (the simulator owns its
    own screen geometry).

    Attributes:
        name: Key in Playwright's device registry (``pw.devices[name]``), or the
            iOS *simulator* name for ``ios-sim`` captures. Also accepted under
            the alias ``device`` in YAML/dict input.
        udid: iOS Simulator UDID (``ios-sim`` only). When set it pins the exact
            simulator; when absent the adapter resolves ``name`` to a UDID.
        width: Explicit viewport width (px); required when ``name``/``udid`` are
            absent.
        height: Explicit viewport height (px); required when ``name``/``udid``
            are absent.
        device_scale_factor: Device pixel ratio (DPR) for explicit devices.
        is_mobile: Whether to emulate a mobile device (meta viewport honored).
        has_touch: Whether the device reports touch support.
        user_agent: Optional UA string override for explicit devices.
    """

    model_config = ConfigDict(populate_by_name=True)

    name: str | None = Field(
        default=None,
        validation_alias=AliasChoices("name", "device"),
    )
    udid: str | None = Field(default=None)
    width: int | None = Field(default=None)
    height: int | None = Field(default=None)
    device_scale_factor: float = Field(default=1)
    is_mobile: bool = Field(default=False)
    has_touch: bool = Field(default=False)
    user_agent: str | None = Field(default=None)

    @model_validator(mode="after")
    def _name_or_dimensions(self) -> "Device":
        """Require a ``name``, a ``udid``, or an explicit ``width``+``height``.

        A registry/simulator ``name`` or a native iOS ``udid`` identifies the
        device on its own; otherwise an explicit ``width``+``height`` pair is
        required.
        """
        if (
            self.name is None
            and self.udid is None
            and (self.width is None or self.height is None)
        ):
            raise ValueError(
                "device must set `name`, `udid`, or both `width` and `height`"
            )
        return self


class PreStep(BaseModel):
    """A single interaction run before a target's screenshot is captured.

    Pre-steps drive component states (hover/focus/open menus), fill inputs, or
    wait for async content so the captured frame reflects the intended state.
    Failures are non-fatal at runtime (recorded as warnings, never abort).

    Attributes:
        action: The interaction to perform.
        selector: CSS/Playwright selector the action targets (most actions).
        value: Value for ``fill`` (text), ``press`` (key), ``set_attribute``.
        attr: Attribute name for ``set_attribute``.
        state: Awaited element state for ``wait_for`` (e.g. ``"visible"``).
        timeout_ms: Per-step timeout in milliseconds.
    """

    action: Literal[
        "click", "hover", "focus", "fill", "press", "wait_for", "set_attribute"
    ]
    selector: str | None = Field(default=None)
    value: str | None = Field(default=None)
    attr: str | None = Field(default=None)
    state: str = Field(default="visible")
    timeout_ms: int = Field(default=5000)


class Target(BaseModel):
    """One independent capture: route x device x theme x state.

    Each target is screenshotted on its own and may carry its own design
    reference, so a single run can match several mockups (desktop vs mobile,
    light vs dark, default vs hover) against the same codebase.

    Attributes:
        name: Human label; auto-filled (``t{idx}``) when omitted.
        route: Path appended to ``serve.url`` (e.g. ``"/login"``).
        device: Key in :attr:`AgyUiScope.devices` to emulate.
        viewport_width: Shortcut viewport width when not using a ``device``.
        design_ref: Mockup image specific to this target (overrides global).
        theme: Theme value to apply via ``theme_attr`` on ``theme_selector``.
        theme_attr: Attribute name used to set the theme (default
            ``data-theme``).
        theme_selector: Element the theme attribute is set on (default
            ``html``).
        rtl: When True, set ``dir="rtl"`` on the document before capture.
        pre_steps: Interactions run before the screenshot is taken.
        reduce_motion: Emulate ``prefers-reduced-motion: reduce`` (default
            True) to stabilize animated UIs for deterministic shots.
        color_scheme: Emulate ``prefers-color-scheme`` (``light``/``dark``/
            ``no-preference``). This drives the OS-level media query, which is
            distinct from the app-level ``theme`` attribute: many apps theme
            themselves purely via ``@media (prefers-color-scheme: dark)``.
        media: Emulate the CSS media type (``screen`` or ``print``) so a
            target can exercise the app's ``@media print`` stylesheet.
        forced_colors: Emulate ``forced-colors`` (``active``/``none``) to test
            high-contrast / Windows forced-colors mode rendering.
        full_page: Capture the full scrollable page (tall pages) when True;
            otherwise only the viewport is captured (the default).
        local_storage: Key/value pairs seeded into ``localStorage`` *before*
            the page loads, used to drive data states (empty/long/error) that
            the app reads from storage on first render.
    """

    name: str | None = Field(default=None)
    route: str = Field(default="")
    device: str | None = Field(default=None)
    viewport_width: int | None = Field(default=None)
    design_ref: str | None = Field(default=None)
    theme: str | None = Field(default=None)
    theme_attr: str = Field(default="data-theme")
    theme_selector: str = Field(default="html")
    rtl: bool = Field(default=False)
    pre_steps: list[PreStep] = Field(default_factory=list)
    reduce_motion: bool = Field(default=True)
    color_scheme: Literal["light", "dark", "no-preference"] | None = Field(
        default=None
    )
    media: Literal["screen", "print"] | None = Field(default=None)
    forced_colors: Literal["active", "none"] | None = Field(default=None)
    full_page: bool = Field(default=False)
    local_storage: dict[str, str] = Field(default_factory=dict)


class AgyUiScope(BaseModel):
    """Validated representation of a ``.agy-ui-scope`` file.

    Attributes:
        model: Gemini model id to delegate to.
        platform: Capture platform for the app under review. Web-targets
            (``web``, ``expo-web``, ``flutter-web``, ``ionic``) all serve over
            HTTP and are captured with Playwright through the same pipeline —
            they differ only in ``serve.cmd``/``url`` and which style globs the
            user allows, not in *how* they are screenshotted. The native
            platforms (``ios-sim``, ``android-emu``) would require a separate
            native screenshot adapter (iOS Simulator / Android emulator) and are
            not yet supported; declaring them raises at capture time. Defaults
            to ``web``.
        allow: Globs the agent may edit/create.
        deny: Globs the agent must never touch (wins over allow).
        ambiguous: Globs that require human confirmation before edit.
        serve: How to start the app for visual review.
        viewports: Viewport widths (px) to capture (backward-compatible; used
            only when ``targets`` is empty).
        devices: Named device profiles referenceable from targets.
        targets: Explicit capture targets (route/device/theme/state). When
            non-empty, ``targets`` supersedes ``viewports``.
    """

    model: str = Field(default=DEFAULT_MODEL)
    platform: Literal[
        "web", "expo-web", "flutter-web", "ionic", "ios-sim", "android-emu"
    ] = Field(
        default="web",
        description=(
            "Capture platform. Web-targets (web/expo-web/flutter-web/ionic) use "
            "the Playwright pipeline; ios-sim/android-emu would need a native "
            "adapter (not yet supported)."
        ),
    )
    allow: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)
    ambiguous: list[str] = Field(default_factory=list)
    serve: ServeConfig | None = Field(default=None)
    viewports: list[int] = Field(default_factory=lambda: [1440, 768, 390])
    devices: dict[str, Device] = Field(default_factory=dict)
    targets: list[Target] = Field(default_factory=list)

    @field_validator("viewports")
    @classmethod
    def _viewports_positive(cls, value: list[int]) -> list[int]:
        """Reject non-positive viewport widths early."""
        if any(width <= 0 for width in value):
            raise ValueError("viewport widths must be positive integers")
        return value


def resolve_targets(scope: AgyUiScope) -> list[Target]:
    """Resolve the effective capture targets for a scope.

    Precedence, always yielding at least one target:

    1. If ``scope.targets`` is non-empty, return them (filling any missing
       ``name`` with a stable ``t{idx}`` default).
    2. Else, if ``scope.viewports`` is non-empty, synthesize one target per
       width (``Target(name="w{w}", viewport_width=w)``) — the backward-
       compatible path for configs that predate ``targets``.
    3. Else, fall back to a single ``desktop`` target at 1440px width.

    Args:
        scope: The loaded scope.

    Returns:
        A non-empty list of :class:`Target` objects.
    """
    if scope.targets:
        resolved: list[Target] = []
        for idx, target in enumerate(scope.targets):
            if target.name is None or target.name == "":
                target = target.model_copy(update={"name": f"t{idx}"})
            resolved.append(target)
        return resolved

    if scope.viewports:
        return [
            Target(name=f"w{width}", viewport_width=width)
            for width in scope.viewports
        ]

    return [Target(name="desktop", viewport_width=1440)]


def load_scope(project_dir: str | Path) -> AgyUiScope:
    """Load and validate the ``.agy-ui-scope`` file from a project directory.

    Args:
        project_dir: Path to the project root containing ``.agy-ui-scope``.

    Returns:
        A validated :class:`AgyUiScope`.

    Raises:
        FileNotFoundError: If no scope file exists in ``project_dir``.
        ValueError: If the YAML is malformed or fails validation.
    """
    root = Path(project_dir)
    scope_path = root / SCOPE_FILENAME
    if not scope_path.is_file():
        raise FileNotFoundError(
            f"No {SCOPE_FILENAME} found in {root}. "
            f"Copy .agy-ui-scope.example and adjust it."
        )

    raw = scope_path.read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:  # pragma: no cover - defensive
        raise ValueError(f"Invalid YAML in {scope_path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError(f"{scope_path} must contain a YAML mapping at the top level")

    return AgyUiScope.model_validate(data)
