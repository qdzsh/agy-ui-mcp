"""Playwright-based screenshots written to files for the vision loop.

The orchestrator (not agy) captures screenshots to *files*; their paths are then
embedded in the agy prompt so agy can open them with its own ``read_file`` tool.
Because of that, every capture function writes a PNG to disk and returns its
path rather than raw bytes.

Playwright's *sync* API is used and imported lazily inside each function, so this
module (and its unit tests) imports fine when the ``playwright`` package or its
browser binaries are not installed. Install browsers with
``playwright install chromium``.

Automated accessibility checks (axe-core) run against web targets via
:func:`audit_a11y`, which injects the vendored ``vendor/axe.min.js`` into a live
Playwright page and returns structured WCAG violations. Native captures
(``ios-sim`` / ``android-emu``) cannot run axe (there is no DOM) and are skipped.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime import
    from .scope import Device, PreStep, Target

#: Default viewport height (px). Width drives responsive review; a tall height
#: plus full-page capture covers the rest.
DEFAULT_HEIGHT: Final[int] = 900

#: Vendored axe-core bundle injected by :func:`audit_a11y` to run WCAG checks in
#: the page. Re-vendor with ``npm install axe-core@<ver>`` (see vendor/README.md).
_AXE_JS_PATH: Final[Path] = Path(__file__).with_name("vendor") / "axe.min.js"

#: Lazily-cached axe-core source so the 0.5MB file is read from disk only once.
_AXE_JS_CACHE: list[str | None] = []


def _load_axe_js() -> str | None:
    """Return the vendored axe-core source, or None if it is not bundled.

    Cached after the first read. A missing bundle is non-fatal: callers degrade
    to skipping the a11y audit with a warning rather than raising.
    """
    if _AXE_JS_CACHE:
        return _AXE_JS_CACHE[0]
    try:
        src: str | None = _AXE_JS_PATH.read_text(encoding="utf-8")
    except OSError:
        src = None
    _AXE_JS_CACHE.append(src)
    return src

#: Platforms whose UI is served over HTTP and can therefore be screenshotted by
#: the existing Playwright pipeline (``capture_target``). Mobile *web-targets*
#: (Ionic, Expo / react-native-web, Flutter web) all fall here: they differ from
#: plain ``web`` only in their ``serve.cmd``/``url``/style globs, never in *how*
#: the screenshot is taken.
WEB_TARGET_PLATFORMS: Final[frozenset[str]] = frozenset(
    {"web", "expo-web", "flutter-web", "ionic"}
)

#: Native platforms that would require a dedicated simulator/emulator screenshot
#: adapter instead of Playwright. Declaring one currently raises at capture time.
NATIVE_PLATFORMS: Final[frozenset[str]] = frozenset({"ios-sim", "android-emu"})


class PlaywrightUnavailableError(RuntimeError):
    """Raised when Playwright (or its browsers) is not available at runtime."""


def _import_sync_playwright():
    """Import ``playwright.sync_api.sync_playwright`` lazily.

    Returns:
        The ``sync_playwright`` factory.

    Raises:
        PlaywrightUnavailableError: If Playwright is not installed.
    """
    try:
        from playwright.sync_api import sync_playwright  # type: ignore

        return sync_playwright
    except Exception as exc:  # pragma: no cover - depends on install
        raise PlaywrightUnavailableError(
            "Playwright is not installed. Run `pip install -e .` then "
            "`playwright install chromium`."
        ) from exc


#: Set once per process after the first auto-install attempt so the (slow) browser
#: download is never retried more than once, even across many capture calls.
_CHROMIUM_INSTALL_ATTEMPTED: bool = False

#: Substrings (matched case-insensitively) that mark a launch failure as a missing
#: browser binary rather than some other error. Playwright's message varies by
#: version, so we match on any of these stable fragments.
_MISSING_BROWSER_MARKERS: Final[tuple[str, ...]] = (
    "executable doesn't exist",
    "playwright install",
    "looks like playwright",
)


def _launch_chromium(pw):
    """Launch a headless Chromium browser, with convenience for non-technical users.

    Reads optional environment variables to support driving a system-installed
    browser, and (by default) auto-installs the bundled Chromium the first time a
    launch fails because the browser binary has not been downloaded yet.

    Environment variables:
        AGY_UI_CHROME_CHANNEL: If non-empty, passed as ``channel`` (e.g. ``chrome``
            or ``msedge``) to drive a system-installed Chrome/Edge instead of the
            downloaded Chromium. Auto-install is skipped in this mode because a
            chromium download cannot fix a missing system browser.
        AGY_UI_CHROME_EXECUTABLE: If set, passed as ``executable_path`` to launch a
            specific browser binary. Auto-install is skipped in this mode too: a
            chromium download cannot fix a user-specified path, and retrying would
            only reuse the same bad path.
        AGY_UI_NO_BROWSER_AUTOINSTALL: If truthy, disables the auto-install fallback.
        AGY_UI_DRY_RUN: If truthy, disables the auto-install fallback (no side effects).

    Args:
        pw: An active ``sync_playwright`` context (the value bound by
            ``with sync_playwright() as pw``).

    Returns:
        A launched Chromium ``Browser`` instance.

    Raises:
        Exception: Re-raises the original launch error if the browser is missing
            and auto-install is disabled, in use of a channel, or itself fails.
    """
    global _CHROMIUM_INSTALL_ATTEMPTED

    channel = os.environ.get("AGY_UI_CHROME_CHANNEL")
    executable = os.environ.get("AGY_UI_CHROME_EXECUTABLE")

    launch_kwargs: dict[str, object] = {"headless": True}
    if channel:
        launch_kwargs["channel"] = channel
    if executable:
        launch_kwargs["executable_path"] = executable

    try:
        return pw.chromium.launch(**launch_kwargs)
    except Exception as exc:
        # Only the "browser binary missing" case is recoverable by installing the
        # bundled Chromium. A channel points at a *system* browser, and an
        # explicit executable_path points at a user-specified binary, so in
        # either case installing Chromium would not help (the retry would just
        # reuse the same bad path) and the error must propagate to the caller.
        autoinstall_disabled = bool(
            os.environ.get("AGY_UI_NO_BROWSER_AUTOINSTALL")
        ) or bool(os.environ.get("AGY_UI_DRY_RUN"))
        message = str(exc).lower()
        is_missing_browser = any(
            marker in message for marker in _MISSING_BROWSER_MARKERS
        )
        if (
            not is_missing_browser
            or bool(channel)
            or bool(executable)
            or autoinstall_disabled
            or _CHROMIUM_INSTALL_ATTEMPTED
        ):
            raise

        _CHROMIUM_INSTALL_ATTEMPTED = True
        print(
            "agy-ui-mcp: installing the Chromium browser for the first time, "
            "this is a one-time setup...",
            file=sys.stderr,
        )
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            check=False,
        )
        # Retry exactly once; if it still fails, let that error propagate.
        return pw.chromium.launch(**launch_kwargs)


def wait_ready(url: str, timeout: int = 60) -> bool:
    """Poll ``url`` until it responds or ``timeout`` seconds elapse.

    Uses ``urllib`` (no browser needed) so the dev server's readiness can be
    checked cheaply before launching Chromium.

    Args:
        url: The base URL the dev server should be serving.
        timeout: Maximum seconds to wait.

    Returns:
        True once the URL responds with any HTTP status; False on timeout.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5):  # noqa: S310
                return True
        except urllib.error.HTTPError:
            # Any HTTP response (even 404) means the server is up.
            return True
        except (urllib.error.URLError, ConnectionError, OSError):
            time.sleep(0.5)
    return False


