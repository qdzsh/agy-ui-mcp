"""Parsing and validation of the ``.agy-ui-scope`` config file.

The scope file declares which files the Gemini agent may edit (``allow``),
must never touch (``deny``), and which require human confirmation
(``ambiguous``), plus how to serve the app and which viewports to capture.

All models are Pydantic v2. The single entry point is :func:`load_scope`.
"""

from __future__ import annotations

import json
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


# --- Zero-config scope synthesis --------------------------------------------
#
# Most users of agy-ui-mcp are non-technical "vibe coders" who cannot hand-write
# the ``.agy-ui-scope`` YAML. :func:`synthesize_scope` inspects the project's
# manifests/lockfiles and produces a sensible scope so the tools work with no
# config at all, returning human-readable warnings that explain the guess and
# point at ``ui_init`` for persisting/customizing it.

#: Short framework labels :func:`detect_framework` may return.
FrameworkLabel = Literal[
    "flutter", "expo", "ionic", "next", "vite-react", "cra", "generic-web", "unknown"
]

#: Extension-agnostic backend/business-logic locations that must NEVER be
#: editable by the zero-config defaults. The CORE GUARANTEE of this server is
#: that it only touches FE/UI; the auto-synthesized scope leans on this list so
#: common backend/logic directories (API routes, server code, data/db layers,
#: services, server actions, route/middleware handlers, models, etc.) stay
#: off-limits even when they sit inside an otherwise-allowed glob like
#: ``src/**/*.tsx``. ``classify_path`` gives ``deny`` precedence over ``allow``,
#: so any path matching one of these is reverted regardless of the allow globs.
_BACKEND_DENY: Final[list[str]] = [
    "**/api/**",
    "**/server/**",
    "backend/**",
    "server/**",
    "**/*.server.*",
    "**/route.ts",
    "**/route.js",
    "**/route.tsx",
    "**/middleware.*",
    "**/services/**",
    "**/service/**",
    "**/repositories/**",
    "**/repository/**",
    "**/db/**",
    "**/database/**",
    "**/prisma/**",
    "**/models/**",
    "**/model/**",
    "**/actions/**",
    "**/functions/**",
]

#: Deny globs shared by every web/expo/ionic/next profile — the backend/logic
#: locations above plus tests and vendored deps are always off-limits.
_WEB_DENY: Final[list[str]] = _BACKEND_DENY + [
    "**/*.test.*",
    "**/*.spec.*",
    "**/node_modules/**",
]

#: Flutter-specific deny globs. Flutter keeps all Dart under ``lib/``, so the
#: extension-agnostic ``_BACKEND_DENY`` (tuned for JS/TS layouts) does not apply;
#: instead we carve the common logic/state/data/network directories out of the
#: broad ``lib/**/*.dart`` allow so widgets/screens/theme stay editable while
#: business logic does not.
_FLUTTER_DENY: Final[list[str]] = [
    "test/**",
    "lib/services/**",
    "lib/service/**",
    "lib/repositories/**",
    "lib/repository/**",
    "lib/data/**",
    "lib/models/**",
    "lib/model/**",
    "lib/providers/**",
    "lib/provider/**",
    "lib/bloc/**",
    "lib/cubit/**",
    "lib/api/**",
    "lib/network/**",
    "lib/db/**",
    "lib/database/**",
    "lib/domain/**",
    "lib/usecases/**",
    "lib/usecase/**",
    "lib/state/**",
    "lib/store/**",
]

#: Allow globs for a generic Vite-style web app (also reused by the generic
#: fallback). Component/style files plus the entry HTML/assets.
_VITE_ALLOW: Final[list[str]] = [
    "src/**/*.css",
    "src/**/*.scss",
    "src/components/**",
    "src/**/*.tsx",
    "src/**/*.jsx",
    "public/**/*.svg",
    "index.html",
]


def _detect_package_manager(root: Path) -> str:
    """Detect the JS package manager from a lockfile in ``root``.

    Returns one of ``pnpm`` / ``yarn`` / ``bun`` / ``npm`` (the default when no
    recognized lockfile is present).
    """
    if (root / "pnpm-lock.yaml").is_file():
        return "pnpm"
    if (root / "yarn.lock").is_file():
        return "yarn"
    if (root / "bun.lockb").is_file():
        return "bun"
    return "npm"


def _run_script_cmd(pm: str, script: str) -> str:
    """Format the command that runs an npm ``script`` with package manager ``pm``.

    ``yarn`` invokes scripts without the ``run`` keyword (``yarn dev``); the
    other managers use ``<pm> run <script>`` (``npm run dev``, ``pnpm run dev``,
    ``bun run dev``).
    """
    if pm == "yarn":
        return f"yarn {script}"
    return f"{pm} run {script}"


