# Security Policy

## Supported versions

Electronics AI is developed on `main`; only the latest release receives
security fixes. There are no LTS branches.

| Version | Supported |
| ------- | --------- |
| Latest release / `main` | ✅ |
| Older releases | ❌ |

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security problems.

Use GitHub's private vulnerability reporting for this repository
(**Security → Report a vulnerability**), or contact the maintainer via
the profile at https://github.com/M-R-Abedini. Include:

- A description of the issue and its impact
- Steps to reproduce, ideally a minimal MCP call sequence or input file
- The Electronics AI version, Python version, and Altium Designer
  version involved
- Any suggested mitigation

This is a solo-maintained project: acknowledgement within 14 days is
the goal, not a guarantee. Confirmed issues are fixed on `main` and
noted in the corresponding release.

## Scope

In scope:

- The unified MCP gateway (`suite_stdio.py`, `suite_*.py`) and the EDA
  backend (`eda-agent/`, including the DelphiScript bridge in
  `eda-agent/scripts/altium/`)
- The native library codecs and file parsers (`.SchLib` / `.PcbLib` /
  `.SchDoc` / `.PcbDoc` readers)
- Cloud library staging and supply-chain adapters (download allowlists,
  credential handling)
- The Windows installer and plugin runtime bootstrap

Out of scope:

- Vulnerabilities in Altium Designer itself: report those to Altium.
- Damage caused by letting an AI edit a design without a backup: the
  tool is experimental, native edits can partially apply, and the
  README's guidance to keep copies of production designs always
  applies.
- Findings that require a malicious MCP client already running with
  your user privileges on your machine, unless they cross a trust
  boundary the project claims to enforce (per-user IPC, checkpointing,
  explicit-target dispatch).

## Handling of secrets

Supplier API keys (Nexar, DigiKey, Mouser, OEMsecrets) are read from
private configuration or environment variables and are never written
to the repository or returned by `suite_status`. Registration tokens,
where used, are stored encrypted with Windows DPAPI under the current
user account. Debug output redacts token/secret-shaped values; if you
find a path where a secret is logged or transmitted unexpectedly,
treat it as a vulnerability and report it as above.
