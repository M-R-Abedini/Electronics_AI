#!/usr/bin/env python3
"""Dispatch circuit verification to GitHub Actions.

The local machine may not be able to run ngspice or KiCad 9 (wrong CPU
architecture, missing shared libraries, no GUI toolkit).  The design files are
plain text, so the same spec can be verified on a pinned Ubuntu runner:

    python3 scripts/ci_dispatch.py --spec examples/rc-lowpass.json
    python3 scripts/ci_dispatch.py --spec examples/rc-lowpass.json --watch --download out/ci

The dispatch is deliberately conservative: it refuses to start when the working
tree is dirty or the branch is ahead of its upstream.  GitHub Actions checks out
the pushed revision, so a local-only change would verify the wrong design.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import env_probe  # noqa: E402

DEFAULT_WORKFLOW = "design.yml"


class CiError(RuntimeError):
    """A CI dispatch could not be performed safely."""


def _run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    timeout: int = 120,
    capture: bool = True,
) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            text=True,
            capture_output=capture,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _text(proc: subprocess.CompletedProcess | None) -> str:
    if proc is None:
        return ""
    return ((proc.stdout or "") + (proc.stderr or "")).strip()


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _repo_args(repo: str | None) -> list[str]:
    return ["--repo", repo] if repo else []


def workflow_inputs(inputs: dict[str, Any] | None) -> list[str]:
    args: list[str] = []
    for key, value in (inputs or {}).items():
        if value is None or value == "":
            continue
        if isinstance(value, bool):
            args.extend(["-F", f"{key}={'true' if value else 'false'}"])
        else:
            args.extend(["-f", f"{key}={value}"])
    return args


def find_run(
    gh: str,
    *,
    repo: str | None,
    workflow: str,
    ref: str,
    started: datetime,
    cwd: Path | None,
    timeout: int = 120,
    interval: int = 5,
) -> dict[str, Any]:
    """Find the workflow_dispatch run started at/after ``started``."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        proc = _run(
            [
                gh,
                "run",
                "list",
                "--workflow",
                workflow,
                "--branch",
                ref,
                "--event",
                "workflow_dispatch",
                "--limit",
                "10",
                "--json",
                "databaseId,createdAt,status,conclusion,url,displayTitle",
                *_repo_args(repo),
            ],
            cwd=cwd,
            timeout=60,
        )
        if proc is not None and proc.returncode == 0:
            try:
                runs = json.loads(proc.stdout or "[]")
            except json.JSONDecodeError:
                runs = []
            for run in runs:
                created = _parse_time(run.get("createdAt"))
                if created is not None and created >= started - timedelta(seconds=30):
                    return run
        time.sleep(interval)
    raise CiError(
        "the workflow was dispatched but no matching run appeared. "
        "Open the repository's Actions tab and check the workflow name/ref."
    )


