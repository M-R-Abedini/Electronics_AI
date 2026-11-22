# SPDX-License-Identifier: MIT
"""Unit tests for Phase 1-4 placement quality and routing hardening."""

from __future__ import annotations

import json
import os
from pathlib import Path

from eda_agent.placer import refine
from eda_agent.placer.eval import congestion
from eda_agent.placer.intent import model
from eda_agent.placer.ir import (
    Board,
    CopperLayer,
    Courtyard,
    Footprint,
    Net,
    NetClass,
    Outline,
    Pad,
    derive,
)
from eda_agent.placer.place import pipeline
from eda_agent.placer.route import check as route_check_mod
from eda_agent.placer.toolchain import sha256_file


def _mini_board_with_edge_pad() -> Board:
    layers = [
        CopperLayer("F.Cu", "F.Cu", 0),
        CopperLayer("In1.Cu", "In1.Cu", 1, "power"),
        CopperLayer("B.Cu", "B.Cu", 2),
    ]
    outline_pts = [(0.0, 0.0), (20.0, 0.0), (20.0, 20.0), (0.0, 20.0)]
    edge_fp = Footprint(
        ref="J1",
        value="EDGE",
        lib_id="Connector:Edge",
        x=10.0,
        y=0.0,
        rotation=0.0,
        side="F",
        locked=True,
        pads=[Pad("1", "SIG1", (0.0, 0.0), (1.2, 1.2), kind="smd", layers=("F.Cu",))],
        courtyard=Courtyard("file", [[(-0.8, -0.8), (0.8, -0.8), (0.8, 0.8), (-0.8, 0.8)]]),
        uuid="u-j1",
    )
    mid_fp = Footprint(
        ref="R1",
        value="10k",
        lib_id="Resistor:R_0603",
        x=10.0,
        y=10.0,
        rotation=0.0,
        side="F",
        pads=[
            Pad("1", "SIG1", (-0.8, 0.0), (0.8, 0.8), kind="smd", layers=("F.Cu",)),
            Pad("2", "GND", (0.8, 0.0), (0.8, 0.8), kind="smd", layers=("F.Cu",)),
        ],
        courtyard=Courtyard("file", [[(-1.4, -0.6), (1.4, -0.6), (1.4, 0.6), (-1.4, 0.6)]]),
        uuid="u-r1",
    )
    board = Board(
        name="edge_test",
        footprints=[edge_fp, mid_fp],
        outline=[Outline(outer=outline_pts, holes=[])],
        board_area_mm2=400.0,
        copper_layers=layers,
        zones=[{"uuid": "z1", "name": "GND", "net": "GND", "layers": ["In1.Cu"], "outline": [outline_pts]}],
        zone_fill_by_layer_mm2={"In1.Cu": {"GND": 380.0}},
        nets=[Net("SIG1", "Default", 2), Net("GND", "Default", 1)],
        netclasses=[NetClass("Default", 0.15, 0.15, 0.5, 0.3)],
        rules={"min_clearance": 0.15, "min_track_width": 0.15, "copper_edge_clearance": 0.30},
    )
    return derive(board)


def _board_20_parts() -> Board:
    """Synthetic 20-movable-part board (>= PARALLEL_MIN_MOVABLE) to test parallel P7 determinism."""
    layers = [CopperLayer("F.Cu", "F.Cu", 0), CopperLayer("B.Cu", "B.Cu", 1)]
    outline_pts = [(0.0, 0.0), (40.0, 0.0), (40.0, 40.0), (0.0, 40.0)]
    fps = [
        Footprint(
            ref="U1",
            value="MCU",
            lib_id="IC:QFN",
            x=20.0,
            y=20.0,
            rotation=0.0,
            side="F",
            locked=True,
            pads=[
                Pad(str(i + 1), f"N{i}", (-2.0 + 0.4 * (i % 10), -2.0 if i < 10 else 2.0),
                    (0.25, 0.5), kind="smd", layers=("F.Cu",))
                for i in range(20)
            ],
            courtyard=Courtyard("file", [[(-3.0, -3.0), (3.0, -3.0), (3.0, 3.0), (-3.0, 3.0)]]),
            uuid="u-u1",
        )
    ]
    nets = []
    for i in range(20):
        col, row = i % 5, i // 5
        fps.append(
            Footprint(
                ref=f"R{i + 1}",
                value="10k",
                lib_id="Resistor:R_0603",
                x=6.0 + col * 6.5,
                y=6.0 + row * 7.5,
                rotation=0.0,
                side="F",
                pads=[
                    Pad("1", f"N{i}", (-0.7, 0.0), (0.7, 0.7), kind="smd", layers=("F.Cu",)),
                    Pad("2", f"N{(i + 1) % 20}", (0.7, 0.0), (0.7, 0.7), kind="smd", layers=("F.Cu",)),
                ],
                courtyard=Courtyard("file", [[(-1.3, -0.6), (1.3, -0.6), (1.3, 0.6), (-1.3, 0.6)]]),
                uuid=f"u-r{i + 1}",
            )
        )
        nets.append(Net(f"N{i}", "Default", 3))
    board = Board(
        name="p20",
        footprints=fps,
        outline=[Outline(outer=outline_pts, holes=[])],
        board_area_mm2=1600.0,
        copper_layers=layers,
        nets=nets,
        netclasses=[NetClass("Default", 0.15, 0.15, 0.5, 0.3)],
        rules={"min_clearance": 0.15, "min_track_width": 0.15, "copper_edge_clearance": 0.25},
    )
    return derive(board)


