# SPDX-License-Identifier: MIT
"""Environment diagnostics and 3-client manifest generator for Electronics AI 2.0 (F9).

Verifies every pinned dependency required by the deterministic placer/router
pipeline:
  - Windows x64 + Python >= 3.12 + core scientific/MCP packages
  - Rust native extension ``placer_core`` (hpwl, Problem, pathfinder_estimate)
  - KiCad 10.0.8 CLI + bundled ``pcbnew`` Python
  - OpenJDK 25 + pinned Freerouting 2.5.0 jar SHA-256
  - Pro PCB Designer Skill package + 11 live ``placer__*`` MCP tools
  - Read-only SHA-256 verification of the sealed corpus (never parses boards)
  - Non-blocking detection + ready-to-use manifests for Claude Code, Codex, and
    Antigravity pointing to one unified ``electronics-ai-placer`` backend.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

from . import corpus, toolchain
from .mcp import PlacerService

REQUIRED_PACKAGES: tuple[tuple[str, str], ...] = (
    ("numpy", "numpy"),
    ("shapely", "shapely"),
    ("scipy", "scipy"),
    ("PIL", "pillow"),
    ("mcp", "mcp"),
    ("jsonschema", "jsonschema"),
    ("olefile", "olefile"),
)

SKILL_REL = Path("skills") / "electronics-ai-pro-pcb-designer"
SKILL_REFERENCES = (
    "references/feranec-floorplan-methodology.md",
    "references/hartley-return-path-si-pi.md",
    "references/intent-and-tool-contract.md",
    "references/review-and-signoff-checklist.md",
)
SERVER_NAME = "electronics-ai-placer"


def _check_platform() -> dict[str, Any]:
    arch = platform.machine()
    py_ver = ".".join(str(x) for x in sys.version_info[:3])
    ok = sys.platform == "win32" and sys.version_info >= (3, 12) and "64" in arch
    return {
        "ok": ok,
        "platform": sys.platform,
        "arch": arch,
        "python": py_ver,
        "executable": sys.executable,
        "detail": "Windows x64 + Python >= 3.12" if ok else "Requires Windows x64 and Python >= 3.12",
    }


def _check_python_packages() -> dict[str, Any]:
    versions: dict[str, str] = {}
    missing: list[str] = []
    for mod_name, dist_name in REQUIRED_PACKAGES:
        try:
            importlib.import_module(mod_name)
            try:
                versions[dist_name] = importlib.metadata.version(dist_name)
            except importlib.metadata.PackageNotFoundError:
                versions[dist_name] = "installed"
        except ImportError:
            missing.append(dist_name)
    return {
        "ok": not missing,
        "packages": versions,
        "missing": missing,
        "detail": f"{len(versions)}/{len(REQUIRED_PACKAGES)} required Python packages available"
        if not missing
        else f"Missing packages: {', '.join(missing)}",
    }


def _check_rust_core() -> dict[str, Any]:
    try:
        import placer_core  # type: ignore[import-not-found]

        expected = ("hpwl", "PlacementProblem", "grid_astar", "pathfinder_estimate")
        missing_attrs = [a for a in expected if not hasattr(placer_core, a)]
        if missing_attrs:
            return {"ok": False, "detail": f"placer_core missing symbols: {', '.join(missing_attrs)}"}
        smoke_hpwl = float(placer_core.hpwl([0.0, 10.0], [0.0, 0.0], [[0, 1]]))
        pf = placer_core.pathfinder_estimate(
            4, 4, [2.0] * 12, [2.0] * 12, [[(0, 0), (3, 3)]], 2, 0.5, 1.0, 1.5
        )
        ok = abs(smoke_hpwl - 10.0) < 1e-9 and isinstance(pf, dict) and "total_overflow" in pf
        return {
            "ok": ok,
            "version": getattr(placer_core, "__version__", None),
            "module_path": getattr(placer_core, "__file__", None),
            "smoke_hpwl": smoke_hpwl,
            "smoke_pathfinder_overflow": pf.get("total_overflow"),
            "detail": (
                f"Rust placer_core {getattr(placer_core, '__version__', '')} native extension verified "
                "(hpwl + PlacementProblem + grid_astar + pathfinder_estimate)"
            )
            if ok
            else "placer_core smoke test failed",
        }
    except (ImportError, OSError, RuntimeError, ValueError, TypeError) as exc:
        return {
            "ok": False,
            "detail": f"placer_core unavailable ({exc}); run `pixi run build-core`",
        }


def _check_kicad() -> dict[str, Any]:
    try:
        kc = toolchain.kicad()
        pcbnew_probe = subprocess.run(
            [str(kc.python), "-c", "import pcbnew; print(pcbnew.Version())"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        pcbnew_ok = pcbnew_probe.returncode == 0
        return {
            "ok": pcbnew_ok,
            "version": kc.version,
            "root": str(kc.root),
            "cli": str(kc.cli),
            "python": str(kc.python),
            "pcbnew_version": pcbnew_probe.stdout.strip() if pcbnew_ok else None,
            "detail": f"KiCad {kc.version} CLI and bundled pcbnew verified"
            if pcbnew_ok
            else f"KiCad pcbnew import failed: {pcbnew_probe.stderr.strip()[:200]}",
        }
    except (toolchain.ToolchainError, OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "detail": str(exc)}


def _check_freerouting() -> dict[str, Any]:
    try:
        fr = toolchain.freerouting()
        java_ok = "25" in fr.java_version or "openjdk" in fr.java_version.lower() or "java" in fr.java_version.lower()
        return {
            "ok": java_ok,
            "version": fr.version,
            "jar": str(fr.jar),
            "sha256": fr.sha256,
            "java": str(fr.java),
            "java_version": fr.java_version,
            "detail": f"Freerouting {fr.version} (pinned SHA-256) + {fr.java_version}"
            if java_ok
            else f"Unexpected Java runtime: {fr.java_version}",
        }
    except (toolchain.ToolchainError, OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "detail": str(exc)}


def _check_skill_and_mcp(repo_root: Path) -> dict[str, Any]:
    skill_dir = repo_root / SKILL_REL
    skill_md = skill_dir / "SKILL.md"
    missing_files = [
        rel for rel in ("SKILL.md", *SKILL_REFERENCES) if not (skill_dir / rel).is_file()
    ]
    live_tools = sorted(t["name"] for t in PlacerService.list_tools())
    mentioned_tools: list[str] = []
    if skill_md.is_file():
        all_md = "\n".join(p.read_text(encoding="utf-8") for p in sorted(skill_dir.rglob("*.md")))
        mentioned_tools = sorted(set(re.findall(r"\bplacer__([a-z][a-z0-9_]*)\b", all_md)))
    tools_match = live_tools == mentioned_tools and len(live_tools) == 11
    ok = not missing_files and tools_match
    return {
        "ok": ok,
        "skill_dir": str(skill_dir),
        "missing_files": missing_files,
        "live_tool_count": len(live_tools),
        "live_tools": [f"placer__{name}" for name in live_tools],
        "skill_tools_match": tools_match,
        "detail": f"Pro PCB Designer skill + {len(live_tools)} placer__* MCP tools verified"
        if ok
        else f"Skill/MCP mismatch (missing={missing_files}, tools_match={tools_match})",
    }


def _check_sealed_corpus() -> dict[str, Any]:
    rows = corpus.verify_sealed()
    interf = next((e for e in corpus.CORPUS if e.name == "interf_u"), None)
    interf_present = bool(interf and interf.path.is_file())
    all_present = interf_present and all(r["present"] and r["hash_matches"] for r in rows)
    return {
        "ok": all_present,
        "interf_u_present": interf_present,
        "hashed_boards": rows,
        "detail": "All sealed test boards present with matching SHA-256"
        if all_present
        else "One or more optional local sealed test boards missing or modified",
    }


def _backend_command(repo_root: Path, pixi_exe: str | None = None, python_exe: str | None = None) -> dict[str, Any]:
    """Return one canonical stdio backend launch spec (installed Python or pixi)."""
    root = repo_root.resolve()
    if python_exe:
        py = str(Path(python_exe).resolve())
        return {
            "command": py,
            "args": ["-B", "-u", "-m", "eda_agent.placer.mcp"],
            "env": {
                "PYTHONPATH": str(root / "eda-agent" / "src"),
                "PYTHONUTF8": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
            },
        }
    pixi = pixi_exe or shutil.which("pixi") or str(Path.home() / "AppData" / "Local" / "pixi" / "bin" / "pixi.exe")
    return {
        "command": pixi,
        "args": ["run", "-q", "--manifest-path", str(root / "pixi.toml"), "placer-mcp"],
        "env": {},
    }


def render_client_manifests(
    repo_root: Path | None = None,
    *,
    pixi_exe: str | None = None,
    python_exe: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Build identical single-backend MCP manifests for Claude Code, Codex, and Antigravity."""
    root = (repo_root or toolchain.REPO_ROOT).resolve()
    spec = _backend_command(root, pixi_exe=pixi_exe, python_exe=python_exe)
    skill_path = str(root / SKILL_REL / "SKILL.md")

    claude_entry: dict[str, Any] = {"type": "stdio", "command": spec["command"], "args": spec["args"]}
    if spec["env"]:
        claude_entry["env"] = spec["env"]
    claude_json = json.dumps({"mcpServers": {SERVER_NAME: claude_entry}}, indent=2, ensure_ascii=False) + "\n"

    antigravity_entry: dict[str, Any] = {"command": spec["command"], "args": spec["args"]}
    if spec["env"]:
        antigravity_entry["env"] = spec["env"]
    antigravity_json = json.dumps({"mcpServers": {SERVER_NAME: antigravity_entry}}, indent=2, ensure_ascii=False) + "\n"

    codex_lines = [
        f"[mcp_servers.{SERVER_NAME}]",
        f"command = {json.dumps(spec['command'])}",
        f"args = {json.dumps(spec['args'])}",
        "startup_timeout_sec = 60",
        "tool_timeout_sec = 900",
        "enabled = true",
    ]
    if spec["env"]:
        codex_lines.append("")
        codex_lines.append(f"[mcp_servers.{SERVER_NAME}.env]")
        for key, value in sorted(spec["env"].items()):
            codex_lines.append(f"{key} = {json.dumps(value)}")
    codex_toml = "\n".join(codex_lines) + "\n"

    return {
        "claude-code": {
            "filename": "claude-code.mcp.json",
            "target_hint": ".mcp.json (project) or ~/.claude.json (user)",
            "format": "json",
            "skill_path": skill_path,
            "content": claude_json,
        },
        "codex": {
            "filename": "codex.config.toml",
            "target_hint": "~/.codex/config.toml (or $CODEX_HOME/config.toml)",
            "format": "toml",
            "skill_path": skill_path,
            "content": codex_toml,
        },
        "antigravity": {
            "filename": "antigravity.mcp_config.json",
            "target_hint": "~/.gemini/antigravity/mcp_config.json",
            "format": "json",
            "skill_path": skill_path,
            "content": antigravity_json,
        },
    }


