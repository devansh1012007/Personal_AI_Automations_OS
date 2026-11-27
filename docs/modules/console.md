# Module: Loop Console (`src/paa/tui/chat/console.py`)

## Module identity

- **Name:** Loop Console (SPEC 2 Wave 8, row C8.5)
- **Path:** `src/paa/tui/chat/console.py` (the textual-free service) + `ConsoleScreen` and its modals in `src/paa/tui/chat/screens.py:747`
- **Main responsibility:** loop management as a TUI surface — the registry panel (trust, tier, version, evidence badge), install/activate/fork, and the six-check conformance run. A view plus a thin dispatcher: no new authority, no new gate, nothing model-decided.
- **Architectural layer:** Presentation, on the C4.4 chat kit's thin-boundary rule (the operator dashboard stays Rich; the console is a screen of the chat app, not a sibling product)
- **Entrypoints:** `ChatApp.console_service` (`src/paa/tui/chat/app.py:321`), the Ctrl+B binding (`src/paa/tui/chat/app.py:256`), and `paa chat --console` (`src/paa/cli/chat.py:190`)
- **Public interfaces:** `ConsoleService`, `SkillPanelRow`, `PipelineForkRow`, `render_fork_overlay` (`src/paa/tui/chat/console.py`)
- **Other modules that depend on it:** none — it is a leaf view; the chat app constructs it lazily, so a session that never opens the console pays nothing for it.

## Purpose and responsibilities

The owner's direction: "a TUI/agent specifically for this type of stuff." The
console is the screen-shaped half of that — every read and every action
delegates to a surface that already exists and already decides, so the
console can only ever be a thinner or thicker view of the same rules the
CLI verbs enforce:

- **The registry panel** reads `PipelineRegistry` (`src/paa/pipelines/registry.py:394`) — rows come from `registry.list()`, never a parallel hand-maintained list, so the panel cannot disagree with `paa pipeline list`. Installed extensions merge their two sources of truth: the marketplace package row (the five-gate record) and the skills-registry row (what a chat turn can dispatch) in `skill_rows` (`src/paa/tui/chat/console.py:336`).
- **Install / activate** drive the C8.4 `ChatExtensionSurface` (`src/paa/chat/extensions.py:400`) whole — discovery through the Claw-Hub adapter, install through ALL five gates (the smoke gate really stages and runs the entrypoint under the sandbox), activation against the staged bytes. The surface is built per action (`src/paa/tui/chat/console.py:429`) with the operator's own opt-in for unsigned local skills, so what runs is exactly what `paa skills` runs.
- **Fork** is pinned to the two mechanisms the kit already offers, one per kind of loop (see below).
- **The conformance run** drives the six-check runner for the chosen pipeline on a throwaway ledger (`src/paa/tui/chat/console.py:557`); the page rendered is the standard's own `render_reports` output (`src/paa/pipelines/conformance.py:128`), so the console cannot show a verdict the standard would phrase differently.

## Internal implementation

### `console.py` — the service