def test_classify_unrouted_separates_unattempted_from_escape_and_via_blocked():
    def pad(net, ref, num, layers, attached):
        return {"uuid": f"{ref}-{num}", "net": net, "ref": ref, "pad": num, "layers": layers, "attached": attached}

    finished = {
        "routing": {"vias_by_net": {"N_ESC": 1}},
        "pads": [
            # Completely unattempted 2-pin opposite-side net (0 attached, 0 vias) -> unattempted (NOT via_blocked)
            pad("N_UNATT_OPP", "R1", "1", ["F.Cu"], False),
            pad("N_UNATT_OPP", "R2", "1", ["B.Cu"], False),
            # Completely unattempted 2-pin same-side net (0 attached, 0 vias) -> unattempted (NOT escape_blocked)
            pad("N_UNATT_SAME", "R3", "1", ["F.Cu"], False),
            pad("N_UNATT_SAME", "R4", "1", ["F.Cu"], False),
            # Partially routed opposite-side net without via -> via_blocked
            pad("N_VIA", "U1", "1", ["F.Cu"], True),
            pad("N_VIA", "R5", "1", ["B.Cu"], False),
            # Partially routed net with via -> escape_blocked
            pad("N_ESC", "U1", "2", ["F.Cu"], True),
            pad("N_ESC", "R6", "1", ["B.Cu"], False),
            # All pads escaped -> path_blocked
            pad("N_PATH", "R7", "1", ["F.Cu"], True),
            pad("N_PATH", "R8", "1", ["F.Cu"], True),
        ],
    }

    items = [
        {"items": [{"description": f"Pad 1 [{n}] of X"}, {"description": f"Pad 1 [{n}] of Y"}]}
        for n in ("N_UNATT_OPP", "N_UNATT_SAME", "N_VIA", "N_ESC", "N_PATH")
    ]
    rows = {r["net"]: r for r in route_check_mod.classify_unrouted(items, finished, "timeout")}
    assert rows["N_UNATT_OPP"]["cause"] == "unattempted"
    assert rows["N_UNATT_OPP"]["attached_pads"] == 0
    assert rows["N_UNATT_OPP"]["opposite_sides"] is True
    assert rows["N_UNATT_SAME"]["cause"] == "unattempted"
    assert rows["N_UNATT_SAME"]["opposite_sides"] is False
    assert rows["N_VIA"]["cause"] == "via_blocked"
    assert rows["N_ESC"]["cause"] == "escape_blocked"
    assert rows["N_PATH"]["cause"] == "path_blocked"


def test_route_check_budget_and_kicad_499_cap_constants():
    assert route_check_mod.KICAD_DRC_UNCONNECTED_CAP == 499
    assert route_check_mod.TIME_BUDGET_OVERHEAD_S >= 60
    # A 600s router timeout must have a time budget > 600s so DRC/import overhead never fails falsely.
    assert max(route_check_mod.TIME_BUDGET_S, 600 + route_check_mod.TIME_BUDGET_OVERHEAD_S) > 600


def test_sanitize_drc_keepouts_builds_pad_safe_local_boxes():
    board = _mini_board_with_edge_pad()
    violations = [
        {
            "type": "clearance",
            "items": [
                {"description": "Track [SIG1] on F.Cu, length 1.0 mm", "pos": {"x": 5.0, "y": 5.0}},
                # On top of pad R1-1 at (9.2, 10.0) -> must be skipped so pad escape is never blocked!
                {"description": "Track [SIG1] on F.Cu, length 1.0 mm", "pos": {"x": 9.2, "y": 10.0}},
            ],
        },
        {
            "type": "drill_out_of_range",
            "items": [
                {"description": "Via [GND] on F.Cu - B.Cu", "pos": {"x": 15.0, "y": 15.0}},
            ],
        },
    ]
    kps = route_check_mod._sanitize_drc_keepouts(violations, board, half_mm=0.25)
    # (5, 5) on F.Cu -> 1 box; (9.2, 10.0) skipped (overlaps R1-1); (15, 15) on F.Cu..B.Cu -> 3 layers.
    assert len(kps) == 4
    f_cu_centers = [
        (round(sum(p[0] for p in k.outline) / 4, 2), round(sum(p[1] for p in k.outline) / 4, 2))
        for k in kps
        if k.layer == "F.Cu"
    ]
    assert (5.0, 5.0) in f_cu_centers
    assert (15.0, 15.0) in f_cu_centers
    assert (9.2, 10.0) not in f_cu_centers