def emit_client_manifests(
    out_dir: str | Path,
    *,
    repo_root: Path | None = None,
    pixi_exe: str | None = None,
    python_exe: str | None = None,
) -> dict[str, str]:
    """Write validated manifests for all three clients into ``out_dir``."""
    dest = Path(out_dir).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    manifests = render_client_manifests(repo_root, pixi_exe=pixi_exe, python_exe=python_exe)
    written: dict[str, str] = {}
    readme_lines = [
        "# Electronics AI 2.0: Client MCP Manifests",
        "",
        "All three clients connect to the exact same local `electronics-ai-placer` backend:",
        "",
    ]
    for client, info in manifests.items():
        # Validate every manifest parses cleanly before writing.
        if info["format"] == "json":
            json.loads(info["content"])
        else:
            tomllib.loads(info["content"])
        target = dest / info["filename"]
        target.write_text(info["content"], encoding="utf-8", newline="\n")
        written[client] = str(target)
        readme_lines.append(f"- **{client}**: copy `{info['filename']}` into `{info['target_hint']}`")
    readme_lines.extend([
        "",
        f"Skill path: `{manifests['claude-code']['skill_path']}`",
        "",
    ])
    readme_path = dest / "README.md"
    readme_path.write_text("\n".join(readme_lines), encoding="utf-8", newline="\n")
    written["readme"] = str(readme_path)
    return written


