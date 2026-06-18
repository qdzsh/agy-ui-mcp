# agy-ui-mcp

An MCP (Model Context Protocol) server that delegates **frontend / UI** work to
Google Antigravity's **`agy` CLI** (Gemini) - while guaranteeing the agent
**never touches backend, API, or business logic**. It is designed to be shared
by **Claude Code** and **Codex** as a dedicated "FE/UI worker".

The server exposes two tools:

- **`ui_implement`** - an iterative vision loop: screenshot the running app,
  prompt `agy` to edit CSS/components toward the target design, diff-gate the
  result to revert anything out of scope, re-screenshot, and repeat until it
  converges (or hits `max_iters`). Edits are applied to your working tree.
- **`ui_review`** - serve the app, screenshot it across every target
  (route × device × theme × state), optionally run accessibility checks, and
  have `agy` critique it **read-only** (any edit `agy` makes is reverted).

## What it can drive

| Surface | Platform values | How it's captured |
|---|---|---|
| Web apps | `web` (default) | Playwright (Chromium) over the dev-server URL |
| Mobile **web-targets** | `expo-web`, `ionic`, `flutter-web` | Playwright (same as web) |
| Native **iOS** | `ios-sim` | `flutter run` on the iOS Simulator + `xcrun simctl` screenshots |
| Native **Android** | `android-emu` | `flutter run` on an Android emulator + `adb` screenshots |

Across these it supports responsive viewports, device emulation, dark mode /
`prefers-color-scheme`, `forced-colors` (high contrast), print media, RTL,
component states (via `pre_steps`), seeded `localStorage`, per-target design
references, and match-score convergence when design references are provided.

**Accessibility:** for web targets, `ui_review` injects the vendored
[axe-core](https://github.com/dequelabs/axe-core) into the page and returns
structured WCAG violations (per target), which also ground `agy`'s critique.

## Use case: realign a drifted frontend

The case this server is built for: you (or Claude Code / Codex) shipped a
**full-stack project** - backend and frontend both done - but the **FE drifted
from, or doesn't match, the original design** (screen mockups, or design tokens
with an HTML/CSS demo). You want to **redo the FE to match the design without
risking the working backend**. That is exactly what the diff-gate guarantees:
`agy` realigns the UI, and anything outside your FE `allow` scope (API, server,
business logic) is reverted automatically.

**What it's strong at vs. where it needs help** - this is an *iterative
refinement loop*, not a from-scratch FE generator:

| Your FE today | Fit |
|---|---|
| Structure is right, **styling/layout/colors/spacing/responsive** is off | **Great** - its core job; realistically ~80-90% then human polish |
| **Partly** wrong (a few components / screens drifted) | Good - run it **screen by screen** with the matching `design_ref` |
| **Structurally** wrong (wrong component tree, missing screens, wrong layout) | **Partial** - it nudges existing code toward the design within scope; it does **not** rebuild markup from scratch. Have Claude Code/Codex scaffold the correct structure first, then use this server to drive pixel fidelity |

Fidelity is highest when you provide an **HTML/CSS demo or design tokens** (exact
colors/spacing/fonts) rather than an image alone (values are inferred from
pixels). Note the convergence score is `agy`'s **own visual self-assessment** -
always eyeball the returned `shots_before`/`shots_after` and `diff` to sign off.

**Workflow**

1. **Commit** your current state (a dirty tree is fine - it's snapshotted and
   preserved; the only requirement is a git repo with ≥1 commit).
2. Drop a `.agy-ui-scope` that **allows only FE files** and **denies the
   backend**, declares how to serve the app, and lists **one target per screen**
   with that screen's `design_ref` (see below).
3. Run **`ui_implement`** per screen with its design ref; review
   `shots_before`/`shots_after` + `diff`, then iterate (`max_iters`).
4. Run **`ui_review`** (read-only + a11y) to have `agy` critique what's left and
   surface WCAG issues.
5. **Human-polish** the last ~10-20% and anything structural the loop can't
   reach within scope.

**Sample config** - a Vite/React app, realigned screen-by-screen against
mockups in `./design/`:

```yaml
model: "gemini-3.5-flash"
platform: web

# FE surface agy may edit/create.
allow:
  - "src/**/*.css"
  - "src/**/*.scss"
  - "src/components/**"
  - "src/**/*.tsx"
  - "index.html"

# Backend / logic - always reverted, even if agy edits them.
deny:
  - "**/api/**"
  - "**/server/**"
  - "**/*.server.*"
  - "**/route.*"

# Sensitive entry points - reverted AND reported for a human to decide.
ambiguous:
  - "src/main.tsx"
  - "src/App.tsx"
  - "vite.config.*"

serve:
  cmd: "npm run dev"
  url: "http://localhost:5173"
  ready_timeout: 30

devices:
  desktop: { width: 1440, height: 900 }
  mobile:  { name: "iPhone 13" }     # full Playwright device emulation

# One capture per screen, each matched against its own design mockup.
# (targets supersedes the simple `viewports` list when present.)
targets:
  - name: "home-desktop"
    route: "/"
    device: "desktop"
    design_ref: "./design/home-desktop.png"     # image OR an HTML/CSS demo render

  - name: "dashboard-desktop"
    route: "/dashboard"
    device: "desktop"
    design_ref: "./design/dashboard-desktop.png"

  - name: "settings-mobile-dark"
    route: "/settings"
    device: "mobile"
    color_scheme: "dark"                          # emulate prefers-color-scheme: dark
    design_ref: "./design/settings-mobile-dark.png"
```

