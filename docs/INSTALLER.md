# Desktop installation and independent runtime

The Free Core works without registration. The Codex plugin remains the preferred
Codex integration because it installs the MCP endpoint and skills together. The
standalone Windows installer is an alternative for machines without Python
and for other AI clients. Altium Designer is a separate prerequisite.

## Independent plugin runtime

With an installed Python 3.12, run the current installed helper:

```powershell
py -3.12 "$pluginRoot\scripts\plugin_runtime.py" bootstrap --altium-exe "C:\Program Files\Altium\AD26\X2.EXE" --library-dir "D:\MyProject\Libraries"
py -3.12 "$pluginRoot\scripts\plugin_runtime.py" doctor --probe-backends
```

Each explicit bootstrap prepares a fresh private environment under
`data/runtimes/<UTC-id>/python-3.12` and installs the EDA package as a regular
wheel. It checks dependencies before switching stored settings and keeps
previous environments/settings for recovery. Subsequent setup preserves current
library roots, options, tool restrictions, timeouts and native workspace identity;
it refuses a conflicting identity. Only EDA and native Python libraries are
configured backends.

The plugin ID and MCP server ID are `mcp-electronics-ai`; marketplace
`electronics-ai`. Default data is `~/.codex/plugins/data/mcp-electronics-ai` and
the default native runtime is `%LOCALAPPDATA%/ElectronicsAI`. The supported helper
commands are `bootstrap`, `doctor`, `configure-clients` and `serve`.

Normal startup resolves the current package code without installing dependencies
or launching the editor. Supported launchers and subprocesses pass `-B` and
`PYTHONDONTWRITEBYTECODE=1`, keeping caches out of immutable package roots.
Checkpoints, sourcing caches and configuration stay in private data.

## Multi-client configuration

```powershell
python configure_suite.py --client cursor --destination "C:\path\to\suite" `
  --python "C:\path\to\python.exe" --data-dir "C:\path\to\plugin-data"
# Review the plan, then append --apply.
```

`--client` accepts `codex`, `claude-desktop`, `claude-code`, `cursor`, `windsurf`,
`vscode`, or `all`. `--config` overrides the path for one selected client/profile.
Default user files on Windows:

| Client | User configuration |
| --- | --- |
| Codex | `CODEX_HOME/config.toml`, otherwise `~/.codex/config.toml` |
| Claude Desktop | `%APPDATA%/Claude/claude_desktop_config.json` |
| Claude Code | `~/.claude.json`, top-level user-scoped `mcpServers` |
| Cursor | `~/.cursor/mcp.json` |
| Windsurf / Cascade in Devin | `%APPDATA%/devin/mcp_config.json` |
| VS Code | `%APPDATA%/Code/User/mcp.json`, `servers` table |

These are Windows defaults, not proof that a particular version/profile reads
that file. The current Devin Local default agent uses a different CLI config;
the `windsurf` adapter targets Cascade only. VS Code's newer Copilot harness can
use a separate config; use the client's MCP settings UI to select its active file.
Microsoft Store Claude installations can use a virtualized profile: select its
actual Developer > Edit Config file via `--config`.

All files preflight before any write. Strict JSON is preserved semantically and
byte-exact backups are made; JSONC comments/trailing commas cause an actionable
failure with no writes rather than being discarded. TOML unrelated tables and
comments are retained. An enabled matching Codex plugin is detected and a
duplicate standalone registration is skipped. The client itself may still
require its own trust/enable step. `--client all` plans/configures all six files, not the apps.

The plugin helper also accepts a global `--client` before the subcommand:

```powershell
python scripts/plugin_runtime.py --data-dir "C:\path\to\plugin-data" --client cursor configure-clients
python scripts/plugin_runtime.py --data-dir "C:\path\to\plugin-data" --client cursor configure-clients --apply
```

`--client` with `bootstrap` applies client configuration
after successful setup. Omitting it never changes client settings. A standalone
MCP registration supplies tools; it does not claim to install skills into other
clients. The Codex plugin bundles the skills directly.

## Altium discovery and startup

```powershell
python altium_installation.py --script-project "C:\path\to\ElectronicsAI-EDA.PrjScr"
```

Detection reads both Windows uninstall registry views and bounded standard
installation folders. Directory-derived AD21–AD26 versions are hints, not verified
binary versions. No editor is launched by discovery. The documented Global
Projects Preferences page can load a script project at Altium startup. Loading a
project does not execute its bridge loop. The existing coordinated MCP workers
start that loop on demand; a permanently running startup script could break their
ownership protocol. No cross-version registry format is vendor documented here,
so this release produces the precise supported UI plan rather than guessing keys.

## Offline installer pipeline

Build on Windows x64 with Python 3.12 and Inno Setup 6+:

```powershell
python installer/build_installer.py --python-embed "C:\inputs\python-3.12.x-embed-amd64.zip" `
  --python-embed-sha256 "<independently-reviewed-sha256>" --iscc "C:\path\ISCC.exe" `
  --output "C:\build\electronics-ai-installer-new"