def dispatch(
    spec: str | Path,
    *,
    repo: str | None = None,
    workflow: str = DEFAULT_WORKFLOW,
    ref: str | None = None,
    require: str | None = None,
    watch: bool = False,
    download_dir: str | Path | None = None,
    timeout: int = 3600,
    cwd: Path | None = None,
) -> dict[str, Any]:
    """Trigger the design workflow and optionally wait for and download it."""
    cwd = cwd or Path.cwd()
    gh = shutil.which("gh")
    if not gh:
        raise CiError("gh CLI not found. Install it (`pkg install gh` on Termux) and run `gh auth login`.")

    info = env_probe.probe_git(cwd)
    root = Path(info["root"]) if info.get("root") else None
    if root is None and repo is None:
        raise CiError("not inside a git repository; CI needs a pushed spec. "
                      "Pass --repo owner/name to override.")
    if root is None:
        raise CiError("not inside a git repository; run from the design project")

    spec_rel = env_probe.repo_relative(root, spec, cwd)
    if spec_rel is None:
        raise CiError(f"spec is outside the repository: {spec}")
    if not (root / spec_rel).is_file():
        raise CiError(f"spec not found in the repository: {spec_rel}")

    blockers = env_probe.ci_blockers(root, spec, workflow=workflow, cwd=cwd)
    if blockers:
        raise CiError("refusing to dispatch CI from this working tree:\n- " + "\n- ".join(blockers))

    branch = ref or info.get("branch")
    if not branch or branch == "HEAD":
        raise CiError("detached HEAD; pass --ref <branch> so CI checks out a real branch")

    payload: dict[str, Any] = {"spec": spec_rel}
    if require:
        payload["require"] = require

    cmd = [gh, "workflow", "run", workflow, "--ref", branch, *_repo_args(repo),
           *workflow_inputs(payload)]
    started = datetime.now(timezone.utc)
    proc = _run(cmd, cwd=root, timeout=180)
    if proc is None or proc.returncode != 0:
        raise CiError("gh workflow run failed:\n" + (_text(proc) or "no output"))

    run = find_run(gh, repo=repo, workflow=workflow, ref=branch, started=started, cwd=root)
    run_id = str(run.get("databaseId"))
    url = run.get("url")

    conclusion = run.get("conclusion")
    exit_code = 0
    if not watch and conclusion and conclusion != "success":
        exit_code = 1
    if watch:
        watch_proc = _run(
            ["gh", "run", "watch", run_id, *_repo_args(repo),
             "--exit-status", "--interval", "10"],
            cwd=root,
            timeout=timeout,
            capture=False,
        )
        exit_code = 0 if watch_proc is not None and watch_proc.returncode == 0 else 1
        view = _run(
            ["gh", "run", "view", run_id, *_repo_args(repo),
             "--json", "status,conclusion,url"],
            cwd=root,
            timeout=60,
        )
        if view is not None and view.returncode == 0:
            try:
                detail = json.loads(view.stdout or "{}")
                conclusion = detail.get("conclusion", conclusion)
                url = detail.get("url", url)
            except json.JSONDecodeError:
                pass

    downloaded: str | None = None
    if download_dir:
        if not watch:
            raise CiError("--download requires --watch so the artifacts exist before downloading")
        target = Path(download_dir)
        target.mkdir(parents=True, exist_ok=True)
        download_proc = _run(
            ["gh", "run", "download", run_id, *_repo_args(repo), "-D", str(target)],
            cwd=root,
            timeout=600,
        )
        if download_proc is None or download_proc.returncode != 0:
            raise CiError("gh run download failed:\n" + (_text(download_proc) or "no output"))
        downloaded = str(target)

    return {
        "run_id": run_id,
        "url": url,
        "conclusion": conclusion,
        "exit_code": exit_code,
        "downloaded": downloaded,
        "workflow": workflow,
        "ref": branch,
        "spec": spec_rel,
        "backend": "ci",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Dispatch circuit verification to GitHub Actions.")
    parser.add_argument("--spec", default="examples/rc-lowpass.json", help="design spec path")
    parser.add_argument("--repo", help="owner/name (default: current git remote)")
    parser.add_argument("--workflow", default=DEFAULT_WORKFLOW, help="workflow file name")
    parser.add_argument("--ref", help="branch to run (default: current branch)")
    parser.add_argument("--require", help="comma-separated stages that must run (e.g. simulate,erc)")
    parser.add_argument("--watch", action="store_true", help="wait for the run to finish")
    parser.add_argument("--download", help="download artifacts into this directory (implies --watch)")
    parser.add_argument("--timeout", type=int, default=3600, help="watch timeout in seconds")
    args = parser.parse_args(argv)

    try:
        result = dispatch(
            args.spec,
            repo=args.repo,
            workflow=args.workflow,
            ref=args.ref,
            require=args.require,
            watch=args.watch or bool(args.download),
            download_dir=args.download,
            timeout=args.timeout,
        )
    except CiError as exc:
        print(f"CI DISPATCH FAILED: {exc}", file=sys.stderr)
        return 1

    print(f"Dispatched {result['workflow']} on {result['ref']}: {result['url']}")
    if result["conclusion"]:
        print(f"Conclusion: {result['conclusion']}")
    if result["downloaded"]:
        print(f"Artifacts: {result['downloaded']}")
    return int(result["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