def _run_off_event_loop(fn, *args, **kwargs):
    """Run a sync (Playwright) callable off any running asyncio loop.

    Playwright's sync API refuses to run inside a live asyncio loop. When the
    caller is on the loop thread (e.g. an MCP tool awaited by FastMCP), run fn
    in a worker thread that has no running loop; otherwise call it directly so
    non-async callers and tests are unaffected.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return fn(*args, **kwargs)
    with ThreadPoolExecutor(max_workers=1) as _ex:
        return _ex.submit(fn, *args, **kwargs).result()


def capture(*args, **kwargs):
    return _run_off_event_loop(_capture_impl, *args, **kwargs)


def _capture_impl(
    url: str,
    viewport_width: int,
    out_path: str | Path,
    *,
    height: int = DEFAULT_HEIGHT,
    full_page: bool = True,
    settle_ms: int = 1500,
) -> str:
    """Capture a PNG screenshot of ``url`` at a viewport width to ``out_path``.

    Navigation is resilient to apps that never reach ``networkidle`` (CDN
    fonts, analytics beacons, long-poll/websocket connections): it first tries
    ``wait_until="networkidle"`` with a bounded timeout and falls back to
    ``wait_until="load"`` if that times out. After navigation it waits
    ``settle_ms`` so client-side renderers (Babel/React) finish painting before
    the shot is taken.

    Args:
        url: The page URL to capture (the dev server must already be serving).
        viewport_width: Viewport width in pixels.
        out_path: Destination PNG file path. Parent dirs are created.
        height: Viewport height in pixels (defaults to :data:`DEFAULT_HEIGHT`).
        full_page: Capture the full scrollable page when True.
        settle_ms: Extra milliseconds to wait after navigation for client-side
            rendering to settle before capturing.

    Returns:
        The absolute path written, as a string.

    Raises:
        PlaywrightUnavailableError: If Playwright/browsers are unavailable.
    """
    sync_playwright = _import_sync_playwright()
    from playwright.sync_api import TimeoutError as PWTimeout  # type: ignore

    dest = Path(out_path)
    dest.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        browser = _launch_chromium(pw)
        try:
            page = browser.new_page(
                viewport={"width": viewport_width, "height": height}
            )
            try:
                page.goto(url, wait_until="networkidle", timeout=15000)
            except PWTimeout:
                # Some apps keep a connection open forever; settle for "load".
                page.goto(url, wait_until="load", timeout=15000)
            # Give client-side renderers (Babel/React) time to paint.
            page.wait_for_timeout(settle_ms)
            page.screenshot(path=str(dest), full_page=full_page, type="png")
        finally:
            browser.close()

    return str(dest.resolve())


def capture_viewports(
    url: str,
    viewports: list[int],
    out_dir: str | Path,
    *,
    height: int = DEFAULT_HEIGHT,
    full_page: bool = True,
    prefix: str = "shot",
    settle_ms: int = 1500,
) -> list[str]:
    """Capture ``url`` across several viewport widths into ``out_dir``.

    Args:
        url: The page URL to capture.
        viewports: Viewport widths (px) to iterate over.
        out_dir: Directory to write the PNG files into (created if missing).
        height: Viewport height for each capture.
        full_page: Capture the full scrollable page when True.
        prefix: Filename prefix; files are named ``<prefix>-<width>.png``.
        settle_ms: Extra milliseconds to wait after navigation for client-side
            rendering to settle before capturing (passed to :func:`capture`).

    Returns:
        Absolute paths of the written PNGs, in ``viewports`` order.

    Raises:
        PlaywrightUnavailableError: If Playwright/browsers are unavailable.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths: list[str] = []
    for width in viewports:
        dest = out / f"{prefix}-{width}.png"
        paths.append(
            capture(
                url,
                width,
                dest,
                height=height,
                full_page=full_page,
                settle_ms=settle_ms,
            )
        )
    return paths


