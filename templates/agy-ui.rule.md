<!--
Copy-paste this block into your project's CLAUDE.md (or your Codex rules file).
It teaches the agent how to use the agy-ui MCP server correctly.
-->

## Using the agy-ui MCP (FE finisher)

`agy-ui` is a frontend FINISHER, not a generator. It converges an already-running
screen toward a design. It does not scaffold missing screens or invent markup.
Follow this order; do not skip ahead to visual polish.

1. **Build the structure yourself first.** Create the component tree, routes, and
   state, and get the screen actually RUNNING in the dev server (real data or
   realistic placeholders, not a blank page). Only then is there something for
   `agy-ui` to refine.

2. **Initialize the scope once (or rely on zero-config).** Run `ui_init(project_dir=".")`
   one time to detect your stack and write a starter `.agy-ui-scope` you can read
   and tweak. If you skip this, the server still works: it auto-detects the stack
   and synthesizes a scope on the fly.

3. **Extract design tokens ONCE, up front.** If you have an HTML/CSS demo or a
   token sheet, pull the exact colors, spacing, typography, and radii into your
   theme (CSS variables / theme config) a single time. Then build every component
   ON those tokens. Do not let per-screen passes re-derive tokens.

4. **Wire one mockup per screen.** Keep per-screen design images in `./design/`
   and list them in the scope as `targets`, each with its own `design_ref` and
   `route`. One target = one screen.

5. **Converge each running screen.** For each screen, run:

   ```
   ui_implement(project_dir=".", task="Match this screen to its design_ref", target_route="/<route>")
   ```

   Review the returned `shots_before` / `shots_after` / `diff`, then iterate.
   Anything outside your FE scope is reverted automatically by the diff-gate.

6. **Review.** Run `ui_review(project_dir=".")` for a read-only critique plus
   accessibility (a11y) findings, and fix what it surfaces.

**Never** call `ui_implement` / `ui_review` on an empty or placeholder screen.
The MCP refines what is already on screen; with nothing rendered it has nothing
to converge toward. Build it, run it, then finish it.
