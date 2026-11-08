# SPDX-License-Identifier: MIT
"""``pixi run bench``: every baseline on a corpus split, evaluated and route-checked (plan F0b, task 5).

Each (board, baseline) job runs end to end on one of ``jobs`` threads: the
placement in a child process with a hard time limit, then the strict route
check.  Routing of one board therefore overlaps placement of the next, and at
most ``jobs`` (<= 4) Freerouting runs are alive at once (its autorouter is
single-threaded).  A baseline slower than ``PLACE_TIMEOUT_S`` is recorded as a
timeout instead of being waited for.  Results are collected in submission
order, never completion order, so the report is deterministic for a fixed
seed (architecture 10).  Each board gets a gallery; the run gets
``bench.json`` and ``bench.md``.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from . import corpus
from .baselines import BASELINES
from .eval.score import evaluate
from .io.kicad import read_board, write_board
from .place.problem import fixed_parts
from .render import Panel, gallery
from .route.check import route_check
from .toolchain import RUNTIME_DIR

MAX_ROUTERS = 4
PLACE_TIMEOUT_S = 600  # declared parameter: a floor baseline slower than this is reported as a timeout
ROUTE_TIMEOUT_S = 300  # declared parameter: one Freerouting budget for every placement compared (C1 fairness)


def _place_worker(board_path: str, baseline: str, seed: int, out_dir: str, result_json: str) -> None:
    """Child process: one baseline on one board -> written board + evaluation (JSON file)."""
    t0 = time.monotonic()
    board = read_board(board_path)
    placed, info = BASELINES[baseline](board, seed)
    seconds = round(time.monotonic() - t0, 2)
    if baseline == "expert":
        target = Path(board_path)
    else:
        target = write_board(board_path, placed, Path(out_dir) / f"{baseline}.kicad_pcb", strip_routing=True)
    fixed = set(fixed_parts(board))  # one S1 set for every baseline and for the expert (P2)
    Path(result_json).write_text(json.dumps({"baseline": baseline, "board_file": str(target), "place_seconds": seconds,
                                             "info": info, "evaluation": evaluate(placed, fixed=fixed)}),
                                 encoding="utf-8")


def _job(entry: corpus.Entry, baseline: str, seed: int, out: Path, route: bool, route_timeout_s: int) -> dict:
    work = out / entry.name
    work.mkdir(parents=True, exist_ok=True)
    result_json = work / f"{baseline}.result.json"
    cmd = [sys.executable, "-m", "eda_agent.placer.bench", "--worker", str(entry.path), baseline, str(seed),
           str(work), str(result_json)]
    try:
        done = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=PLACE_TIMEOUT_S, check=False)
    except subprocess.TimeoutExpired:
        return {"error": f"placement timeout after {PLACE_TIMEOUT_S} s"}
    if done.returncode != 0 or not result_json.is_file():
        return {"error": "placement failed: " + done.stderr.strip()[-600:]}
    placed = json.loads(result_json.read_text(encoding="utf-8"))
    if route:
        try:
            report = route_check(placed["board_file"], out_dir=work / f"route_{baseline}", reference_board=entry.path,
                                 router_timeout_s=route_timeout_s)
            placed["route"] = {"passed": report["passed"], "metrics": report["metrics"],
                               "failed_gates": [g["name"] for g in report["gates"] if not g["passed"]],
                               "routed_board": report["artifacts"]["routed_board"]}
        except Exception as exc:  # noqa: BLE001  # one failing board must not stop the benchmark
            placed["route"] = {"error": f"{type(exc).__name__}: {exc}"}
    return placed


def _row(name: str, baseline: str, r: dict) -> dict:
    if "error" in r:
        return {"board": name, "baseline": baseline, "error": r["error"]}
    rt = r.get("route", {})
    ev = r["evaluation"]
    return {
        "board": name, "baseline": baseline, "place_s": r["place_seconds"],
        "legal_strict": ev["legality"]["strict_violations"], "legal_unwaived": ev["legality"]["violations"],
        "hpwl_signal_mm": ev["metrics"]["hpwl"]["signal_mm"],
        "crossings": ev["metrics"]["crossings"]["count"],
        "rudy_over": ev["metrics"]["rudy"].get("over_capacity_fraction"),
        "pro_score_v0": ev["pro_score"]["score"],
        "route": ({k: rt["metrics"].get(k) for k in ("coverage", "unrouted", "vias", "track_length_mm", "seconds_total",
                                                     "router_stop", "deterministic")}
                  if "metrics" in rt else rt),
        "route_failed_gates": rt.get("failed_gates"),
        "info": r["info"],
    }


def _panel(name: str, baseline: str, r: dict) -> Panel:
    """Show every placement with the copper Freerouting gave it, so panels compare like with like (C1)."""
    routed = r.get("route", {}).get("routed_board")
    shown = routed if routed and Path(routed).is_file() else r["board_file"]
    title = f"{name} / {baseline}" + (" (Freerouting result)" if shown == routed else "")
    return Panel(title, read_board(shown), r["evaluation"], r.get("route", {}).get("metrics"))


def run(names: list[str], baselines: list[str], *, seed: int = 0, route: bool = True, jobs: int = 4,
        out: Path | None = None, route_timeout_s: int = ROUTE_TIMEOUT_S) -> dict:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = (out or RUNTIME_DIR / "bench" / stamp).resolve()
    out.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    entries = [corpus.get(n) for n in names]
    workers = max(1, min(MAX_ROUTERS, jobs))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [(e, b, pool.submit(_job, e, b, seed, out, route, route_timeout_s)) for e in entries for b in baselines]
        results = [(e, b, f.result()) for e, b, f in futures]
    rows = []
    for e in entries:
        panels = []
        for e2, b, r in results:
            if e2.name != e.name:
                continue
            rows.append(_row(e.name, b, r))
            if "error" not in r:
                panels.append(_panel(e.name, b, r))
        if panels:
            gallery(panels, out / e.name / "gallery.html", title=f"{e.name}: baselines (seed {seed})")
    summary = {"created_utc": stamp, "seed": seed, "boards": names, "baselines": baselines, "jobs": workers,
               "place_timeout_s": PLACE_TIMEOUT_S, "route_timeout_s": route_timeout_s, "total_seconds": round(time.monotonic() - started, 1),
               "rows": rows}
    (out / "bench.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    (out / "bench.md").write_text(markdown(summary), encoding="utf-8")
    return summary


def markdown(summary: dict) -> str:
    head = ("| board | baseline | legal (strict / unwaived) | HPWL signal mm | crossings | coverage | unrouted | vias "
            "| Pro Score v0 | place s | route s |\n|---|---|---|---|---|---|---|---|---|---|---|\n")
    lines = []
    for r in summary["rows"]:
        if "error" in r:
            lines.append(f"| {r['board']} | {r['baseline']} | {r['error']} | | | | | | | | |")
            continue
        rt = r["route"] if isinstance(r["route"], dict) else {}
        cov = rt.get("coverage")
        cov_text = f"{cov:.1%}" if cov is not None else rt.get("error", "-")
        if rt.get("router_stop") == "timeout":
            cov_text += " (router budget hit)"
        lines.append(f"| {r['board']} | {r['baseline']} | {r['legal_strict']} / {r['legal_unwaived']} "
                     f"| {r['hpwl_signal_mm']:.0f} | {r['crossings']} | {cov_text} "
                     f"| {rt.get('unrouted', '-')} | {rt.get('vias', '-')} | {r['pro_score_v0']} | {r['place_s']} "
                     f"| {rt.get('seconds_total', '-')} |")
    return (f"# Bench {summary['created_utc']} (seed {summary['seed']})\n\n"
            f"Total {summary['total_seconds']} s, {summary['jobs']} parallel jobs, placement limit "
            f"{summary['place_timeout_s']} s.\n\n" + head + "\n".join(lines) + "\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="bench", description=__doc__)
    ap.add_argument("--split", default="dev", help="corpus split (dev, validation, validation_ext, stress)")
    ap.add_argument("--boards", help="comma-separated corpus names instead of a split")
    ap.add_argument("--baselines", default=",".join(BASELINES))
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--no-route", action="store_true")
    ap.add_argument("--route-timeout", type=int, default=ROUTE_TIMEOUT_S, help="Freerouting budget per run, seconds")
    ap.add_argument("--out")
    ap.add_argument("--worker", nargs=5, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if args.worker:
        board, baseline, seed, out_dir, result_json = args.worker
        _place_worker(board, baseline, int(seed), out_dir, result_json)
        return 0
    if args.split == "test" and not args.boards:
        raise corpus.SealedBoardError("the test split is sealed (plan section 3)")
    names = args.boards.split(",") if args.boards else [e.name for e in corpus.entries(args.split)]
    summary = run(names, args.baselines.split(","), seed=args.seed, route=not args.no_route, jobs=args.jobs,
                  out=Path(args.out) if args.out else None, route_timeout_s=args.route_timeout)
    print(markdown(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