def _build_context_kwargs(target: "Target", devices: dict[str, "Device"], pw) -> dict:
    """Resolve ``browser.new_context`` kwargs for a target.

    Device/viewport metrics must be set on the *context* (here), before the
    page navigates, so the first paint already reflects the emulated device.

    Resolution order:
        1. ``target.device`` -> a named registry device: spread
           ``pw.devices[name]`` (viewport, DPR, UA, touch all included).
        2. ``target.device`` -> an explicit device: build ``viewport`` from
           its width/height plus DPR/mobile/touch/UA (omitting None fields).
        3. ``target.viewport_width``: a ``{width, 900}`` viewport.
        4. Fallback: a ``{1440, 900}`` desktop viewport.

    ``reduced_motion="reduce"`` is added when ``target.reduce_motion`` is set.

    Args:
        target: The capture target.
        devices: The scope's named device registry.
        pw: The live ``sync_playwright`` instance (for ``pw.devices``).

    Returns:
        A kwargs dict suitable for ``browser.new_context(**kwargs)``.
    """
    ctx_kwargs: dict = {}
    device = devices.get(target.device) if target.device else None

    if device is not None and device.name:
        # Named registry device: copy its full descriptor.
        ctx_kwargs = dict(pw.devices[device.name])
    elif device is not None and device.width and device.height:
        ctx_kwargs["viewport"] = {"width": device.width, "height": device.height}
        ctx_kwargs["device_scale_factor"] = device.device_scale_factor
        ctx_kwargs["is_mobile"] = device.is_mobile
        ctx_kwargs["has_touch"] = device.has_touch
        if device.user_agent is not None:
            ctx_kwargs["user_agent"] = device.user_agent
    elif target.viewport_width:
        ctx_kwargs["viewport"] = {
            "width": target.viewport_width,
            "height": DEFAULT_HEIGHT,
        }
    else:
        ctx_kwargs["viewport"] = {"width": 1440, "height": DEFAULT_HEIGHT}

    if target.reduce_motion:
        ctx_kwargs["reduced_motion"] = "reduce"

    return ctx_kwargs


def _run_pre_step(page, step: "PreStep") -> str | None:  # type: ignore[name-defined]
    """Execute one pre-step against ``page``; return a warning string on error.

    Each action is best-effort: a failure is converted into a human-readable
    warning rather than raised, so one bad selector can't abort the capture.
    """
    try:
        sel = step.selector
        if step.action == "click":
            page.click(sel, timeout=step.timeout_ms)
        elif step.action == "hover":
            page.hover(sel, timeout=step.timeout_ms)
        elif step.action == "focus":
            page.focus(sel, timeout=step.timeout_ms)
        elif step.action == "fill":
            page.fill(sel, step.value or "", timeout=step.timeout_ms)
        elif step.action == "press":
            page.press(sel, step.value or "", timeout=step.timeout_ms)
        elif step.action == "wait_for":
            page.wait_for_selector(sel, state=step.state, timeout=step.timeout_ms)
        elif step.action == "set_attribute":
            page.evaluate(
                "([s, a, v]) => document.querySelector(s)"
                ".setAttribute(a, v)",
                [sel, step.attr, step.value or ""],
            )
        return None
    except Exception as exc:  # noqa: BLE001 - non-fatal: report and continue
        return f"pre_step {step.action}({step.selector!r}) failed: {exc}"


def _local_storage_init_script(local_storage: dict[str, str]) -> str:
    """Build a JS init script that seeds ``localStorage`` before page scripts.

    Each key/value is embedded via :func:`json.dumps` so arbitrary strings
    (quotes, newlines, JSON payloads) are escaped safely and cannot break out
    of the script. Run as an *init* script, the writes land before the app's
    own scripts execute, so first render already sees the seeded data state.

    Args:
        local_storage: Key/value pairs to write into ``window.localStorage``.

    Returns:
        A JavaScript source string suitable for ``context.add_init_script``.
    """
    lines = ["try {"]
    for key, value in local_storage.items():
        lines.append(
            f"  window.localStorage.setItem({json.dumps(key)}, {json.dumps(value)});"
        )
    lines.append("} catch (e) {}")
    return "\n".join(lines)


def _emulate_media_kwargs(target: "Target") -> dict:
    """Collect non-None ``page.emulate_media`` kwargs for a target.

    Maps the target's media-related fields onto Playwright's
    ``emulate_media`` parameters, omitting any that are unset so Playwright
    keeps its own defaults. ``reduced_motion`` is always derived from
    ``target.reduce_motion`` (``"reduce"`` vs ``"no-preference"``).

    Args:
        target: The capture target.

    Returns:
        A kwargs dict for ``page.emulate_media(**kwargs)`` (possibly only the
        always-present ``reduced_motion`` entry).
    """
    kwargs: dict = {}
    if target.media is not None:
        kwargs["media"] = target.media
    if target.color_scheme is not None:
        kwargs["color_scheme"] = target.color_scheme
    if target.forced_colors is not None:
        kwargs["forced_colors"] = target.forced_colors
    kwargs["reduced_motion"] = "reduce" if target.reduce_motion else "no-preference"
    return kwargs


def capture_target(*args, **kwargs):
    return _run_off_event_loop(_capture_target_impl, *args, **kwargs)


