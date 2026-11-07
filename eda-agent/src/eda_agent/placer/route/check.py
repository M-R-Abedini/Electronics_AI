# SPDX-License-Identifier: MIT
"""Strict route check: KiCad -> DSN -> Freerouting -> SES -> refill -> DRC (plan F0a).

The board file given by the user is never written.  Everything happens on
copies inside one run directory, and the result is one ``run_report.json``
(architecture 4.6) whose gates say whether the routed copy may be trusted.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path

from .. import report as run_report
from ..toolchain import RUNTIME_DIR, kicad, sha256_file
from . import drc, dsn
from .freerouting import RouterConfig, route

HELPER = Path(__file__).resolve().parents[1] / "io" / "kicad_pcbnew_helper.py"
PROJECT_SIDECARS = (".kicad_pro", ".kicad_dru")  # .kicad_prl is per-user view state
TIME_BUDGET_S = 300  # plan F0a acceptance: whole path <= 5 minutes on StickHub


class RouteCheckError(RuntimeError):
    pass


def _helper(*args: str | Path, timeout_s: int = 900) -> None:
    cmd = [str(kicad().python), "-I", str(HELPER), *map(str, args)]
    done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s, check=False)
    if done.returncode != 0:
        noise = "swig/python detected a memory leak"
        err = "\n".join(line for line in done.stderr.splitlines() if noise not in line)
        raise RouteCheckError(f"pcbnew helper '{args[0]}' failed: {err.strip()[-1500:]}")


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def stage_input(board: Path, run_dir: Path) -> tuple[Path, dict]:
    """Copy the board and its project sidecars; the project file is mandatory."""
    board = board.resolve()
    if board.suffix != ".kicad_pcb" or not board.is_file():
        raise RouteCheckError(f"not a .kicad_pcb file: {board}")
    project = board.with_suffix(".kicad_pro")
    if not project.is_file():
        raise RouteCheckError(f"{project.name} missing next to the board; without it DRC ignores the "
                              "project rules (architecture 7.3)")
    target = run_dir / "input"
    target.mkdir(parents=True, exist_ok=True)
    copied = target / board.name
    shutil.copyfile(board, copied)
    files = {"board": {"path": str(board), "sha256": sha256_file(board)}}
    for suffix in PROJECT_SIDECARS:
        side = board.with_suffix(suffix)
        if side.is_file():
            shutil.copyfile(side, copied.with_suffix(suffix))
            files[suffix.lstrip(".")] = {"path": str(side), "sha256": sha256_file(side)}
    return copied, files


def keepouts_from(obstacles: list[dict]) -> tuple[list[dsn.Keepout], list[dict]]:
    """DSN keepouts for the obstacles KiCad does not export; skipped ones carry a reason."""
    keep, skipped = [], []
    for row in obstacles:
        reason = ("overlaps pads " + ", ".join(row["overlapping_pads"]) if row["overlapping_pads"]
                  else f"carries net {row['net']}" if row.get("net") else None)
        if reason:
            skipped.append({"uuid": row["uuid"], "kind": row["kind"], "source": row["source"], "reason": reason})
            continue
        for i, outline in enumerate(row["outlines"]):
            keep.append(dsn.Keepout(name=f"{row['kind']}_{row['uuid'][:8]}_{i}", layer=row["copper"],
                                    outline=tuple((float(x), float(y)) for x, y in outline)))
    return keep, skipped


def _by_kind(obstacles: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in obstacles:
        counts[row["kind"]] = counts.get(row["kind"], 0) + 1
    return dict(sorted(counts.items()))


def classify_unrouted(items: list[dict], finished: dict, stop_reason: str) -> list[dict]:
    """One row per DRC ``unconnected_items`` entry, with a cause decided per net.

    KiCad picks which two items of the disconnected islands it reports, and the
    pick depends on item UUIDs that SES import regenerates, so the same session
    can be reported through different items.  The cause therefore uses only
    facts of the net, which the session fixes:

    via_blocked    the net has SMD pads on two different copper layers, no
                   through pad, and the router placed no via on it
    escape_blocked some pad of the net has no routed copper touching it
    path_blocked   every pad escaped but the router did not join the islands
    The reported items are kept as evidence; the run's stop reason
    (converged / pass_limit / timeout) is attached to every row.
    """
    by_net: dict[str, list[dict]] = {}
    for pad in finished["pads"]:
        by_net.setdefault(pad["net"], []).append(pad)
    vias = finished["routing"].get("vias_by_net", {})
    rows = []
    for entry in items:
        ends = entry.get("items", [])
        descs = [e.get("description", "") for e in ends]
        net = next((n for n in map(drc.item_net, descs) if n is not None), None)
        net_pads = by_net.get(net or "", [])
        smd_layers = {p["layers"][0] for p in net_pads if len(p["layers"]) == 1}
        through = any(len(p["layers"]) > 1 for p in net_pads)
        unescaped = sorted(f"{p['ref']}-{p['pad']}" for p in net_pads if not p["attached"])
        if len(smd_layers) > 1 and not through and not vias.get(net or "", 0):
            cause = "via_blocked"
        elif unescaped:
            cause = "escape_blocked"
        else:
            cause = "path_blocked"
        rows.append({"stage": "S6c", "net": net, "cause": cause, "run_stop": stop_reason,
                     "unescaped_pads": unescaped,
                     "evidence": [{"description": d, "pos": e.get("pos")} for d, e in zip(descs, ends)]})
    rows.sort(key=lambda r: (r["net"] or "", r["cause"]))
    return rows


def route_check(board: str | Path, *, out_dir: str | Path | None = None,
                config: RouterConfig | None = None, neck_width_um: float | None = None,
                obstacle_keepouts: bool = True) -> dict:
    started = time.monotonic()
    board = Path(board)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir = (Path(out_dir) if out_dir else RUNTIME_DIR / "route_check" / board.stem / stamp).resolve()
    if run_dir.exists() and any(run_dir.iterdir()):
        raise RouteCheckError(f"run directory not empty: {run_dir}")
    work, output = run_dir / "work", run_dir / "output"
    work.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    timings: dict[str, float] = {}

    def tick(name: str, t0: float) -> float:
        now = time.monotonic()
        timings[name] = round(now - t0, 2)
        return now

    t = time.monotonic()
    source, inputs = stage_input(board, run_dir)
    reference = drc.run(source, work / "drc_input.json")
    t = tick("drc_input", t)

    _helper("prepare", source, work / "prepared.kicad_pcb", work / "board.dsn", work / "prepare.json")
    prepared = _load(work / "prepare.json")
    if not prepared["dsn_exported"]:
        raise RouteCheckError("KiCad failed to export the Specctra DSN")
    keepouts, skipped = keepouts_from(prepared["obstacles"]) if obstacle_keepouts else ([], [])
    text = (work / "board.dsn").read_text(encoding="utf-8")
    route_dsn = work / "route.dsn"
    route_dsn.write_text(dsn.add_keepouts(text, keepouts), encoding="utf-8", newline="\n")
    t = tick("prepare", t)

    neck = neck_width_um if neck_width_um is not None else round(prepared["min_track_width_mm"] * 1000, 3)
    cfg = replace(config, neck_width_um=neck) if config else RouterConfig(neck_width_um=neck)
    ses = work / "route.ses"
    routed = route(route_dsn, ses, work / "freerouting", cfg)
    t = tick("freerouting", t)
    if routed.session is None:
        raise RouteCheckError(f"Freerouting produced no session (exit {routed.returncode}, "
                              f"stop {routed.stop_reason}); see {work / 'freerouting'}")

    out_board = output / f"{board.stem}.routed.kicad_pcb"
    _helper("finish", source, ses, out_board, work / "finish.json")
    finished = _load(work / "finish.json")
    for suffix in PROJECT_SIDECARS:
        side = source.with_suffix(suffix)
        if side.is_file() and not out_board.with_suffix(suffix).is_file():
            shutil.copyfile(side, out_board.with_suffix(suffix))
    t = tick("import_refill", t)

    result = drc.run(out_board, work / "drc_routed.json")
    tick("drc_routed", t)
    attribution = drc.attribute(result, reference)
    unrouted_items = [u for u in result.get("unconnected_items", [])]
    failures = classify_unrouted(unrouted_items, finished, routed.stop_reason)

    denominator = prepared["denominator"]
    by_tracks = denominator - finished["unrouted_tracks_only"]
    via_zone = finished["unrouted_tracks_only"] - finished["unrouted_final"]
    unsupported = 0  # no connection class is declared unsupported in F0a
    unrouted = finished["unrouted_final"]
    total_s = round(time.monotonic() - started, 2)
    metrics = {
        "denominator": denominator,
        "routed_by_tracks": by_tracks,
        "connected_via_zone": via_zone,
        "unsupported": unsupported,
        "unrouted": unrouted,
        "coverage": round((denominator - unrouted) / denominator, 4) if denominator else 1.0,
        "vias": finished["routing"]["vias"],
        "track_length_mm": finished["routing"]["length_mm"],
        "track_width_histogram_mm": finished["routing"]["width_histogram_mm"],
        "session_sha256": sha256_file(ses),
        "router_passes": routed.passes,
        "router_stop": routed.stop_reason,
        "seconds_total": total_s,
    }
    gates = [
        run_report.gate("session_imported", finished["imported"], True),
        run_report.gate("drc_router_caused", attribution.counts()["router_caused"], 0, "=="),
        run_report.gate("drc_new_non_routing", attribution.counts()["new_non_routing"], 0, "=="),
        run_report.gate("denominator_stable", finished["denominator"], denominator, "=="),
        run_report.gate("accounting", by_tracks + via_zone + unsupported + unrouted, denominator, "=="),
        run_report.gate("unrouted_listed", len(failures), unrouted, "=="),
        run_report.gate("time_budget_s", total_s, TIME_BUDGET_S, "<="),
    ]
    report = run_report.build(
        kind="route_check",
        inputs=inputs,
        config={"router": asdict(cfg), "obstacle_keepouts": obstacle_keepouts},
        stages={
            "prepare": {k: v for k, v in prepared.items() if k != "obstacles"},
            "obstacle_keepouts": {"exported": len(keepouts), "by_kind": _by_kind(prepared["obstacles"]),
                                  "skipped": skipped},
            "freerouting": routed.summary(),
            "finish": {k: v for k, v in finished.items() if k != "pads"},
            "drc": attribution.counts(),
        },
        metrics=metrics,
        gates=gates,
        failures=failures,
        drc={"router_caused": attribution.router, "new_non_routing": attribution.new_other,
             "pre_existing": attribution.pre_existing,
             "input_pin_violations_seen_by_router": routed.pre_existing_violations},
        timings=timings,
        artifacts={"run_dir": str(run_dir), "routed_board": str(out_board), "session": str(ses),
                   "dsn": str(route_dsn)},
    )
    run_report.write(report, run_dir / "run_report.json")
    return report