Then drive each screen, e.g. `ui_implement(project_dir=".",
task="Match this screen to its design_ref", target_route="/dashboard")`. Targets
carry the per-screen mockup; `target_route` picks which one to work on.

## Platform support

The server runs on **macOS and Linux**. It spawns `agy` (and native
`flutter run`) through a Unix pseudo-terminal (`pty`) and manages process groups
with POSIX-only calls, so **native Windows is not supported** - run it under
**WSL2** (Windows Subsystem for Linux) instead.

| OS | Web + mobile web-targets | Native Android | Native iOS |
|---|---|---|---|
| **macOS** | yes | yes | yes |
| **Linux** | yes | yes | no (iOS needs macOS + Xcode) |
| **Windows (native)** | no | no | no |
| **Windows via WSL2** | yes | with adb/emulator setup | no |

Notes:

- **iOS always requires macOS + Xcode**, regardless of host OS.
- **WSL2:** install the Linux build of `agy` (and log in) and run
  `playwright install chromium` inside WSL. Web and mobile web-targets work out
  of the box; native Android additionally needs `adb`/emulator wiring (e.g.
  connecting to a Windows-side emulator over TCP, or running the emulator inside
  WSL2).
- A native-Windows port would require replacing the `pty` layer with ConPTY
  (e.g. `pywinpty`) and the POSIX process-group calls; it is not implemented.

## How it works

- **PTY spawn.** `agy` is run as `agy -p "<prompt>"` through a Python
  pseudo-terminal (`pty.openpty` + `subprocess.Popen`), because `agy` drops its
  stdout when attached to a non-TTY pipe. Output is captured from the PTY
  master; ANSI escapes and carriage returns are stripped.
- **Subscription auth.** `agy` authenticates via your existing Gemini
  subscription/login - no `GEMINI_API_KEY` is passed by this server.
- **Diff-gate (the real guardrail).** Scope is **not** enforced inside `agy`.
  Web runs happen in a throwaway **git worktree**; after each turn the server
  classifies every changed path against your scope
  (`deny > ambiguous > allow > default-deny`) and reverts anything not allowed
  (ambiguous paths are reverted and reported as escalations). A staged edit is
  restored from the baseline, not the index, so it cannot slip through.
- **Vision loop.** The orchestrator (this server) screenshots to files with
  Playwright, embeds those paths in the prompt (`agy` opens them with its own
  `read_file` tool - there is no image flag), lets `agy` edit, applies the
  diff-gate, re-screenshots, and loops.
- **Native runs.** Native platforms run **in place** (no worktree, to reuse the
  build cache). `flutter run` is launched under a PTY and **hot-reloaded** (`r`)
  between iterations - with an automatic **hot-restart** (`R`) fallback when a
  reload produces no visual change. A graceful quit (`q`) lets Flutter release
  its lockfile cleanly.
- **In-place safety (snapshot-restore).** Before a native run, the server
  snapshots your project's current state into a dangling git baseline commit
  (without touching your index/HEAD/worktree) and records your pre-existing
  untracked files. The diff-gate and reverts compare against that baseline, so
  **only `agy`'s edits are gated/undone and your uncommitted work is preserved
  exactly** - you do **not** need to commit or stash first. The only hard
  requirement is that the project is a git repo with at least one commit; if it
  isn't, the tool returns a structured `{"status": "blocked", ...}` result
  explaining how to fix it (e.g. `git init`) instead of running unprotected.

## Requirements

- **Python ≥ 3.10**
- **The `agy` CLI**, installed and logged in (subscription auth)
- **Playwright Chromium** for web/a11y captures - installed separately
  (`playwright install chromium`); not needed for native-only use
- **Native iOS** (`ios-sim`): macOS + Xcode + a Flutter project, and a booted
  iOS Simulator
- **Native Android** (`android-emu`): the Android SDK platform-tools (`adb`) and
  an AVD; the adapter can auto-launch the AVD by name (`emulator -avd <name>`)

## Install

### From the remote