def _capture_target_impl(
    base_url: str,
    target: "Target",
    devices: dict[str, "Device"],
    out_path: str | Path,
    *,
    settle_ms: int = 1500,
) -> tuple[str, list[str]]:
    """Capture one :class:`Target` (route x device x theme x state) to a PNG.

    Unlike :func:`capture`, the device/viewport is established on the browser
    *context* before navigation, then theme/RTL/pre-steps are applied so the
    captured frame reflects the intended component state. Non-fatal problems
    (a failed pre-step, a theme/RTL eval error) are collected and returned as
    warnings rather than raised.

    Additional media/state emulation is layered on:
      * ``target.local_storage`` is seeded via a context init script *before*
        navigation, so the app's first render sees the desired data state.
      * ``page.emulate_media`` applies ``media`` (screen/print),
        ``color_scheme`` (``prefers-color-scheme``), ``forced_colors``
        (high-contrast), and ``reduced_motion`` (from ``reduce_motion``).
      * the screenshot honors ``target.full_page`` for tall pages.
    Any emulate/seed failure is appended as a warning (never fatal).

    Args:
        base_url: The dev server base URL (``target.route`` is appended).
        target: The capture target describing route/device/theme/state.
        devices: The scope's named device registry (for ``target.device``).
        out_path: Destination PNG path. Parent dirs are created.
        settle_ms: Milliseconds to wait after navigation for client rendering.

    Returns:
        A ``(path, warnings)`` tuple: the absolute PNG path written and a list
        of non-fatal warning strings (empty when everything succeeded).

    Raises:
        PlaywrightUnavailableError: If Playwright/browsers are unavailable.
    """
    sync_playwright = _import_sync_playwright()
    from playwright.sync_api import TimeoutError as PWTimeout  # type: ignore

    dest = Path(out_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []

    route = target.route or ""
    if route:
        url = base_url.rstrip("/") + "/" + route.lstrip("/")
    else:
        url = base_url

    with sync_playwright() as pw:
        ctx_kwargs = _build_context_kwargs(target, devices, pw)
        browser = _launch_chromium(pw)
        try:
            context = browser.new_context(**ctx_kwargs)

            # Seed localStorage before any page script runs so the first render
            # already reflects the desired data state (empty/long/error).
            if target.local_storage:
                try:
                    context.add_init_script(
                        _local_storage_init_script(target.local_storage)
                    )
                except Exception as exc:  # noqa: BLE001 - non-fatal
                    warnings.append(f"local_storage seed failed: {exc}")

            page = context.new_page()

            # Emulate media/preferences (print, prefers-color-scheme,
            # forced-colors, reduced-motion). Best-effort: a failure here must
            # not abort the capture.
            try:
                page.emulate_media(**_emulate_media_kwargs(target))
            except Exception as exc:  # noqa: BLE001 - non-fatal
                warnings.append(f"emulate_media failed: {exc}")

            try:
                page.goto(url, wait_until="networkidle", timeout=15000)
            except PWTimeout:
                # Some apps keep a connection open forever; settle for "load".
                page.goto(url, wait_until="load", timeout=15000)
            page.wait_for_timeout(settle_ms)

            if target.rtl:
                try:
                    page.evaluate(
                        "document.documentElement.setAttribute('dir', 'rtl')"
                    )
                except Exception as exc:  # noqa: BLE001 - non-fatal
                    warnings.append(f"rtl apply failed: {exc}")

            if target.theme:
                try:
                    page.evaluate(
                        f"document.querySelector({json.dumps(target.theme_selector)})"
                        f".setAttribute({json.dumps(target.theme_attr)}, "
                        f"{json.dumps(target.theme)})"
                    )
                    page.wait_for_timeout(200)
                except Exception as exc:  # noqa: BLE001 - non-fatal
                    warnings.append(f"theme apply failed: {exc}")

            for step in target.pre_steps:
                warn = _run_pre_step(page, step)
                if warn:
                    warnings.append(warn)

            page.wait_for_timeout(300)
            page.screenshot(
                path=str(dest), full_page=target.full_page, type="png"
            )
        finally:
            browser.close()

    return str(dest.resolve()), warnings


#: The in-page axe-core invocation: run against the document, keep only
#: violations, and compact each (with up to 5 offending nodes) so the payload
#: returned to Python stays small. axe.run resolves a Promise, which sync
#: Playwright awaits.
_AXE_RUN_JS: Final[str] = """
async () => {
  const r = await axe.run(document, { resultTypes: ['violations'] });
  return r.violations.map(v => ({
    id: v.id,
    impact: v.impact,
    help: v.help,
    helpUrl: v.helpUrl,
    tags: v.tags,
    count: v.nodes.length,
    nodes: v.nodes.slice(0, 5).map(n => ({
      target: n.target,
      failureSummary: n.failureSummary,
    })),
  }));
}
"""


def audit_a11y(*args, **kwargs):
    return _run_off_event_loop(_audit_a11y_impl, *args, **kwargs)


def _audit_a11y_impl(
    base_url: str,
    target: "Target",
    devices: dict[str, "Device"],
    *,
    settle_ms: int = 1500,
) -> tuple[list[dict], list[str]]:
    """Run axe-core against one web :class:`Target` and return its violations.

    Navigates and emulates the target exactly like :func:`capture_target` (same
    device/context, ``local_storage`` seed, ``emulate_media``, RTL/theme,
    ``pre_steps``) so the audited DOM is the same one that would be screenshotted
    — then injects the vendored axe-core bundle and runs it. This is **web-only**:
    native captures have no DOM and never reach here.

    Every failure is non-fatal and surfaced as a warning (a missing axe bundle,
    a navigation timeout, or an axe runtime error) so an a11y audit can never
    break the surrounding review.

    Args:
        base_url: The dev server base URL (``target.route`` is appended).
        target: The capture target describing route/device/theme/state.
        devices: The scope's named device registry (for ``target.device``).
        settle_ms: Milliseconds to wait after navigation before auditing.

    Returns:
        A ``(violations, warnings)`` tuple. ``violations`` is a list of compact
        dicts ``{id, impact, help, helpUrl, tags, count, nodes:[{target,
        failureSummary}]}`` (empty when the page is clean or the audit was
        skipped); ``warnings`` collects non-fatal problems.

    Raises:
        PlaywrightUnavailableError: If Playwright/browsers are unavailable.
    """
    warnings: list[str] = []
    axe_js = _load_axe_js()
    if axe_js is None:
        return [], [
            "axe-core bundle not found (vendor/axe.min.js); a11y audit skipped."
        ]

    route = target.route or ""
    if route:
        url = base_url.rstrip("/") + "/" + route.lstrip("/")
    else:
        url = base_url

    violations: list[dict] = []
    # The ENTIRE Playwright session is wrapped: browser launch, context/navigation
    # and the fallback `goto(... wait_until="load")` can all raise, and ui_review
    # calls this directly — so an a11y failure must degrade to a warning, never
    # abort the review ("a11y can never break the review").
    try:
        sync_playwright = _import_sync_playwright()
        from playwright.sync_api import TimeoutError as PWTimeout  # type: ignore

        with sync_playwright() as pw:
            ctx_kwargs = _build_context_kwargs(target, devices, pw)
            browser = _launch_chromium(pw)
            try:
                context = browser.new_context(**ctx_kwargs)
                if target.local_storage:
                    try:
                        context.add_init_script(
                            _local_storage_init_script(target.local_storage)
                        )
                    except Exception as exc:  # noqa: BLE001 - non-fatal
                        warnings.append(f"local_storage seed failed: {exc}")

                page = context.new_page()
                try:
                    page.emulate_media(**_emulate_media_kwargs(target))
                except Exception as exc:  # noqa: BLE001 - non-fatal
                    warnings.append(f"emulate_media failed: {exc}")

                try:
                    page.goto(url, wait_until="networkidle", timeout=15000)
                except PWTimeout:
                    page.goto(url, wait_until="load", timeout=15000)
                page.wait_for_timeout(settle_ms)

                if target.rtl:
                    try:
                        page.evaluate(
                            "document.documentElement.setAttribute('dir', 'rtl')"
                        )
                    except Exception as exc:  # noqa: BLE001 - non-fatal
                        warnings.append(f"rtl apply failed: {exc}")
                if target.theme:
                    try:
                        page.evaluate(
                            f"document.querySelector({json.dumps(target.theme_selector)})"
                            f".setAttribute({json.dumps(target.theme_attr)}, "
                            f"{json.dumps(target.theme)})"
                        )
                        page.wait_for_timeout(200)
                    except Exception as exc:  # noqa: BLE001 - non-fatal
                        warnings.append(f"theme apply failed: {exc}")
                for step in target.pre_steps:
                    warn = _run_pre_step(page, step)
                    if warn:
                        warnings.append(warn)
                page.wait_for_timeout(300)

                try:
                    page.add_script_tag(content=axe_js)
                    result = page.evaluate(_AXE_RUN_JS)
                    if isinstance(result, list):
                        violations = result
                except Exception as exc:  # noqa: BLE001 - axe failure is non-fatal
                    warnings.append(f"axe-core run failed: {exc}")
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001 - a11y must NEVER break the review
        warnings.append(f"a11y audit skipped (Playwright/setup error): {exc}")

    return violations, warnings


def summarize_a11y(violations: list[dict]) -> str:
    """Render axe violations as a compact, prompt-friendly text block.

    Used to ground an agy review in concrete WCAG findings. Returns a short
    "no violations" line when the list is empty.
    """
    if not violations:
        return "axe-core: no accessibility violations detected."
    order = {"critical": 0, "serious": 1, "moderate": 2, "minor": 3}
    ordered = sorted(
        violations, key=lambda v: order.get((v.get("impact") or "minor"), 4)
    )
    lines = [f"axe-core found {len(ordered)} violation rule(s):"]
    for v in ordered:
        impact = v.get("impact") or "n/a"
        count = v.get("count", len(v.get("nodes") or []))
        lines.append(
            f"- [{impact}] {v.get('id')}: {v.get('help')} "
            f"({count} element(s)) {v.get('helpUrl', '')}".rstrip()
        )
        for node in (v.get("nodes") or [])[:3]:
            sel = node.get("target")
            sel_txt = ", ".join(sel) if isinstance(sel, list) else str(sel)
            lines.append(f"    · {sel_txt}")
    return "\n".join(lines)


class SimulatorAdapter:
    """Native iOS Simulator screenshotter driven by ``xcrun simctl``.

    Web-targets are captured by Playwright via :func:`capture_target`. True
    native apps cannot be reached over HTTP, so this adapter instead shells out
    to Apple's simulator tooling. It only owns two responsibilities — **boot**
    the target simulator and **screenshot** it:

    * resolve the target :class:`~agy_ui_mcp.scope.Device` to a simulator UDID
      (``device.udid`` directly, else ``device.name`` looked up via
      ``xcrun simctl list devices available --json``),
    * ensure that simulator is booted (``xcrun simctl bootstatus <udid> -b``),
    * capture a PNG (``xcrun simctl io <udid> screenshot <out.png>``).

    Building and launching the app *into* the simulator is **not** this
    adapter's job — that is handled externally by the scope's ``serve.cmd``
    (e.g. ``flutter run -d <udid>``) before any capture runs. Likewise device
    emulation, routing, and component-state driving (the things
    :class:`~agy_ui_mcp.scope.Target` expresses for web-targets) have no direct
    equivalent for native captures and are ignored here.

    Android emulators are handled by the parallel :class:`AndroidAdapter`
    (``adb``); ``capture_for_platform`` dispatches ``android-emu`` there.
    """

    @staticmethod
    def _run(args: list[str]) -> subprocess.CompletedProcess:
        """Run ``args`` via :func:`subprocess.run`, capturing output.

        ``check=False`` so callers can inspect ``returncode``/``stderr`` and
        decide whether a non-zero exit is fatal (a failed screenshot) or benign
        (an "already booted" boot). All shell-out goes through this single seam,
        which tests monkeypatch to stay fully offline.

        Args:
            args: The argv list to execute (e.g. ``["xcrun", "simctl", ...]``).

        Returns:
            The completed process, with ``stdout``/``stderr`` as text.
        """
        return subprocess.run(args, capture_output=True, text=True, check=False)

    def _resolve_udid(self, device: "Device") -> str:
        """Resolve a :class:`Device` to a concrete iOS Simulator UDID.

        Resolution order:
            1. ``device.udid`` — returned as-is (no ``simctl`` call).
            2. ``device.name`` — ``xcrun simctl list devices available --json``
               is parsed and every runtime is scanned for a device whose
               ``name`` matches. A "Booted" match wins over the first available
               one (so an already-running simulator is reused).

        Args:
            device: The native device to resolve.

        Returns:
            The simulator UDID string.

        Raises:
            RuntimeError: If neither ``udid`` nor a resolvable ``name`` is set,
                if the ``simctl list`` call fails, or if no simulator matches
                ``name``.
        """
        if device.udid:
            return device.udid
        if not device.name:
            raise RuntimeError(
                "Native (ios-sim) device must set `udid` or `name` to identify "
                "the simulator to screenshot."
            )

        proc = self._run(
            ["xcrun", "simctl", "list", "devices", "available", "--json"]
        )
        if proc.returncode != 0:
            raise RuntimeError(
                "`xcrun simctl list devices` failed "
                f"(exit {proc.returncode}): {proc.stderr.strip()}"
            )
        try:
            data = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Could not parse `xcrun simctl list` JSON output: {exc}"
            ) from exc

        fallback_udid: str | None = None
        for entries in (data.get("devices") or {}).values():
            for entry in entries:
                if entry.get("name") != device.name:
                    continue
                udid = entry.get("udid")
                if not udid:
                    continue
                if entry.get("state") == "Booted":
                    return udid
                if fallback_udid is None:
                    fallback_udid = udid

        if fallback_udid is not None:
            return fallback_udid

        raise RuntimeError(
            f"No available iOS simulator named {device.name!r} was found. "
            "List them with `xcrun simctl list devices available`."
        )

    def _ensure_booted(self, udid: str) -> None:
        """Boot the simulator ``udid`` (if needed) and wait until it is booted.

        Uses ``xcrun simctl bootstatus <udid> -b`` which boots the device when
        it is shut down and blocks until it has finished booting. An
        "already booted" condition (the device was running already) is not an
        error and is swallowed; any other non-zero exit is fatal.

        Args:
            udid: The simulator UDID to boot.

        Raises:
            RuntimeError: If booting fails for a reason other than the device
                already being booted.
        """
        proc = self._run(["xcrun", "simctl", "bootstatus", udid, "-b"])
        if proc.returncode == 0:
            return
        combined = f"{proc.stdout}\n{proc.stderr}".lower()
        if "already booted" in combined or "current state: booted" in combined:
            return
        raise RuntimeError(
            f"Failed to boot iOS simulator {udid!r} "
            f"(exit {proc.returncode}): {proc.stderr.strip()}"
        )

    def capture(
        self,
        target: "Target",
        devices: dict[str, "Device"],
        out_path: str | Path,
    ) -> tuple[str, list[str]]:
        """Boot the target's simulator and screenshot it to ``out_path``.

        The capture is purely native: ``base_url``/routing/``settle_ms`` and the
        web-only emulation fields on ``target`` do not apply. The flow is
        resolve-UDID -> ensure-booted -> ``simctl io ... screenshot``.

        Args:
            target: The capture target; its ``device`` keys into ``devices``.
            devices: The scope's named device registry.
            out_path: Destination PNG path. Parent dirs are created.

        Returns:
            A ``(path, warnings)`` tuple: the absolute PNG path written and a
            list of non-fatal warning strings (empty on a clean capture).

        Raises:
            RuntimeError: If ``target.device`` is unset/unknown, the simulator
                cannot be resolved/booted, or the screenshot command fails.
        """
        if not target.device:
            raise RuntimeError(
                f"Target {target.name!r} has no `device`; a native (ios-sim) "
                "capture must reference a device that identifies the simulator "
                "(by `udid` or `name`)."
            )
        try:
            device = devices[target.device]
        except KeyError as exc:
            raise RuntimeError(
                f"Target {target.name!r} references unknown device "
                f"{target.device!r}; not found in the scope's `devices`."
            ) from exc

        dest = Path(out_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        warnings: list[str] = []

        udid = self._resolve_udid(device)
        self._ensure_booted(udid)

        proc = self._run(
            ["xcrun", "simctl", "io", udid, "screenshot", "--type=png", str(dest)]
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"`xcrun simctl io {udid} screenshot` failed "
                f"(exit {proc.returncode}): {proc.stderr.strip()}"
            )

        return str(dest.resolve()), warnings


#: Process-wide adapter instance reused for native iOS captures (stateless).
_SIMULATOR_ADAPTER: Final[SimulatorAdapter] = SimulatorAdapter()


def _resolve_adb() -> str:
    """Locate the ``adb`` executable.

    Resolution order: ``$ADB`` override, ``adb`` on ``PATH``, then the
    ``platform-tools/adb`` under ``$ANDROID_HOME`` / ``$ANDROID_SDK_ROOT`` /
    the default macOS SDK location ``~/Library/Android/sdk``.

    Returns:
        An absolute path to ``adb`` (or the bare name ``"adb"`` when it is on
        ``PATH``).

    Raises:
        RuntimeError: If no ``adb`` can be found.
    """
    override = os.environ.get("ADB")
    if override and Path(override).exists():
        return override
    on_path = shutil.which("adb")
    if on_path:
        return on_path
    roots = [
        os.environ.get("ANDROID_HOME"),
        os.environ.get("ANDROID_SDK_ROOT"),
        str(Path.home() / "Library/Android/sdk"),
        str(Path.home() / "Android/Sdk"),
    ]
    for root in roots:
        if not root:
            continue
        candidate = Path(root) / "platform-tools" / "adb"
        if candidate.exists():
            return str(candidate)
    raise RuntimeError(
        "`adb` not found. Install Android platform-tools, or set $ADB / "
        "$ANDROID_HOME so the android-emu adapter can locate it."
    )


def _resolve_emulator() -> str:
    """Locate the Android ``emulator`` executable (for auto-launching an AVD).

    Resolution order: ``$ANDROID_EMULATOR`` override, ``emulator`` on ``PATH``,
    then ``emulator/emulator`` under ``$ANDROID_HOME`` / ``$ANDROID_SDK_ROOT`` /
    the default SDK locations.

    Raises:
        RuntimeError: If no ``emulator`` binary can be found.
    """
    override = os.environ.get("ANDROID_EMULATOR")
    if override and Path(override).exists():
        return override
    on_path = shutil.which("emulator")
    if on_path:
        return on_path
    roots = [
        os.environ.get("ANDROID_HOME"),
        os.environ.get("ANDROID_SDK_ROOT"),
        str(Path.home() / "Library/Android/sdk"),
        str(Path.home() / "Android/Sdk"),
    ]
    for root in roots:
        if not root:
            continue
        candidate = Path(root) / "emulator" / "emulator"
        if candidate.exists():
            return str(candidate)
    raise RuntimeError(
        "Android `emulator` binary not found, so no emulator could be "
        "auto-launched. Start one manually (`emulator -avd <name>`) or set "
        "$ANDROID_HOME / $ANDROID_EMULATOR."
    )


class AndroidAdapter:
    """Native Android emulator screenshotter driven by ``adb``.

    The mirror of :class:`SimulatorAdapter` for ``android-emu``. It owns only
    **resolve serial -> ensure booted -> screenshot**; building/launching the
    Flutter app onto the emulator is the scope's ``serve.cmd``
    (``flutter run -d <serial>``), driven by the server's pty hot-reload loop.

    * resolve the target :class:`~agy_ui_mcp.scope.Device` to an adb serial:
      ``device.udid`` is treated as the serial directly (e.g. ``emulator-5554``);
      otherwise ``device.name`` is matched against the AVD name of each running
      emulator (``adb -s <serial> emu avd name``),
    * ensure the device has finished booting (``adb wait-for-device`` then poll
      ``getprop sys.boot_completed`` == ``1``),
    * capture a PNG (``adb -s <serial> exec-out screencap -p`` -> stdout bytes;
      ``exec-out`` avoids the CRLF mangling of a plain ``shell screencap``).

    When the target device is identified by AVD ``name`` and no matching
    emulator is running, the adapter **auto-launches** it
    (``emulator -avd <name>``) and waits for it to attach — the parallel to
    iOS's ``simctl bootstatus -b`` auto-boot. The launched emulator is a
    long-lived detached process left running for the rest of the session (the
    caller/user owns shutting it down, e.g. ``adb -s <serial> emu kill``).
    Web-only :class:`~agy_ui_mcp.scope.Target` fields (route/theme/state) have no
    native equivalent and are ignored.
    """

    @staticmethod
    def _run(
        args: list[str], *, binary: bool = False, timeout: float | None = None
    ) -> subprocess.CompletedProcess:
        """Run ``[adb, *args]`` capturing output; the single shell-out seam.

        ``check=False`` so callers inspect ``returncode``/``stderr``. With
        ``binary=True`` stdout is kept as raw bytes (for ``screencap`` PNG data);
        otherwise it is decoded as text. ``timeout`` bounds blocking calls
        (notably ``adb wait-for-device``, which otherwise hangs forever on a
        device stuck offline) and surfaces as :class:`subprocess.TimeoutExpired`.
        Tests monkeypatch this to stay offline.
        """
        adb = _resolve_adb()
        return subprocess.run(
            [adb, *args],
            capture_output=True,
            text=not binary,
            check=False,
            timeout=timeout,
        )

    def _running_serials(self) -> list[str]:
        """Return the serials of currently-attached devices (``adb devices``)."""
        proc = self._run(["devices"])
        if proc.returncode != 0:
            raise RuntimeError(
                f"`adb devices` failed (exit {proc.returncode}): "
                f"{proc.stderr.strip()}"
            )
        serials: list[str] = []
        for line in (proc.stdout or "").splitlines()[1:]:  # skip the header line
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                serials.append(parts[0])
        return serials

    def _avd_name(self, serial: str) -> str | None:
        """Return the AVD name backing a running emulator serial, or None."""
        proc = self._run(["-s", serial, "emu", "avd", "name"])
        if proc.returncode != 0:
            return None
        # Output is the AVD name on the first line, then "OK".
        for line in (proc.stdout or "").splitlines():
            line = line.strip()
            if line and line != "OK":
                return line
        return None

    def _attached_serials(self) -> list[str]:
        """Return every attached serial (``device`` *and* ``offline`` states).

        Unlike :meth:`_running_serials` (which keeps only fully-connected
        ``device`` entries), this includes still-booting emulators so a freshly
        launched one is detected as soon as it appears in ``adb devices``.
        """
        proc = self._run(["devices"])
        serials: list[str] = []
        for line in (proc.stdout or "").splitlines()[1:]:  # skip header
            parts = line.split()
            if len(parts) >= 2 and parts[0]:
                serials.append(parts[0])
        return serials

    def _launch_emulator(self, avd_name: str, timeout: int = 180) -> str:
        """Launch ``emulator -avd <avd_name>`` and return its new adb serial.

        Detached, long-lived process (left running for the session). Polls
        ``adb devices`` until a serial appears that was not attached before the
        launch, then returns it; ``_ensure_booted`` later waits for full boot.

        Raises:
            RuntimeError: If the emulator binary is missing, the process exits
                before a device attaches, or none appears within ``timeout``.
        """
        emu = _resolve_emulator()
        before = set(self._attached_serials())
        proc = subprocess.Popen(
            [emu, "-avd", avd_name, "-no-snapshot-save", "-no-boot-anim", "-no-audio"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            # Only bind to a serial that is (a) new since the launch, (b) fully
            # connected (`device` state, not `offline`), and (c) actually backed
            # by the AVD we launched (or whose name we cannot read yet). This
            # prevents binding to a concurrently-attached different emulator or a
            # physical device that happens to appear during the wait.
            new = sorted(set(self._running_serials()) - before)
            for serial in new:
                name = self._avd_name(serial)
                if name == avd_name or name is None:
                    return serial
                # A different emulator attached concurrently — ignore it and keep
                # waiting for ours (do not re-pick it next loop).
                before.add(serial)
            if proc.poll() is not None:
                raise RuntimeError(
                    f"`emulator -avd {avd_name}` exited (code {proc.returncode}) "
                    f"before a device attached. Check the AVD name "
                    f"(`emulator -list-avds`)."
                )
            time.sleep(2)
        raise RuntimeError(
            f"AVD {avd_name!r} did not attach to adb within {timeout}s of launch."
        )

    def _resolve_serial(self, device: "Device") -> str:
        """Resolve a :class:`Device` to an adb serial, auto-launching if needed.

        Resolution order:
            1. ``device.udid`` — used as the serial directly (no ``adb`` call).
            2. ``device.name`` — matched against the AVD name of each running
               emulator. If none matches and exactly one emulator is running
               whose AVD name is *unreadable*, that one is used (best-effort).
               Otherwise the named AVD is **auto-launched** and its serial
               returned.

        Raises:
            RuntimeError: If neither ``udid`` nor ``name`` is set, or the named
                AVD cannot be launched.
        """
        if device.udid:
            return device.udid
        if not device.name:
            raise RuntimeError(
                "Native (android-emu) device must set `udid` (adb serial, e.g. "
                "'emulator-5554') or `name` (the AVD name) to identify the "
                "emulator to screenshot."
            )
        running = self._running_serials()
        unreadable: list[str] = []
        for serial in running:
            name = self._avd_name(serial)
            if name == device.name:
                return serial
            if name is None:
                unreadable.append(serial)
        if len(running) == 1 and unreadable:
            # Single running emulator whose AVD name we could not read: trust it
            # rather than launching a duplicate.
            return running[0]
        # No running emulator is the requested AVD: launch it.
        return self._launch_emulator(device.name)

    def _ensure_booted(self, serial: str, timeout: int = 120) -> None:
        """Block until ``serial`` finishes booting (``sys.boot_completed`` == 1).

        ``adb wait-for-device`` returns as soon as the device is *attached* (not
        fully booted), so we additionally poll ``getprop sys.boot_completed``.
        Both adb calls are TIME-BOUNDED: ``wait-for-device`` blocks forever on a
        device stuck ``offline``, which would hang the whole run before the
        Python deadline below is ever consulted.

        Raises:
            RuntimeError: If the device never attaches or reports boot completion
                in time.
        """
        deadline = time.monotonic() + timeout
        try:
            self._run(["-s", serial, "wait-for-device"], timeout=timeout)
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"Android emulator {serial!r} never came online "
                f"(`adb wait-for-device` timed out after {timeout}s; device may "
                f"be stuck offline)."
            ) from None
        while time.monotonic() < deadline:
            try:
                proc = self._run(
                    ["-s", serial, "shell", "getprop", "sys.boot_completed"],
                    timeout=10,
                )
            except subprocess.TimeoutExpired:  # adb wedged; retry until deadline
                time.sleep(2)
                continue
            if proc.returncode == 0 and (proc.stdout or "").strip() == "1":
                return
            time.sleep(2)
        raise RuntimeError(
            f"Android emulator {serial!r} did not finish booting within "
            f"{timeout}s (sys.boot_completed != 1)."
        )

    def capture(
        self,
        target: "Target",
        devices: dict[str, "Device"],
        out_path: str | Path,
    ) -> tuple[str, list[str]]:
        """Resolve+boot the target's emulator and screenshot it to ``out_path``.

        Native capture: ``base_url``/routing/``settle_ms`` and the web-only
        emulation fields on ``target`` do not apply. Flow is resolve-serial ->
        ensure-booted -> ``adb exec-out screencap -p`` -> write PNG bytes.

        Returns:
            A ``(path, warnings)`` tuple: the absolute PNG path written and a
            list of non-fatal warning strings (empty on a clean capture).

        Raises:
            RuntimeError: If ``target.device`` is unset/unknown, the emulator
                cannot be resolved/booted, or the screencap command fails.
        """
        if not target.device:
            raise RuntimeError(
                f"Target {target.name!r} has no `device`; a native (android-emu) "
                "capture must reference a device identifying the emulator "
                "(by `udid` serial or AVD `name`)."
            )
        try:
            device = devices[target.device]
        except KeyError as exc:
            raise RuntimeError(
                f"Target {target.name!r} references unknown device "
                f"{target.device!r}; not found in the scope's `devices`."
            ) from exc

        dest = Path(out_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        warnings: list[str] = []

        serial = self._resolve_serial(device)
        self._ensure_booted(serial)

        proc = self._run(["-s", serial, "exec-out", "screencap", "-p"], binary=True)
        if proc.returncode != 0 or not proc.stdout:
            stderr = proc.stderr
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", "replace")
            raise RuntimeError(
                f"`adb -s {serial} exec-out screencap` failed "
                f"(exit {proc.returncode}): {(stderr or '').strip()}"
            )
        dest.write_bytes(proc.stdout)
        return str(dest.resolve()), warnings


#: Process-wide adapter instance reused for native Android captures (stateless).
_ANDROID_ADAPTER: Final[AndroidAdapter] = AndroidAdapter()


def capture_for_platform(
    platform: str,
    base_url: str,
    target: "Target",
    devices: dict[str, "Device"],
    out_path: str | Path,
    *,
    settle_ms: int = 1500,
) -> tuple[str, list[str]]:
    """Dispatch a single capture to the adapter matching ``platform``.

    For any web-target platform (see :data:`WEB_TARGET_PLATFORMS`) this is a
    thin pass-through to :func:`capture_target` — those platforms (plain web and
    the mobile web-targets Ionic / Expo-web / Flutter-web) all serve over HTTP
    and share the exact same Playwright capture path.

    ``ios-sim`` is dispatched to :class:`SimulatorAdapter` (``xcrun simctl``) and
    ``android-emu`` to :class:`AndroidAdapter` (``adb``); both are native paths
    that ignore ``base_url``/``settle_ms`` entirely (there is no HTTP server or
    DOM to settle). Any other value is rejected as a configuration error.

    Args:
        platform: The scope's ``platform`` value.
        base_url: The dev server base URL (``target.route`` is appended). Unused
            for native (``ios-sim``) captures.
        target: The capture target describing route/device/theme/state.
        devices: The scope's named device registry (for ``target.device``).
        out_path: Destination PNG path. Parent dirs are created.
        settle_ms: Milliseconds to wait after navigation for client rendering.
            Unused for native (``ios-sim``) captures.

    Returns:
        A ``(path, warnings)`` tuple, identical to :func:`capture_target`.

    Raises:
        ValueError: For an unrecognized ``platform`` value.
        RuntimeError: For native platforms when the simulator/emulator cannot be
            resolved, booted, or screenshotted.
        PlaywrightUnavailableError: If Playwright/browsers are unavailable (web).
    """
    if platform in WEB_TARGET_PLATFORMS:
        return capture_target(
            base_url, target, devices, out_path, settle_ms=settle_ms
        )
    if platform == "ios-sim":
        # Native capture: base_url/settle_ms do not apply (no HTTP, no DOM).
        return _SIMULATOR_ADAPTER.capture(target, devices, out_path)
    if platform == "android-emu":
        # Native capture: base_url/settle_ms do not apply (no HTTP, no DOM).
        return _ANDROID_ADAPTER.capture(target, devices, out_path)
    raise ValueError(
        f"Unknown platform {platform!r}. Expected one of: "
        f"{sorted(WEB_TARGET_PLATFORMS | NATIVE_PLATFORMS)}."
    )