`ConsoleService` (`src/paa/tui/chat/console.py:286`) is constructed from the pieces the chat wiring already holds (ledger, relational backend, `PAA_HOME`, permission mode, and the runtime's sandbox — carried by `ChatWiring.sandbox`, `src/paa/tui/chat/wiring.py:81`). It owns no state beyond those references:

- `pipeline_rows()` / `fork_rows()` — the registry's own rows, plus the personal forks with their load verdicts (`ok`, `invalid (reason)`, `source no longer registered`).
- `discover()` / `install()` / `activate()` — pass-throughs to the extension surface; a gate's refusal is returned as a refusal and rendered as one, never retried around.
- `fork_skill()` (`src/paa/tui/chat/console.py:534`) — copies the gated staged bytes into the personal skills dir (`PAA_HOME/skills/personal/<name>`, the word `personal` selects it in the install flow, `src/paa/tui/chat/console.py:97`). The fork itself registers nothing: installing the copy goes back through discovery and all five gates, exactly as a hand-written personal skill would. Refuses when there are no staged bytes or when the personal dir already holds that name.
- `fork_pipeline()` (`src/paa/tui/chat/console.py:574`) — seeds a **personal declaration overlay** under `PAA_HOME/pipelines/overlays/<pipeline>/<name>.toml` (`FORKS_DIR_NAME`, `src/paa/tui/chat/console.py:93`). The seed is `render_fork_overlay` (`src/paa/tui/chat/console.py:225`): a restatement of the pipeline's own declaration (params at declared defaults, slots at declared refs, capabilities exactly as asked, limits only where bounded), so it sits inside the loader's tighten-only envelope (ADR-0052, `src/paa/pipelines/overlay.py:307`) by construction — any widening edit the operator makes is refused at load by the existing loader, not by the console. A local-manifest copy was rejected deliberately: the manifest contract requires all four `[limits]` as concrete positive ints, and five of the six built-ins leave theirs unbounded — a manifest fork would have to invent numbers this surface has no authority to choose.
- `run_conformance()` — maps the registered name onto the runner `src/paa/pipelines/conformance.py` already defines (`_CONFORMANCE_RUNNERS`, `src/paa/tui/chat/console.py:153`), runs it on a scratch database that is closed before the report returns, and hands a pipeline without a runner the RED report naming the gap — the honesty rule `every_builtin_conformance` applies to builtins, never a faked green.

No new event type and no new gate: mutations ride the extension surface's own `EXTENSION_*` lineage; the console journals `console.*` UI events to the app's structured log only.

### `screens.py` — the screen and its modals

`ConsoleScreen` (`src/paa/tui/chat/screens.py:747`) binds the keys: `i` inspect (the declaration read out, `src/paa/tui/chat/screens.py:529`), `c` conformance, `f` fork, `d` install, `a` activate, `r` refresh. Its modals:

- `ConsoleInspectModal` (`src/paa/tui/chat/screens.py:568`) — one pipeline's declaration as text, the facts `paa pipeline inspect` prints.
- `ConformanceModal` (`src/paa/tui/chat/screens.py:585`) — the standard's own one-page report, verdict per check.
- `ForkNameModal` (`src/paa/tui/chat/screens.py:613`) — one input; a refused fork name is shown inside the still-open modal, so the operator fixes the name in place.
- `InstallSourceModal` (`src/paa/tui/chat/screens.py:667`) — two stages in one modal: the source (a directory, or `personal`), then the candidate list; Enter is the operator's opt-in for unsigned local skills, the same opt-in `paa skills install --allow-unverified` asks for.

The one-line status area names outcomes with glyphs (✓/✗), never colour alone.

### The doors

Ctrl+B and `/console` from the chat (`src/paa/tui/chat/app.py:256`), and `paa chat --console` opens the app on the console with Esc climbing back to the conversation (`src/paa/tui/chat/app.py:375`). The flag refuses the `--plain` combination rather than silently opening a surface the plain loop does not have (`src/paa/cli/chat.py:204`).

## Inputs and outputs

| Operation | Input | Output | Side effects |
|---|---|---|---|
| `pipeline_rows()` | — | the registry's rows | none (registry built per call) |
| `install(skill, ...)` | a discovered skill | install report | five-gate install, staged bytes, `EXTENSION_*` ledger rows |
| `fork_skill(name)` | installed skill name | the personal-dir path | copies staged bytes; registers nothing |
| `fork_pipeline(name, fork)` | pipeline + new name | the overlay path | writes one TOML file |
| `run_conformance(name)` | pipeline name | six-check report | scratch ledger only, closed after |

## Failure behavior

- A gate refusal (install) renders as the gate's own refusal — the gate named, the reason, "nothing landed: no staged bytes, no registry row" — with zero trace behind it.
- Fork refusals name what was refused: no staged bytes, a taken name, a name that would shadow a registered pipeline, a vanished source.
- `run_conformance` on an unknown name is a named refusal; on a pipeline without a runner it is the RED six-fail report.
- Every action that needs `PAA_HOME` refuses loudly when it is unset.

## Modification impact

- A pipeline added to the registry without a conformance runner reads RED in the console (the runner map is locked against the registry's runnable set, `tests/tui/test_console_service.py:383`).
- The fork seed must keep round-tripping through the loader unchanged for every registered pipeline (`tests/tui/test_console_service.py:171`); a declaration vocabulary the renderer cannot express raises rather than approximates.

## Known gaps

- The hosted/remote part of the marketplace (registry client) stays parked; discovery reads directories.
- The console renders one conformance run at a time; no batch/board view.

## Related documents

- [`modules/skills.md`](skills.md) — the skill adapter and registry the extensions panel merges.
- [`modules/marketplace.md`](marketplace.md) — the five gates and the package rows.
- [`../adr/0052-manifest-slots-overlays-tighten-only.md`](../adr/0052-manifest-slots-overlays-tighten-only.md) — the tighten-only overlay rule the pipeline fork rides.
- Pilot suite: `tests/tui/test_console_app.py:180` (doors), `tests/tui/test_console_app.py:245` (install through the gates), `tests/tui/test_console_app.py:403` (fork), `tests/tui/test_console_app.py:510` (conformance); service suite: `tests/tui/test_console_service.py:398` (install/activate, zero-trace refusal).
