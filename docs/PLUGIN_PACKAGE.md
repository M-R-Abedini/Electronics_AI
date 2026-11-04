# Codex plugin packaging and setup

Electronics AI 0.8.1 bundles one MCP connection and three skills for circuit
design, PCB layout and setup. The canonical source is the
[Electronics_AI repository](https://github.com/M-R-Abedini/Electronics_AI), locally
`D:\project\Electronics_AI`. Build artifacts and installed caches are deployment
outputs; edit and test the source repository.

The package contains root `plugin.json`, `mcp.json`, and `skills/`. Its launcher
resolves installed files through `${PLUGIN_ROOT}`. Altium automation requires
Windows and a separately installed, licensed Altium Designer. The plugin setup
runtime requires Python 3.12; the standalone installer supplies its own reviewed
runtime. See [installer and client support](INSTALLER.md).

## Package contents

- Plugin/MCP ID `mcp-electronics-ai`, display name **Electronics AI**,
  marketplace `electronics-ai`.
- `electronics-ai-setup`, `electronics-ai-circuit-design` and
  `electronics-ai-pcb-layout` skills.
- The EDA bridge, original native Python library codecs, analysis services,
  DelphiScript sources, runtime requirements and applicable license notices.

Configured minimal mode exposes `suite_catalog`, `suite_invoke` and
`suite_status`; namespaced capabilities are discovered on demand. Persistent
backend configuration contains exactly `eda` and `libraries`. Library operations
run through Python OLE2 codecs without an external binary, Rust compiler or crate
sources. The package excludes credentials, user designs, private settings,
Altium and a Python interpreter.

## Build and install

From a prepared development checkout:

```powershell
.\.venv\Scripts\python.exe scripts/build_plugin.py --output dist --smoke --check-reproducible
codex plugin marketplace add .\dist\marketplace --json
$installedPlugin = codex plugin add mcp-electronics-ai@electronics-ai --json | ConvertFrom-Json
$pluginRoot = $installedPlugin.installedPath
```

Outputs include `dist/mcp-electronics-ai-0.8.1.zip`, `artifacts.json` and the
local `dist/marketplace` tree. The builder selects public package files; private
`analysis/`, `tools/`, environments, configuration and repair backups are not
release inputs. `--smoke` initializes an isolated empty-config package and checks
its MCP surface without launching Altium. `--check-reproducible` compares builds
from identical selected inputs and records their file hashes.

Keep the local marketplace folder available for updates. `codex plugin list
--json` reports actual installation and enabled state; registering a marketplace
alone does not install the plugin. Reconnect Codex after installing or updating
to load its current skills and MCP definitions. A Git marketplace must point to
a published ref containing the manifests. Local/Git distribution does not imply
publication in the public plugin directory.

## Configure the private runtime

Use `$electronics-ai-setup` or run the installed helper explicitly:

```powershell
py -3.12 "$pluginRoot\scripts\plugin_runtime.py" bootstrap --altium-exe "C:\Program Files\Altium\AD26\X2.EXE" --library-dir "D:\MyProject\Libraries"
py -3.12 "$pluginRoot\scripts\plugin_runtime.py" doctor --probe-backends
```

Repeat `--library-dir` for intended existing library roots. Bootstrap creates a
fresh environment at `data/runtimes/<UTC-id>/python-3.12` and installs the packaged
EDA sources as a regular wheel. It verifies dependencies before switching the
stored runtime state. It keeps backups of previous settings and does not delete
the previous environment. Development rendering/test/dashboard extras are not
part of normal runtime setup.

A subsequent bootstrap retains current configured library roots, logging
options, tool restrictions, timeouts and the selected native runtime/workspace
identity. A conflicting workspace fails preflight. Never clear ownership
markers or move a running editor to another workspace to make setup pass.

Default state is `CODEX_HOME/plugins/data/mcp-electronics-ai`, using `~/.codex`
when `CODEX_HOME` is unset. The native runtime and workspace default to
`%LOCALAPPDATA%/ElectronicsAI` and its `workspace` subdirectory. An absolute
`ELECTRONICS_AI_PLUGIN_DATA` or global `--data-dir` selects another state path:

```powershell
py -3.12 "$pluginRoot\scripts\plugin_runtime.py" --data-dir "D:\ElectronicsAIData" doctor
```

Use the same path for setup and launch. Product environment names use
`ELECTRONICS_AI_*`; `ALTIUM_EXE` and `EDA_AGENT_WORKSPACE` retain their technical
meaning. The launcher accepts `-DataDir` for an explicit invocation.

The supported helper commands are `bootstrap`, `doctor`, `configure-clients`
and `serve`. Normal `serve` resolves workers from the currently selected package
and never installs dependencies or launches Altium. Before setup, the launcher
provides only a setup-needed `suite_status`, which does not establish editor
readiness. An incomplete state/template pair is refused rather than guessed.

## Verify the installed connection

After reconnecting, discover the live schema and invoke the returned name:

```text
suite_status()
suite_catalog(backend="eda", query="app_context", with_schema=true)
suite_invoke(name="eda__app_context", arguments={})
suite_catalog(backend="libraries", query="list_components", with_schema=true)
```

`doctor --probe-backends` initializes the EDA worker and lists tools without
calling a native tool. Success proves worker/catalog availability. A successful
read-only `app_context` call with one intended Altium instance supplies separate
native bridge evidence. Use an explicit permitted sample library for a reader
check. A production design is not a setup fixture.

Retain an initialization failure's diagnostic ID, phase and causes. An ambiguous
or possibly dispatched design write must not be replayed automatically. Setup,
metadata and catalog checks do not prove schematic correctness, ERC/DRC,
manufacturing readiness or tested hardware.

Cloud search/staging is described in [cloud library support](review/CLOUD_LIBRARIES_2026-10-27.md).
Provider accounts/API access and model verification are separate from plugin
installation. Downloaded symbols/footprints need exact part, package and
pin-to-pad verification.

## Maintain and update

Edit source, run tests, build the package, install the new marketplace version
and reconnect. Never patch the installed cache. Keep one active connection to
the native editor and preserve current modern native workspace identity and
private library/tool policy through updates. Setup does not edit or save
user designs.

Treat published versions as immutable; increment the package version and record
the new ZIP checksum for each release. Historical tags retain their original
contents. Current source uses MIT and Apache-2.0 components with the notices in
[THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md). See
[CONTRIBUTING](../CONTRIBUTING.md) for source, package and native verification.