def _read_package_json(root: Path) -> tuple[dict[str, str], dict[str, str]]:
    """Read ``package.json`` and return its merged deps and its scripts.

    Returns ``(deps, scripts)`` where ``deps`` merges ``dependencies`` and
    ``devDependencies`` (name -> version). Missing/malformed files degrade to
    empty mappings so detection still produces a generic web profile.
    """
    try:
        data = json.loads((root / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, {}
    if not isinstance(data, dict):
        return {}, {}
    deps: dict[str, str] = {}
    for key in ("dependencies", "devDependencies"):
        section = data.get(key)
        if isinstance(section, dict):
            deps.update(section)
    scripts = data.get("scripts")
    scripts = scripts if isinstance(scripts, dict) else {}
    return deps, scripts


def _glob_exists(root: Path, *patterns: str) -> bool:
    """Return True if any of ``patterns`` matches a file directly under ``root``."""
    return any(next(iter(root.glob(p)), None) is not None for p in patterns)


def detect_framework(project_dir: str | Path) -> FrameworkLabel:
    """Classify the project's frontend framework from its manifests on disk.

    The same precedence as :func:`synthesize_scope`, exposed separately so the
    ``ui_init`` tool can report a stable framework label without re-deriving a
    full scope.
    """
    root = Path(project_dir)
    if (root / "pubspec.yaml").is_file():
        return "flutter"
    if not (root / "package.json").is_file():
        return "unknown"

    deps, _ = _read_package_json(root)
    if "expo" in deps:
        return "expo"
    if any(name.startswith("@ionic/") for name in deps):
        return "ionic"
    if "next" in deps or _glob_exists(root, "next.config.*"):
        return "next"
    if "vite" in deps or _glob_exists(root, "vite.config.*"):
        return "vite-react"
    if "react-scripts" in deps:
        return "cra"
    return "generic-web"


def _customize_hint() -> str:
    """Standard pointer telling the user how to persist/customize the guess."""
    return (
        "Run the `ui_init` tool to write a `.agy-ui-scope` file you can edit "
        "(see `.agy-ui-scope.example` for per-screen targets and other options)."
    )


def _safety_hint() -> str:
    """Warn that the zero-config safety scope is a best-effort guess.

    The synthesized deny globs cover common backend/logic layouts, but cannot
    know a project's non-standard structure. Tell the user to run ``ui_init`` to
    review and tighten the deny globs so backend/business logic stays off-limits.
    """
    return (
        "SAFETY: this zero-config edit scope is a best-effort guess at which "
        "files are pure FE/UI; it cannot reliably detect a non-standard backend "
        "layout. Before trusting it, run the `ui_init` tool to review and "
        "tighten the deny globs so your API/server/business-logic files stay "
        "off-limits."
    )


def synthesize_scope(project_dir: str | Path) -> tuple[AgyUiScope, list[str]]:
    """Auto-detect the stack and build a zero-config :class:`AgyUiScope`.

    Inspects ``project_dir`` for framework manifests/lockfiles and returns a
    ready-to-use scope plus human-readable warnings explaining that a guessed
    (zero-config) scope was used and how to customize it via ``ui_init``.

    Detection precedence: Flutter (``pubspec.yaml``) -> JS frameworks by
    dependency (expo / ionic / next / vite / react-scripts) -> generic web. With
    no manifest at all, falls back to the generic web profile.

    Args:
        project_dir: Path to the project root to inspect.

    Returns:
        A ``(scope, warnings)`` tuple. ``warnings`` is always non-empty (it at
        least notes that zero-config detection was used).
    """
    root = Path(project_dir)
    warnings: list[str] = []
    hint = _customize_hint()

    # 1. Flutter: pubspec.yaml at the root.
    if (root / "pubspec.yaml").is_file():
        scope = AgyUiScope(
            model=DEFAULT_MODEL,
            platform="flutter-web",
            serve=ServeConfig(
                cmd="flutter run -d web-server --web-port 5000",
                url="http://localhost:5000",
                ready_timeout=120,
            ),
            allow=["lib/**/*.dart"],
            deny=list(_FLUTTER_DENY),
            ambiguous=[],
        )
        warnings.append(
            "No .agy-ui-scope found; detected a Flutter project (pubspec.yaml) "
            "and used zero-config flutter-web defaults. " + _safety_hint() + " " + hint
        )
        return scope, warnings

    # 2/3. No package.json -> generic web fallback (no manifest found).
    if not (root / "package.json").is_file():
        scope = _generic_web_scope()
        warnings.append(
            "No .agy-ui-scope and no package.json/pubspec.yaml found; used "
            "generic web defaults (Vite-style, http://localhost:5173). "
            + _safety_hint() + " " + hint
        )
        return scope, warnings

    # 2. JS/TS project: read package.json + pick a profile by dependencies.
    deps, scripts = _read_package_json(root)
    pm = _detect_package_manager(root)
    dev_script = "dev" if "dev" in scripts else ("start" if "start" in scripts else None)
    # Default dev command for HMR servers (next/vite/generic). Fall back to a
    # plain `<pm> run dev` when the project declares no usable script.
    dev_cmd = _run_script_cmd(pm, dev_script) if dev_script else _run_script_cmd(pm, "dev")

    # Expo: react-native-web served via the expo CLI (StyleSheet lives in tsx/jsx).
    if "expo" in deps:
        scope = AgyUiScope(
            model=DEFAULT_MODEL,
            platform="expo-web",
            serve=ServeConfig(
                cmd="npx expo start --web",
                url="http://localhost:19006",
                ready_timeout=60,
            ),
            allow=[
                "app/**/*.tsx",
                "app/**/*.jsx",
                "components/**",
                "src/**/*.tsx",
                "src/**/*.jsx",
            ],
            deny=list(_WEB_DENY),
            ambiguous=[],
        )
        warnings.append(
            "No .agy-ui-scope found; detected an Expo project and used "
            "zero-config expo-web defaults. " + _safety_hint() + " " + hint
        )
        return scope, warnings

    # Ionic: any @ionic/* dependency -> `ionic serve` web build on :8100.
    if any(name.startswith("@ionic/") for name in deps):
        scope = AgyUiScope(
            model=DEFAULT_MODEL,
            platform="ionic",
            serve=ServeConfig(
                cmd="ionic serve",
                url="http://localhost:8100",
                ready_timeout=60,
            ),
            allow=["src/**/*.scss", "src/**/*.html", "src/**/*.css"],
            deny=list(_WEB_DENY),
            ambiguous=[],
        )
        warnings.append(
            "No .agy-ui-scope found; detected an Ionic project and used "
            "zero-config ionic defaults. " + _safety_hint() + " " + hint
        )
        return scope, warnings

    # Next.js: `next` dep or a next.config.* at the root -> :3000.
    if "next" in deps or _glob_exists(root, "next.config.*"):
        scope = AgyUiScope(
            model=DEFAULT_MODEL,
            platform="web",
            serve=ServeConfig(
                cmd=dev_cmd,
                url="http://localhost:3000",
                ready_timeout=60,
            ),
            # Next.js Server Components / Server Actions commonly live in
            # `app/**/*.tsx`, so those are NOT silently allowed; only pure
            # presentational components and styles are. Page/layout/server-prone
            # files go to `ambiguous` (reverted AND reported) so a human decides.
            allow=[
                "components/**",
                "src/components/**",
                "**/*.css",
                "**/*.scss",
                "**/*.module.css",
            ],
            deny=list(_WEB_DENY),
            ambiguous=[
                "app/**/*.tsx",
                "app/**/*.jsx",
                "src/app/**/*.tsx",
                "src/app/**/*.jsx",
                "pages/**/*.tsx",
                "pages/**/*.jsx",
                "app/layout.tsx",
                "next.config.*",
            ],
        )
        warnings.append(
            "No .agy-ui-scope found; detected a Next.js project and used "
            "zero-config web defaults (http://localhost:3000). "
            + _safety_hint() + " " + hint
        )
        return scope, warnings

    # Vite: `vite` dep or a vite.config.* at the root -> :5173.
    if "vite" in deps or _glob_exists(root, "vite.config.*"):
        scope = AgyUiScope(
            model=DEFAULT_MODEL,
            platform="web",
            serve=ServeConfig(
                cmd=dev_cmd,
                url="http://localhost:5173",
                ready_timeout=30,
            ),
            allow=list(_VITE_ALLOW),
            deny=list(_WEB_DENY),
            ambiguous=["src/main.tsx", "src/App.tsx", "vite.config.*"],
        )
        warnings.append(
            "No .agy-ui-scope found; detected a Vite project and used "
            "zero-config web defaults (http://localhost:5173). "
            + _safety_hint() + " " + hint
        )
        return scope, warnings

    # Create React App: `react-scripts` dep -> CRA dev server on :3000.
    if "react-scripts" in deps:
        scope = AgyUiScope(
            model=DEFAULT_MODEL,
            platform="web",
            serve=ServeConfig(
                cmd=_run_script_cmd(pm, "start"),
                url="http://localhost:3000",
                ready_timeout=60,
            ),
            allow=list(_VITE_ALLOW),
            deny=list(_WEB_DENY),
            ambiguous=["src/main.tsx", "src/App.tsx"],
        )
        warnings.append(
            "No .agy-ui-scope found; detected a Create React App project and "
            "used zero-config web defaults (http://localhost:3000). "
            + _safety_hint() + " " + hint
        )
        return scope, warnings

    # Generic web: a package.json we could not classify -> Vite-style defaults.
    scope = _generic_web_scope(dev_cmd=dev_cmd)
    warnings.append(
        "No .agy-ui-scope found; could not identify the framework from "
        "package.json, used generic web defaults (http://localhost:5173). "
        + _safety_hint() + " " + hint
    )
    return scope, warnings


def _generic_web_scope(dev_cmd: str = "npm run dev") -> AgyUiScope:
    """Build the generic Vite-style web profile used by the fallbacks."""
    return AgyUiScope(
        model=DEFAULT_MODEL,
        platform="web",
        serve=ServeConfig(
            cmd=dev_cmd,
            url="http://localhost:5173",
            ready_timeout=30,
        ),
        allow=list(_VITE_ALLOW),
        deny=list(_WEB_DENY),
        ambiguous=[],
    )