```

The explicit runtime input must be verified against its publisher. There is no
unverified `latest` download. Review the selected runtime build and its security updates before publishing
a distribution. A pinned input is reproducible; that alone does not establish
that it is the latest security runtime. The pipeline
records the exact Python/runtime dependency versions and SHA-256 values in
`INSTALLER_PROVENANCE.json`. It rejects traversal and symlinks before extraction.

The builder creates the EDA wheel, resolves constrained binary wheels into the
embedded runtime, and runs a real import probe including Windows COM dependencies
and `olefile`. The package audit checks every `.PrjScr` member against wheel includes; CI
also builds the actual EDA wheel and verifies its native files byte-for-byte.
Native library codecs are original Python source in the shared plugin package. No external library binary, crate vendor tree or corresponding-
source archive is produced. Wheel distribution metadata and the embedded Python
license remain in the installation.

Inno Setup installs under `%LOCALAPPDATA%/Programs/ElectronicsAI` without admin,
adds a local setup wizard, and leaves private state/backups on uninstall. The
wizard lets the user select Altium and one AI client; it does not launch native
scripts or save designs. CLI equivalent:

```powershell
.\python\python.exe -B suite_hub.py setup-bundled --altium-exe "C:\Program Files\Altium\AD26\X2.EXE" --client cursor
```

Existing coordinator identity and tool restrictions survive installer setup.
The optional `suite_hub.py register-token` reads an externally issued token from
stdin and stores only Windows DPAPI ciphertext in private `auth.json`. No account
registration service, login validation, tray daemon or paid entitlement service
is implemented; the Free Core never requires such a token.

`.github/workflows/installer.yml` is manually dispatched with the reviewed
Python.org URL and SHA-256; it uploads installer and checksums as CI
artifacts. It does not publish a release automatically or claim code signing.
The product installer is unsigned unless separately signed by the publisher.

## Sources reviewed 2026-10-28

The embedded interpreter's `._pth` mode ignores `PYTHON*` environment variables
and may execute site bootstrap hooks before a Python entry script starts.
Supported launchers, configured clients, dependency probes and Python workers
therefore pass `-B` explicitly. Use `python.exe -B ...` for manual bundled-runtime
commands too; changing `sys.dont_write_bytecode` inside a script alone is too late
to protect those startup hooks.

- [Claude Code MCP scopes](https://code.claude.com/docs/en/mcp)
- [Claude Desktop local servers](https://modelcontextprotocol.io/docs/develop/connect-local-servers)
- [Cursor MCP configuration](https://cursor.com/docs/context/mcp)
- [Cascade/Devin current MCP file](https://docs.devin.ai/desktop/cascade/mcp)
- [VS Code MCP configuration](https://code.visualstudio.com/docs/copilot/customization/mcp-servers)
- [VS Code harness configuration migration](https://code.visualstudio.com/docs/agent-customization/migrate-customizations)
- [Altium supported script loading](https://www.altium.com/documentation/altium-designer/scripting/running-scripts)
- [CPython embedded distribution](https://docs.python.org/3.12/using/windows.html#the-embeddable-package)
- [Python 3.12 security release and binary availability](https://www.python.org/downloads/release/python-31216/)
- [Inno Setup](https://jrsoftware.org/isinfo.php)
