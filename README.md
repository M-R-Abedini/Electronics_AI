# Electronics AI

[![Tests](https://github.com/M-R-Abedini/Electronics_AI/actions/workflows/tests.yml/badge.svg)](.github/workflows/tests.yml)

[English](README.md) · [فارسی](docs/README.fa.md) · [简体中文](docs/README.zh-CN.md) · [العربية](docs/README.ar.md)

**Electronics AI** is a desktop Codex plugin that packages one MCP connection, circuit-design, PCB-layout and setup skills, and coordinated Altium automation. It lets an AI read and edit schematics, PCBs and library files while you keep Altium open. This repository remains the source of truth for both the plugin and standalone MCP installation.

**Windows · Python 3.12 · Tested live on Altium Designer 26**

## What it can do

- Read and edit the open schematic: add components, draw wires, place net labels and power ports, edit parameters, pull a BOM or netlist.
- Work the PCB: move components, lay tracks and vias, check placement clearances, panelize.
- Audit the design: orphan net labels, floating ports, unconnected IC pins, missing decoupling caps, designator collisions, off-grid parts — or 33 lint checks in one call, with optional DRC.
- Check connectivity with four read-only tools: snapshot pin-to-net assignments (SHA-256 digest), per-pin contracts (expected net, or intentional NC with a reason), before/after diffs, and schematic↔PCB pad parity in both directions.
- Calculate trace width, impedance, and length budgets. These are calculators, not a signal-integrity proof.
- Inspect saved or live PCB copper with the `si_pi__` tools: classify net intent, audit actual 2D routing and reference coverage, preview physical design rules, and cross-probe stored violations. Missing stackup, topology or qualified current data remains explicit unknown evidence. See [copper SI/PI workflow](docs/COPPER_SI_PI.md).
- Review PCB placement with `layout__`: explicit circuit intent, composable profiles, transformed pad geometry, baseline comparison and a stored, validated move plan. Apply uses checkpoints, live freshness and readback. Unsupported electrical, mechanical or recovery evidence remains unknown. See the [adaptive layout skill](skills/electronics-ai-pcb-layout/SKILL.md) and [implementation report](PCB_PLACEMENT_REPORT.fa.md).
- Read and write `.SchLib` / `.PcbLib` library files.
- Search ChilaBoard's public catalog and download revision-pinned libraries with source/hash/license receipts. Stage authorized SnapMagic/SamacSys downloads without executing archive contents. Native geometry and pin mapping still need verification. See [unified suite and cloud workflow](docs/review/UNIFIED_SUITE_2026-10-27.md).
- Use the researched [electronics-ai-circuit-design skill](skills/electronics-ai-circuit-design/SKILL.md) for requirements, model verification, structured schematic plans and design-check evidence.
- Manage native assembly variants by GUID, bulk-update fitted states, and export their component matrix. Changes use project backups and native readback; see the [variant workflow](docs/review/STORAGE_VARIANTS_2026-10-08.md).
- One native transport: EDA operations share a serialized script engine; every request carries an ID so a late answer to an old request is never accepted.
- Concurrent clients: a local FIFO service admits native commands in order; health traffic yields to queued work. `pcb_capture_snapshot` reads components, nets and pad assignments in one native pass. Clients can then use `design_read_snapshot` and `design_analyze_snapshot` concurrently without occupying Altium. Snapshots are historical evidence, not live edit preconditions. See [usage and measured limits](docs/review/CONCURRENT_CLIENTS_2026-10-14.md).
- The current native script project is generated from the selected package. Explicit target documents, ownership checks and correlated request IDs protect native dispatch.

## What's still in development

- Placement assistance: batch placement and clearance checks work; finer placement tooling is still being worked on.
- Bridge reliability: every known DelphiScript crash gets fixed or guarded, but new ones still surface — this is ongoing.
- Validation so far is on Altium Designer 26; other versions haven't been exercised yet.

## Current limitations

- Native batch edits can leave partial changes after a failure; undo grouping does not guarantee rollback. Save a copy before bulk edits.

- Experimental. Some operations can crash Altium's DelphiScript engine and stop the polling loop — back up your design before letting the AI edit it.
- Native save and script-backed UI commands can wait during a handler. The loop releases the engine after about two idle seconds; background pings do not extend that deadline. The next coordinated tool call restarts it automatically.
- EDA JSON parameters above U+00FF (Ω, Chinese text) are rejected before dispatch to prevent silent `?` substitution. Full Unicode transport is not implemented.
- The EDA transport preserves UNC paths; access to the share depends on Windows permissions.
- `proj_sync_pcb` requires `allow_modal=True`; the schematic→PCB ECO still opens an interactive dialog.
- The connectivity checkers verify pin assignments, not copper. They can't see data you didn't feed them.
- An open modal dialog blocks script commands; Win32 dialog diagnostics remain available. Busy handlers reject new dispatch. Timed-out requests are withdrawn if not yet consumed; in-flight edits cannot be cancelled, so inspect the design before retrying.

## Setup

### Codex plugin

The plugin bundles its MCP definition and skills together. From a development
checkout with the Python dependencies installed, build a clean package, register
its local marketplace, and install it with the supported Codex CLI:

```powershell
.\.venv\Scripts\python.exe scripts/build_plugin.py --output dist --smoke
codex plugin marketplace add ./dist/marketplace --json
$installedPlugin = codex plugin add mcp-electronics-ai@electronics-ai --json | ConvertFrom-Json
$pluginRoot = $installedPlugin.installedPath
```

Run the bundled **Electronics AI Setup** skill (`$electronics-ai-setup`), or
select the installed path returned by `codex plugin add` and run:

```powershell
py -3.12 "$pluginRoot\scripts\plugin_runtime.py" bootstrap --altium-exe "C:\Program Files\Altium\AD26\X2.EXE" --library-dir "D:\MyProject\Libraries"
py -3.12 "$pluginRoot\scripts\plugin_runtime.py" doctor --probe-backends
```

Bootstrap creates an independent private Python environment and installs the EDA
package as a regular wheel. Configuration stays outside the cache; normal MCP
startup never installs dependencies. Altium Designer and its license remain
separate prerequisites. Reconnect Codex after installation or an update. Keep one
active `mcp-electronics-ai` connection. See [installation, updates and
verification](docs/PLUGIN_PACKAGE.md).

### Source development and other MCP clients

The canonical local source root is `D:\project\Electronics_AI`; installed
cache files are deployment artifacts. On another machine, clone the repository
and prepare its development environment:

```powershell
git clone https://github.com/M-R-Abedini/Electronics_AI.git
Set-Location Electronics_AI
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe scripts/plugin_runtime.py bootstrap --altium-exe "C:\Program Files\Altium\AD26\X2.EXE" --library-dir "D:\MyProject\Libraries"
.\.venv\Scripts\python.exe scripts/plugin_runtime.py --client cursor configure-clients
# Review the configuration plan, then repeat with --apply.
```

Use `--client` and optional `--config` for the intended client only. The Codex
plugin installs MCP plus skills; a standalone registration supplies MCP tools.
Keep the configured modern native runtime/workspace identity during subsequent
updates and do not clear ownership markers to force setup. See [desktop and
client setup](docs/INSTALLER.md).

The default `--toolset minimal` exposes `suite_catalog`, `suite_invoke` and
`suite_status`. Discover exact schemas through
`suite_catalog(query="...", backend="eda", with_schema=true)`, then invoke the
returned `eda__...` name through `suite_invoke`. Current namespaces are `eda__`,
`libraries__`, `cloud__`, `suite__`, `supply__`, `route__`, `telemetry__`, `si_pi__`
and `layout__`. `--toolset full` advertises the namespaced tools directly.

All 34 library operations execute through original Python OLE2 codecs. Repeat
`--library-dir` for existing intended library roots; cloud staging gets its own
allowlisted private root. No Rust compiler or external library executable is
needed. SnapMagic and SamacSys account/API access is not bundled.

## Questions and contributions

Ask setup and usage questions in [Q&A](https://github.com/M-R-Abedini/Electronics_AI/discussions/categories/q-a).
For bugs and feature requests, use [Issues](https://github.com/M-R-Abedini/Electronics_AI/issues/new/choose).
See [the contributing guide](CONTRIBUTING.md) for development setup, tests, translations, and crediting collaborators.

## License

Suite coordination and native library codecs: MIT. EDA backend: Apache-2.0. See [license scope and exceptions](THIRD_PARTY_NOTICES.md) and [upstream snapshots](UPSTREAM.json).
# Free desktop product and native libraries

The [0.2.0 baseline release](https://github.com/M-R-Abedini/Electronics_AI/releases/tag/v0.2.0)
and the annotated `v0.3.0` Free-Tier Desktop baseline are merged in `main`. The baseline adds free local disk
checkpoints, electrical-value normalization, component supply adapters, a local
Freerouting runner, explicit opt-in structural datasets, multi-client setup,
and a Windows installer build pipeline. No suite account is required.

Discover `suite`, `supply`, `route` and `telemetry` backends with
`suite_catalog`; the advertised minimal MCP surface remains three meta-tools.
Start with [native edit safety](docs/PRODUCT_SAFETY.md),
[supply intelligence](docs/SUPPLY_CHAIN.md), [routing limits](docs/AUTOROUTE.md),
[optional datasets](docs/TELEMETRY.md), and [installation](docs/INSTALLER.md).
These documents distinguish working public/offline features from API access
requirements and unfinished native exchange; SES generation does not establish
Altium import or a DRC-clean board. Collection is disabled by default.

The current 0.7.0 package uses independent Python library codecs and one EDA
native transport. Retired compatibility namespaces, setup commands and old
plugin identifiers are removed. Historical release tags retain their original
contents and licenses.