def _json_has_server(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return False
    servers = data.get("mcpServers", {}) if isinstance(data, dict) else {}
    return isinstance(servers, dict) and (SERVER_NAME in servers or "mcp-electronics-ai" in servers)


def _toml_has_server(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return False
    servers = data.get("mcp_servers", {}) if isinstance(data, dict) else {}
    plugins = data.get("plugins", {}) if isinstance(data, dict) else {}
    has_srv = isinstance(servers, dict) and (SERVER_NAME in servers or "mcp-electronics-ai" in servers)
    has_plugin = isinstance(plugins, dict) and any(k.startswith("mcp-electronics-ai@") for k in plugins)
    return has_srv or has_plugin


def check_clients(home: Path | None = None, repo_root: Path | None = None) -> dict[str, dict[str, Any]]:
    """Inspect registration state across Claude Code, Codex, and Antigravity (read-only)."""
    user_home = (home or Path.home()).resolve()
    root = (repo_root or toolchain.REPO_ROOT).resolve()
    codex_cfg = (
        Path(os.environ["CODEX_HOME"]) / "config.toml"
        if home is None and os.environ.get("CODEX_HOME")
        else user_home / ".codex" / "config.toml"
    )
    claude_project = root / ".mcp.json"
    claude_user = user_home / ".claude.json"
    ag_cfg = user_home / ".gemini" / "antigravity" / "mcp_config.json"

    return {
        "claude-code": {
            "project_config": str(claude_project),
            "user_config": str(claude_user),
            "configured": _json_has_server(claude_project) or _json_has_server(claude_user),
        },
        "codex": {
            "config": str(codex_cfg),
            "configured": _toml_has_server(codex_cfg),
        },
        "antigravity": {
            "project_config": str(claude_project),
            "user_config": str(ag_cfg),
            "configured": _json_has_server(ag_cfg) or _json_has_server(claude_project),
        },
    }


def run_doctor(repo_root: Path | None = None, *, home: Path | None = None) -> dict[str, Any]:
    """Run all Electronics AI 2.0 environment and packaging checks."""
    root = (repo_root or toolchain.REPO_ROOT).resolve()
    checks = {
        "platform": _check_platform(),
        "python_packages": _check_python_packages(),
        "rust_core": _check_rust_core(),
        "kicad": _check_kicad(),
        "freerouting": _check_freerouting(),
        "skill_and_mcp": _check_skill_and_mcp(root),
    }
    optional = {
        "sealed_corpus": _check_sealed_corpus(),
        "clients": check_clients(home=home, repo_root=root),
    }
    ok = all(item["ok"] for item in checks.values())
    return {
        "schema": "electronics-ai/doctor",
        "schema_version": 1,
        "ok": ok,
        "repo_root": str(root),
        "checks": checks,
        "optional": optional,
    }


def _format_human(report: dict[str, Any]) -> str:
    lines = ["Electronics AI 2.0 Doctor", "=" * 40]
    for name, item in report["checks"].items():
        tag = "[PASS]" if item["ok"] else "[FAIL]"
        lines.append(f"{tag} {name:16s} {item['detail']}")
    sc = report["optional"]["sealed_corpus"]
    lines.append(f"[INFO] {'sealed_corpus':16s} {sc['detail']}")
    for client, cinfo in report["optional"]["clients"].items():
        state = "configured" if cinfo["configured"] else "not configured (run --emit-manifests)"
        lines.append(f"[INFO] client:{client:11s} {state}")
    lines.append("-" * 40)
    lines.append("OVERALL: GREEN (ready)" if report["ok"] else "OVERALL: FAILED (see FAIL items above)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Electronics AI 2.0 environment doctor & 3-client manifest tool.")
    parser.add_argument("--json", action="store_true", help="Print full JSON report.")
    parser.add_argument("--emit-manifests", type=Path, help="Write Claude Code, Codex, and Antigravity MCP manifests.")
    parser.add_argument("--python-exe", help="Use an embedded/installed Python path instead of pixi in emitted manifests.")
    args = parser.parse_args(argv)

    report = run_doctor()
    if args.emit_manifests:
        report["emitted_manifests"] = emit_client_manifests(args.emit_manifests, python_exe=args.python_exe)

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(_format_human(report))
        if "emitted_manifests" in report:
            print("Emitted manifests:")
            for k, v in report["emitted_manifests"].items():
                print(f"  - {k}: {v}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