def test_parallel_solve_is_bit_for_bit_deterministic_and_uses_full_budget():
    board = _board_20_parts()
    intent = {
        "parts": {
            "U1": {"role": model.claim("ic", "board", 1.0)},
            **{f"R{i}": {"role": model.claim("resistor", "board", 1.0)} for i in range(1, 21)},
        },
        "blocks": [{"name": "B0", "parts": ["U1", "R1", "R2", "R3"]}],
        "diff_pairs": [],
        "nets": {},
    }
    p_par1, i_par1 = pipeline.solve(board, intent=intent, floorplan=True, mode="sa_only", seed=0, parallel=True)
    p_par2, _i_par2 = pipeline.solve(board, intent=intent, floorplan=True, mode="sa_only", seed=0, parallel=True)
    p_seq, i_seq = pipeline.solve(board, intent=intent, floorplan=True, mode="sa_only", seed=0, parallel=False)

    assert i_par1["floorplan"]["iteration_budget_divisor"] == 1
    assert i_seq["floorplan"]["iteration_budget_divisor"] == 1
    if (os.cpu_count() or 1) > 1:
        assert i_par1["floorplan"]["parallel_workers"] >= 2
    assert i_seq["floorplan"]["parallel_workers"] == 1

    poses_par1 = [(fp.ref, fp.x, fp.y, fp.rotation, fp.side) for fp in p_par1.footprints]
    poses_par2 = [(fp.ref, fp.x, fp.y, fp.rotation, fp.side) for fp in p_par2.footprints]
    poses_seq = [(fp.ref, fp.x, fp.y, fp.rotation, fp.side) for fp in p_seq.footprints]
    assert poses_par1 == poses_par2
    assert poses_par1 == poses_seq


def test_congestion_capacity_scale_probes_near_saturation():
    board = _board_20_parts()
    base = congestion.estimate(board, capacity_scale=1.0)
    scaled = congestion.estimate(board, capacity_scale=0.5)
    assert base["available"] and scaled["available"]
    assert scaled["edge_capacity"] <= base["edge_capacity"]
    assert scaled["peak_utilization"] >= base["peak_utilization"] - 1e-6


def test_refine_baseline_cache_hits_and_min_improvement_guard(tmp_path: Path, monkeypatch):
    source = tmp_path / "placed.kicad_pcb"
    source.write_bytes(b"original PCB")
    report = {
        "kind": "place_board",
        "inputs": {
            "board": {"path": str(source), "sha256": sha256_file(source)},
            "intent": {"path": None, "sha256": "intent-hash-1"},
        },
        "stages": {"place": {"fixed": {"J1": "locked"}}},
        "artifacts": {"placed_board": str(source)},
        "config": {},
    }
    (tmp_path / "run_report.json").write_text(json.dumps(report), encoding="utf-8")

    board = _mini_board_with_edge_pad()
    monkeypatch.setattr(refine, "read_board", lambda _: board)
    monkeypatch.setattr(
        refine,
        "resolve",
        lambda *_: ({"parts": {}, "nets": {}, "diff_pairs": [], "mechanical": {}}, {"sha256": "intent-hash-1"}),
    )
    monkeypatch.setattr(refine.legality, "check", lambda *a, **kw: [])

    def fake_write(_src, _board, dest, **kwargs):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"copy-stripped")
        return dest

    monkeypatch.setattr(refine, "write_board", fake_write)

    calls: list[str] = []
    after_unrouted = {"val": 4}  # trial 1: baseline=5 -> after=4 (delta=1)

    def fake_route(path, *, router_timeout_s, **kwargs):
        is_base = Path(path).parent.name == "baseline"
        calls.append("baseline" if is_base else "candidate")
        u = 5 if is_base else after_unrouted["val"]
        return {
            "metrics": {
                "settled": True,
                "denominator": 10,
                "unrouted": u,
                "unrouted_signal": u,
                "unrouted_plane": 0,
                "coverage": round((10 - u) / 10, 4),
            },
            "drc": {"router_caused": [], "new_non_routing": []},
            "passed": True,
            "gates": [{"name": "drc_router_caused", "passed": True}],
            "artifacts": {"routed_board": str(path)},
        }

    monkeypatch.setattr(refine, "route_check", fake_route)

    trials = tmp_path / "trials"
    # Trial 1: cache miss -> calls BOTH baseline & candidate; min_improvement=2 rejects delta=1.
    r1 = refine.refine(str(tmp_path), {"ref": "R1", "rotation_deg": 90}, trials, route_timeout_s=15, min_improvement=2)
    assert r1["status"] == "rejected"
    assert r1["baseline_cached"] is False
    assert calls.count("baseline") == 1 and calls.count("candidate") == 1

    # Trial 2: cache HIT -> skips baseline completely! delta=2 >= min_improvement=2 -> accepted!
    after_unrouted["val"] = 3
    r2 = refine.refine(str(tmp_path), {"ref": "R1", "rotation_deg": 180}, trials, route_timeout_s=15, min_improvement=2)
    assert r2["status"] == "accepted"
    assert r2["baseline_cached"] is True
    assert calls.count("baseline") == 1 and calls.count("candidate") == 2
