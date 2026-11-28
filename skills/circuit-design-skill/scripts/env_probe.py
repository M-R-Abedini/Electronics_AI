#!/usr/bin/env python3
"""Detect which verification stages can run on this machine.

Presence on ``PATH`` is not capability.  A binary can be present and still be
unusable: wrong CPU architecture (an x86-64 build on aarch64 Android), a
missing shared library, a glibc binary on Termux, or a version too old for the
flags the skill passes.  This probe therefore *executes* each tool, including a
small smoke workload where that is cheap, and classifies it as:

    ok            found, runs, and meets the minimum version
    incompatible  found, but cannot run or is too old (reason recorded)
    missing       not found on PATH or not importable

Stages map to the checks in ``verify.py``.  When a stage is not locally
available, the skill can dispatch the same design to GitHub Actions; this
module decides which backend is usable and refuses CI when the working tree
cannot represent what would be tested.

CLI::

    python3 scripts/env_probe.py
    python3 scripts/env_probe.py --json
    python3 scripts/env_probe.py --stage simulate --require local
    python3 scripts/env_probe.py --spec specs/rc.json --require ci
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent

MIN_PYTHON = (3, 10)
MIN_KICAD = 9

# A stage is runnable only when every listed tool is present *and* supported.
STAGE_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "design": (),
    "simulate": ("ngspice",),
    "kicad-parse": ("kiutils",),
    "erc": ("kicad-cli",),
    "pcb-drc": ("kicad-pcb-drc",),
    "fab": ("kicad-cli",),
}
ALL_STAGES = ("simulate", "kicad-parse", "erc", "pcb-drc", "fab")

SMOKE_NETLIST = """* ngspice compatibility probe
V1 1 0 DC 1
R1 1 0 1k
.op
.end
"""

# Probe results do not change during a single verify run.
_CACHE: dict[str, dict[str, Any]] = {}


def _run(cmd: list[str], *, timeout: int = 20, cwd: Path | None = None) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(cwd) if cwd else None,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _text(proc: subprocess.CompletedProcess | None) -> str:
    if proc is None:
        return ""
    return ((proc.stdout or "") + (proc.stderr or "")).strip()


def _short(text: str, limit: int = 240) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _failure_detail(proc: subprocess.CompletedProcess | None, fallback: str) -> str:
    """Turn an exec failure into the sentence a human needs."""
    if proc is None:
        return fallback
    text = _text(proc) or fallback
    lowered = text.lower()
    for needle, reason in (
        ("exec format error", "wrong CPU architecture for this machine"),
        ("cannot execute", "the binary or its interpreter cannot be executed here"),
        ("cannot link executable", "missing shared library"),
        ("error while loading shared libraries", "missing shared library"),
        ("permission denied", "not executable"),
    ):
        if needle in lowered:
            return f"{reason}: {_short(text)}"
    if proc.returncode != 0:
        return f"exited with {proc.returncode}: {_short(text)}"
    return _short(text)


def parse_kicad_major(text: str | None) -> int | None:
    if not text:
        return None
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text)
    if match:
        return int(match.group(1))
    match = re.search(r"\b(\d+)\b", text)
    return int(match.group(1)) if match else None


def _tool(ok: bool, *, supported: bool | None = None, **extra: Any) -> dict[str, Any]:
    return {"ok": ok, "supported": ok if supported is None else supported, **extra}


def probe_python() -> dict[str, Any]:
    version = platform.python_version()
    return _tool(
        sys.version_info[:2] >= MIN_PYTHON,
        version=version,
        executable=sys.executable,
        detail=f"Python {version}",
    )


def probe_ngspice() -> dict[str, Any]:
    cached = _CACHE.get("ngspice")
    if cached is not None:
        return cached
    path = shutil.which("ngspice")
    if not path:
        result = _tool(False, version=None, path=None, detail="ngspice not found on PATH")
        _CACHE["ngspice"] = result
        return result
    version_proc = _run([path, "-v"], timeout=20)
    if version_proc is None or version_proc.returncode != 0:
        result = _tool(True, supported=False, version=None, path=path,
                       detail=_failure_detail(version_proc, f"{path} did not report a version"))
        _CACHE["ngspice"] = result
        return result
    version = (_text(version_proc).splitlines() or [""])[0].strip() or None

    # A version banner does not prove the binary can link or parse a deck.
    with tempfile.TemporaryDirectory() as td:
        deck = Path(td) / "probe.cir"
        log = Path(td) / "probe.log"
        deck.write_text(SMOKE_NETLIST, encoding="utf-8")
        smoke = _run([path, "-b", "-o", str(log), str(deck)], timeout=60)
        smoke_text = (log.read_text(errors="replace") if log.exists() else "") + _text(smoke)
    if smoke is None or smoke.returncode != 0:
        result = _tool(True, supported=False, version=version, path=path,
                       detail="ngspice reports a version but the smoke netlist failed: "
                              + _failure_detail(smoke, "no output"))
        _CACHE["ngspice"] = result
        return result
    if re.search(r"singular matrix|doAnalyses:\s*iteration limit", smoke_text, re.IGNORECASE):
        result = _tool(True, supported=False, version=version, path=path,
                       detail="ngspice reports a version but cannot solve a 2-resistor DC deck")
        _CACHE["ngspice"] = result
        return result
    result = _tool(True, version=version, path=path,
                   detail=f"ngspice {version}; smoke netlist solved")
    _CACHE["ngspice"] = result
    return result


def probe_kicad_cli(command: str | None = None) -> dict[str, Any]:
    command = command or os.environ.get("KICAD_CLI") or "kicad-cli"
    path = command if os.path.isabs(command) else shutil.which(command)
    if path and not Path(path).is_file():
        path = None
    if not path:
        return _tool(False, version=None, major=None, path=None,
                     detail=f"{command} not found on PATH")
    version_proc = _run([path, "version"], timeout=30)
    if version_proc is None or version_proc.returncode != 0:
        return _tool(True, supported=False, version=None, major=None, path=path,
                     detail=_failure_detail(version_proc, f"{path} did not report a version"))
    version = (_text(version_proc).splitlines() or [""])[0].strip() or None
    major = parse_kicad_major(version)
    supported = major is not None and major >= MIN_KICAD

    # Verify the ERC subcommand and flags this skill depends on exist.
    erc_help = _run([path, "sch", "erc", "--help"], timeout=30)
    if erc_help is None or erc_help.returncode != 0:
        return _tool(True, supported=False, version=version, major=major, path=path,
                     detail="kicad-cli found but `sch erc` is unavailable: "
                            + _failure_detail(erc_help, "subcommand failed"))
    if "--exit-code-violations" not in _text(erc_help):
        supported = False

    detail = f"kicad-cli {version}" if version else "kicad-cli found"
    if major is not None and major < MIN_KICAD:
        detail += f" (KiCad {MIN_KICAD}+ required)"
    elif not supported and major is not None:
        detail += " (ERC exit-code gating unavailable; KiCad 9+ required)"
    return _tool(True, supported=supported, version=version, major=major, path=path, detail=detail)



def probe_kicad_pcb_drc(command: str | None = None) -> dict[str, Any]:
    """Probe the actual PCB DRC subcommand and violation exit-code flag."""
    base = probe_kicad_cli(command)
    if not base.get("supported"):
        return _tool(bool(base.get("ok")), supported=False,
                     version=base.get("version"), major=base.get("major"),
                     path=base.get("path"), detail=base.get("detail", "kicad-cli unavailable"))
    proc = _run([base["path"], "pcb", "drc", "--help"], timeout=30)
    if proc is None or proc.returncode != 0:
        return _tool(True, supported=False, version=base.get("version"),
                     major=base.get("major"), path=base.get("path"),
                     detail="kicad-cli found but PCB DRC subcommand is unavailable: "
                            + _failure_detail(proc, "subcommand failed"))
    if "--exit-code-violations" not in _text(proc):
        return _tool(True, supported=False, version=base.get("version"),
                     major=base.get("major"), path=base.get("path"),
                     detail="PCB DRC lacks --exit-code-violations")
    return _tool(True, supported=True, version=base.get("version"),
                 major=base.get("major"), path=base.get("path"),
                 detail=f"kicad-cli {base.get('version')}: PCB DRC supported")


def probe_kiutils() -> dict[str, Any]:
    code = (
        "import importlib.metadata as m\n"
        "import kiutils.schematic\n"
        "try:\n"
        "    print(m.version('kiutils'))\n"
        "except m.PackageNotFoundError:\n"
        "    print('unknown')\n"
    )
    proc = _run([sys.executable, "-c", code], timeout=30)
    if proc is None or proc.returncode != 0:
        lines = [line.strip() for line in _text(proc).splitlines() if line.strip()]
        detail = lines[-1] if lines else "kiutils is not importable by this Python"
        return _tool(False, version=None, detail=_short(detail))
    version = (_text(proc).splitlines() or [""])[0].strip() or "unknown"
    return _tool(True, version=version, detail=f"kiutils {version}")


def probe_gh() -> dict[str, Any]:
    path = shutil.which("gh")
    if not path:
        return _tool(False, authenticated=False, path=None, version=None,
                     detail="gh CLI not found on PATH")
    version_proc = _run([path, "--version"], timeout=15)
    version = (_text(version_proc).splitlines() or [""])[0].strip() or None
    auth = _run([path, "auth", "status"], timeout=30)
    authenticated = auth is not None and auth.returncode == 0
    detail = version or "gh found"
    if not authenticated:
        detail += " (not authenticated: run `gh auth login`)"
    return _tool(True, authenticated=authenticated, path=path, version=version, detail=detail)


def probe_git(cwd: Path | None = None) -> dict[str, Any]:
    cwd = cwd or Path.cwd()
    proc = _run(["git", "rev-parse", "--show-toplevel"], timeout=15, cwd=cwd)
    root_text = _text(proc)
    root = Path(root_text).resolve() if proc is not None and proc.returncode == 0 and root_text else None
    branch = upstream = remote = None
    if root is not None:
        branch_proc = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], timeout=15, cwd=root)
        branch = _text(branch_proc) or None if branch_proc is not None and branch_proc.returncode == 0 else None
        upstream_proc = _run(
            ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
            timeout=15,
            cwd=root,
        )
        upstream = _text(upstream_proc) or None if upstream_proc is not None and upstream_proc.returncode == 0 else None
        remote_proc = _run(["git", "remote", "get-url", "origin"], timeout=15, cwd=root)
        remote = _text(remote_proc) or None if remote_proc is not None and remote_proc.returncode == 0 else None
    return {
        "ok": root is not None,
        "root": str(root) if root else None,
        "branch": branch,
        "upstream": upstream,
        "remote": remote,
        "detail": f"{root} @ {branch}" if root and branch else "not a git repository",
    }


def repo_relative(root: Path, path: str | Path, cwd: Path | None = None) -> str | None:
    """Return ``path`` relative to ``root``, or None when it is outside."""
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = (cwd or Path.cwd()) / candidate
    try:
        return candidate.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return None


def ci_blockers(
    root: Path | str | None,
    spec: str | Path | None = None,
    *,
    workflow: str = "design.yml",
    cwd: Path | None = None,
    paths: list[str | Path] | None = None,
) -> list[str]:
    """Reasons the pushed tree cannot represent the local working copy in CI."""
    if root is None:
        return ["not inside a git repository"]
    root = Path(root)
    blockers: list[str] = []
    workflow_path = root / ".github" / "workflows" / workflow
    if not workflow_path.is_file():
        blockers.append(f"workflow missing: {workflow_path}")

    watched: list[str] = ["scripts", "schemas", "tests", "examples", ".github/workflows/design.yml"]
    for extra in paths or []:
        rel = repo_relative(root, extra, cwd)
        if rel is None:
            blockers.append(f"input is outside the repository: {extra}")
        elif not (root / rel).is_file():
            blockers.append(f"input not found: {rel}")
        elif rel not in watched:
            watched.insert(0, rel)
    if spec is not None:
        rel = repo_relative(root, spec, cwd)
        if rel is None:
            blockers.append(f"spec is outside the repository: {spec}")
        elif not (root / rel).is_file():
            blockers.append(f"spec not found: {rel}")
        elif rel not in watched:
            watched.insert(0, rel)

    status = _run(["git", "status", "--porcelain", "--", *watched], timeout=30, cwd=root)
    dirty = _text(status)
    if dirty:
        blockers.append(
            "uncommitted or untracked pipeline inputs:\n    "
            + "\n    ".join(dirty.splitlines()[:10])
        )

    upstream_proc = _run(
        ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
        timeout=15,
        cwd=root,
    )
    upstream = _text(upstream_proc)
    if upstream_proc is None or upstream_proc.returncode != 0 or not upstream:
        blockers.append("current branch has no upstream (push it with `git push -u origin <branch>`)")
    else:
        ahead_proc = _run(["git", "rev-list", "--count", f"{upstream}..HEAD"], timeout=30, cwd=root)
        try:
            ahead = int(_text(ahead_proc) or "0")
        except ValueError:
            ahead = 0
        if ahead > 0:
            blockers.append(f"{ahead} commit(s) not pushed to {upstream}")

    return blockers


def stage_status(tools: dict[str, Any], stage: str) -> dict[str, Any]:
    missing: list[str] = []
    incompatible: list[str] = []
    for name in STAGE_REQUIREMENTS.get(stage, ()):
        tool = tools.get(name, {})
        if not tool.get("supported", False):
            if tool.get("ok"):
                incompatible.append(name)
            else:
                missing.append(name)
    return {"ok": not missing and not incompatible,
            "missing": missing, "incompatible": incompatible}


def missing_for_stages(probe: dict[str, Any], stages: list[str] | tuple[str, ...] | None = None) -> list[str]:
    stages = tuple(stages or ALL_STAGES)
    names: list[str] = []
    for stage in stages:
        for name in probe.get("stages", {}).get(stage, {}).get("missing", []):
            if name not in names:
                names.append(name)
    return names


def incompatible_for_stages(probe: dict[str, Any],
                            stages: list[str] | tuple[str, ...] | None = None) -> list[str]:
    stages = tuple(stages or ALL_STAGES)
    names: list[str] = []
    for stage in stages:
        for name in probe.get("stages", {}).get(stage, {}).get("incompatible", []):
            if name not in names:
                names.append(name)
    return names


def stages_ready(probe: dict[str, Any], stages: list[str] | tuple[str, ...] | None = None) -> bool:
    return not missing_for_stages(probe, stages) and not incompatible_for_stages(probe, stages)


def recommended_backend(probe: dict[str, Any],
                        stages: list[str] | tuple[str, ...] | None = None) -> str:
    if stages_ready(probe, stages):
        return "local"
    if probe.get("ci", {}).get("usable"):
        return "ci"
    return "none"


def probe(
    *,
    kicad_cli: str | None = None,
    spec: str | Path | None = None,
    paths: list[str | Path] | None = None,
    workflow: str = "design.yml",
    cwd: Path | None = None,
) -> dict[str, Any]:
    cwd = cwd or Path.cwd()
    tools = {
        "python": probe_python(),
        "ngspice": probe_ngspice(),
        "kicad-cli": probe_kicad_cli(kicad_cli),
        "kicad-pcb-drc": probe_kicad_pcb_drc(kicad_cli),
        "kiutils": probe_kiutils(),
        "gh": probe_gh(),
    }
    stages = {name: stage_status(tools, name) for name in STAGE_REQUIREMENTS}
    git = probe_git(cwd)
    root = Path(git["root"]) if git.get("root") else None
    blockers = (
        ci_blockers(root, spec, workflow=workflow, cwd=cwd, paths=paths)
        if root is not None
        else ["not inside a git repository"]
    )
    workflow_present = bool(root and (root / ".github" / "workflows" / workflow).is_file())
    ci_usable = bool(
        tools["gh"].get("ok")
        and tools["gh"].get("authenticated")
        and git.get("ok")
        and git.get("branch")
        and git.get("branch") != "HEAD"
        and workflow_present
        and not blockers
    )
    return {
        "python": tools["python"],
        "tools": {key: value for key, value in tools.items() if key != "python"},
        "stages": stages,
        "local_ready": all(stages[name]["ok"] for name in ALL_STAGES),
        "missing": missing_for_stages({"stages": stages}),
        "incompatible": incompatible_for_stages({"stages": stages}),
        "platform": {
            "system": platform.system(),
            "machine": platform.machine(),
            "detail": platform.platform(),
            "termux": "com.termux" in os.environ.get("PREFIX", ""),
        },
        "ci": {
            "usable": ci_usable,
            "workflow": workflow,
            "workflow_present": workflow_present,
            "blockers": blockers,
            "git": git,
            "gh": tools["gh"],
        },
        "recommended_backend": recommended_backend({"stages": stages, "ci": {"usable": ci_usable}}),
    }


def render(probe_result: dict[str, Any], stages: list[str] | tuple[str, ...] | None = None) -> str:
    stages = tuple(stages or ALL_STAGES)
    lines = ["Environment probe", ""]
    python = probe_result.get("python", {})
    lines.append(f"  python       {'ok' if python.get('supported') else 'MISSING':<8} "
                 f"{python.get('detail', 'unknown')}")
    for name in ("ngspice", "kicad-cli", "kicad-pcb-drc", "kiutils", "gh"):
        tool = probe_result.get("tools", {}).get(name, {})
        if tool.get("supported"):
            mark = "ok"
        elif tool.get("ok"):
            mark = "INCOMPAT"
        else:
            mark = "MISSING"
        lines.append(f"  {name:<12} {mark:<8} {tool.get('detail', '')}")
    plat = probe_result.get("platform", {})
    lines.append(f"  platform     {plat.get('machine', '?')} {plat.get('system', '?')}"
                 + (" (Termux)" if plat.get("termux") else ""))

    lines.extend(["", "Stages", ""])
    for name in ("design", "simulate", "kicad-parse", "erc", "pcb-drc", "fab"):
        status = probe_result.get("stages", {}).get(name, {})
        if status.get("ok"):
            lines.append(f"  {name:<12} ready")
        else:
            reasons = [f"missing {n}" for n in status.get("missing", [])]
            reasons += [f"incompatible {n}" for n in status.get("incompatible", [])]
            lines.append(f"  {name:<12} NOT READY: {', '.join(reasons) or 'unknown'}")

    ci = probe_result.get("ci", {})
    git = ci.get("git", {})
    lines.extend(["", "Backends", ""])
    local_ready = all(probe_result.get("stages", {}).get(name, {}).get("ok") for name in stages)
    lines.append(f"  local   {'READY' if local_ready else 'NOT READY'}")
    if ci.get("usable"):
        lines.append(f"  ci      READY ({git.get('detail', '')})")
    else:
        lines.append("  ci      NOT READY")
        for blocker in ci.get("blockers", []):
            lines.append(f"          - {blocker}")
    lines.extend(["", f"Recommended backend: {recommended_backend(probe_result, stages)}"])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Detect which circuit-verification stages this machine can run.")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("--spec", help="design spec to check for CI readiness")
    parser.add_argument("--kicad-cli", help="explicit kicad-cli command")
    parser.add_argument("--workflow", default="design.yml", help="GitHub Actions workflow file name")
    parser.add_argument(
        "--stage",
        action="append",
        choices=sorted(STAGE_REQUIREMENTS),
        help="stage to check; repeatable (default: simulate, kicad-parse, erc, fab)",
    )
    parser.add_argument(
        "--require",
        choices=("auto", "local", "ci"),
        help="exit non-zero unless the selected backend is available",
    )
    args = parser.parse_args(argv)

    stages = args.stage or list(ALL_STAGES)
    result = probe(kicad_cli=args.kicad_cli, spec=args.spec, workflow=args.workflow)
    backend = recommended_backend(result, stages)

    if args.json:
        payload = dict(result)
        payload["checked_stages"] = stages
        payload["recommended_backend"] = backend
        print(json.dumps(payload, indent=2))
    else:
        print(render(result, stages))

    if args.require:
        if args.require == "auto":
            return 0 if backend != "none" else 1
        if args.require == "local":
            return 0 if stages_ready(result, stages) else 1
        return 0 if result.get("ci", {}).get("usable") else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