```bash
# 1. Install the server (gives you an `agy-ui-mcp` command on PATH)
pipx install git+https://github.com/qdzsh/agy-ui-mcp
# or: uv tool install git+https://github.com/qdzsh/agy-ui-mcp

# 2. Register it with Claude Code
claude mcp add agy-ui --scope user -- agy-ui-mcp

# 3. (web/a11y only) install the browser
"$(pipx environment --value PIPX_LOCAL_VENVS)/agy-ui-mcp/bin/playwright" install chromium
```

### From a clone (one command)

```bash
git clone https://github.com/qdzsh/agy-ui-mcp.git && cd agy-ui-mcp
./scripts/install.sh          # installs the package + Chromium, and offers to
                              # register with Claude Code
```

`scripts/install.sh` is interactive and idempotent; re-run it any time.

## Wire into Claude Code

```bash
# If installed as a console script (pipx / uv tool / pip):
claude mcp add agy-ui --scope user -- agy-ui-mcp

# Or run the module directly (e.g. from an editable/venv install):
claude mcp add agy-ui --scope user -- python -m agy_ui_mcp
```

## Wire into Codex

Add to `~/.codex/config.toml`:

```toml
[mcp_servers.agy-ui]
command = "agy-ui-mcp"        # or: command = "python", args = ["-m", "agy_ui_mcp"]
```

## Configure a project

Drop a `.agy-ui-scope` (YAML) in the target project's root. Copy the fully
annotated template and edit it for your stack:

```bash
cp .agy-ui-scope.example /path/to/your/app/.agy-ui-scope
```

A minimal web scope:

```yaml
# platform: web            # default; also expo-web / ionic / flutter-web / ios-sim / android-emu
allow:
  - "src/**/*.css"
  - "src/components/**"
deny:
  - "src/api/**"           # backend - agy edits here are always reverted
  - "**/*.server.*"
ambiguous:
  - "src/main.tsx"         # reverted AND reported for a human to decide
serve:
  cmd: "npm run dev"
  url: "http://localhost:5173"
  ready_timeout: 30
viewports: [1440, 768, 390]
model: "gemini-3.5-flash"
```

A native (iOS) scope uses `targets` + a device registry instead of `viewports`:

```yaml
platform: ios-sim
serve:
  cmd: "flutter run -d <simulator-udid>"   # argv-split (no shell) for native
  url: ""
  ready_timeout: 600                        # first Xcode/gradle build is slow
allow: ["lib/main.dart"]
deny:  ["lib/data.dart"]
devices:
  sim: { name: "iPhone 17" }                # or udid: "..."
targets:
  - { name: order-mobile, device: sim }
model: "gemini-3.5-flash"
```

See `.agy-ui-scope.example` for the full set of options (per-target
`design_ref`, `theme`, `rtl`, `color_scheme`, `forced_colors`, `media`,
`full_page`, `local_storage`, `pre_steps`, `serve.reload_cmd`, etc.).

## Tool reference

**`ui_implement(project_dir, task, design_refs=None, target_route=None,
max_iters=4, apply=True, match_threshold=90)`** → returns `files_changed`,
`diff`, `escalations`, `iterations`, `shots_before`/`shots_after`, `targets`,
`applied`/`applied_files`, `match_score`, `match_gaps`, `warnings`. When `apply`
is true the surviving in-scope edits are written to your working tree.

**`ui_review(project_dir, target_route=None, against_design=None, a11y=True)`**
→ returns `critique`, `shots`, `targets`, `a11y` (`{target: [violations]}`),
`warnings`. Read-only.

Both may instead return `{"status": "blocked", "blocked_reason": "...", ...}`
when a native/in-place run can't be made safe (non-git or no commit yet) - the
`blocked_reason` tells you exactly what to do.

## Notes & limitations

- **Native needs a git repo + ≥1 commit.** This is by design (the in-place
  safety snapshot). A dirty working tree is fine and is preserved; only a
  non-git or commit-less project is refused (with a clear message). Web runs use
  an isolated worktree and also require git.
- **Playwright browsers are separate.** Web and a11y features need
  `playwright install chromium`; native-only use does not.
- **Native serve command is argv-split** (`shlex.split`, no shell) so the PTY
  can deliver `r`/`R`/`q` keystrokes to `flutter` directly - shell features
  (`&&`, env-var expansion, `cd`) in `serve.cmd` won't work for native.
- **First native build can take minutes** (Xcode / Gradle). Set
  `serve.ready_timeout` generously (e.g. 300-600s).
- **Flutter scaffolding.** If `flutter run` reports a missing `ios/` or
  `android/` project, regenerate it with `flutter create --platforms=ios .`
  (or `android`).

## Offline / dry runs

Set `AGY_UI_DRY_RUN=1` to make the tools skip every external side effect
(spawning `agy`, launching Playwright, running git, starting a dev server) and
return a stub payload. The package imports and the scope/diff-gate logic
unit-test without `agy`, Playwright browsers, or a running dev server.

```bash
pip install -e ".[dev]" && python -m pytest -q
```

## License

MIT.
