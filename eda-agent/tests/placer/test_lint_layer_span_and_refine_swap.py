# SPDX-License-Identifier: MIT
"""Regression tests for exact HDI via layer-span in lint.py and pairwise swap_with_ref in refine.py."""

from __future__ import annotations

import json
from pathlib import Path

from eda_agent.placer import refine, toolchain
from eda_agent.placer.eval import lint
from eda_agent.placer.intent import infer, model
from eda_agent.placer.ir import (
    Board,
    CopperLayer,
    Courtyard,
    Footprint,
    Net,
    NetClass,
    Outline,
    Pad,
    Stackup,
    StackupLayer,
    derive,
)


def _make_6l_board() -> Board:
    layers = [
        CopperLayer("F.Cu", "F.Cu", 0, "signal"),
        CopperLayer("In1.Cu", "In1.Cu", 1, "power"),
        CopperLayer("In2.Cu", "In2.Cu", 2, "signal"),
        CopperLayer("In3.Cu", "In3.Cu", 3, "signal"),
        CopperLayer("In4.Cu", "In4.Cu", 4, "power"),
        CopperLayer("B.Cu", "B.Cu", 5, "signal"),
    ]
    u1 = Footprint(
        "U1", "MCU", "QFN", 10.0, 10.0, 0.0, "F", locked=True,
        pads=[
            Pad("1", "USB_DP", (-1.5, 0.0), (0.35, 0.35), layers=("F.Cu",)),
            Pad("2", "GND", (0.0, 0.0), (1.0, 1.0), layers=("F.Cu",)),
            Pad("3", "+3V3", (1.5, 0.0), (0.35, 0.35), layers=("F.Cu",)),
        ],
        courtyard=Courtyard("file", [[(-2.5, -2.5), (2.5, -2.5), (2.5, 2.5), (-2.5, 2.5)]]), uuid="u-U1",
    )
    r1 = Footprint(
        "R1", "10k", "R_0402", 16.0, 10.0, 0.0, "F", locked=False,
        pads=[Pad("1", "SIG1", (-0.4, 0.0), (0.4, 0.4)), Pad("2", "GND", (0.4, 0.0), (0.4, 0.4))],
        courtyard=Courtyard("file", [[(-0.8, -0.4), (0.8, -0.4), (0.8, 0.4), (-0.8, 0.4)]]), uuid="u-R1",
    )
    r2 = Footprint(
        "R2", "10k", "R_0402", 20.0, 10.0, 0.0, "F", locked=False,
        pads=[Pad("1", "SIG2", (-0.4, 0.0), (0.4, 0.4)), Pad("2", "GND", (0.4, 0.0), (0.4, 0.4))],
        courtyard=Courtyard("file", [[(-0.8, -0.4), (0.8, -0.4), (0.8, 0.4), (-0.8, 0.4)]]), uuid="u-R2",
    )
    zones = [
        {"uuid": "z-gnd1", "net": "GND", "layers": ["In1.Cu"], "filled": True,
         "filled_area_mm2": {"In1.Cu": 550.0},
         "outline": [[(0.0, 0.0), (30.0, 0.0), (30.0, 20.0), (0.0, 20.0)]]},
        {"uuid": "z-gnd4", "net": "GND", "layers": ["In4.Cu"], "filled": True,
         "filled_area_mm2": {"In4.Cu": 550.0},
         "outline": [[(0.0, 0.0), (30.0, 0.0), (30.0, 20.0), (0.0, 20.0)]]},
    ]
    b = Board(
        name="hdi_test",
        footprints=[u1, r1, r2],
        outline=[Outline([(0.0, 0.0), (30.0, 0.0), (30.0, 20.0), (0.0, 20.0)])],
        board_area_mm2=600.0,
        copper_layers=layers,
        netclasses=[NetClass("Default", 0.15, 0.15, 0.45, 0.25)],
        nets=[Net("GND", "Default", 3), Net("+3V3", "Default", 1), Net("USB_DP", "Default", 1),
              Net("SIG1", "Default", 1), Net("SIG2", "Default", 1)],
        zones=zones,
        zone_fill_by_layer_mm2={"In1.Cu": {"GND": 550.0}, "In4.Cu": {"GND": 550.0}},
        stackup=Stackup("complete", [StackupLayer(l.name, "copper") for l in layers], []),
        rules={"min_clearance": 0.12, "min_track_width": 0.12, "min_via_diameter": 0.45, "min_through_drill": 0.25},
    )
    derive(b)
    return b


def test_lint_blind_microvia_does_not_bridge_unspanned_layers():
    b = _make_6l_board()
    name_to_canon = {cl.name: cl.canonical for cl in b.copper_layers}
    tracks = [
        {"net": "USB_DP", "layer": "F.Cu", "width": 0.15, "points": [(5.0, 5.0), (7.0, 5.0)]},
        {"net": "USB_DP", "layer": "B.Cu", "width": 0.15, "points": [(7.0, 5.0), (9.0, 5.0)]},
    ]
    micro_only = [{"net": "USB_DP", "x": 7.0, "y": 5.0, "diameter": 0.20, "drill": 0.10, "layers": ["F.Cu", "In1.Cu"]}]
    nodes, adj = lint._copper_graph_for_net(b, "USB_DP", tracks, micro_only, name_to_canon)
    path_mm, _ = lint._shortest_copper_path(
        nodes, adj,
        [(5.0, 5.0, {"F.Cu"}, 0.2)],
        [(9.0, 5.0, {"B.Cu"}, 0.2)],
    )
    assert path_mm is None

    thru_via = [{"net": "USB_DP", "x": 7.0, "y": 5.0, "diameter": 0.45, "drill": 0.25, "layers": ["F.Cu", "B.Cu"]}]
    nodes2, adj2 = lint._copper_graph_for_net(b, "USB_DP", tracks, thru_via, name_to_canon)
    path_mm2, used_via = lint._shortest_copper_path(
        nodes2, adj2,
        [(5.0, 5.0, {"F.Cu"}, 0.2)],
        [(9.0, 5.0, {"B.Cu"}, 0.2)],
    )
    assert path_mm2 is not None and used_via is True


def test_lint_return_via_requires_spanning_both_reference_planes():
    b = _make_6l_board()
    # Signal transitions F.Cu (ref In1.Cu GND) -> B.Cu (ref In4.Cu GND) via a through-via at (10, 5).
    # A blind L1-L2 GND microvia at (10.5, 5) does NOT connect In1.Cu to In4.Cu, so return_via must fire!
    b.routing = {
        "tracks": [
            {"net": "USB_DP", "layer": "F.Cu", "width": 0.15, "points": [(8.0, 5.0), (10.0, 5.0)]},
            {"net": "USB_DP", "layer": "B.Cu", "width": 0.15, "points": [(10.0, 5.0), (12.0, 5.0)]},
        ],
        "vias": [
            {"net": "USB_DP", "x": 10.0, "y": 5.0, "diameter": 0.45, "drill": 0.25, "layers": ["F.Cu", "B.Cu"]},
            {"net": "GND", "x": 10.5, "y": 5.0, "diameter": 0.20, "drill": 0.10, "layers": ["F.Cu", "In1.Cu"]},
        ],
    }
    b.routing_items = 4
    intent = {
        "schema": model.SCHEMA,
        "schema_version": model.SCHEMA_VERSION,
        "parts": {},
        "nets": {"USB_DP": {"class": model.claim("high_speed", "board", 1.0)}},
    }
    res_blind = lint.audit(b, intent=intent)
    assert any(f["rule"] == "return_via" and f.get("net") == "USB_DP" for f in res_blind["findings"])

    # Adding a through-via GND stitching In1.Cu <-> In4.Cu clears the return_via warning.
    b.routing["vias"].append(
        {"net": "GND", "x": 10.6, "y": 5.0, "diameter": 0.45, "drill": 0.25, "layers": ["F.Cu", "B.Cu"]}
    )
    res_thru = lint.audit(b, intent=intent)
    assert not any(f["rule"] == "return_via" and f.get("net") == "USB_DP" for f in res_thru["findings"])


def test_refine_swap_with_ref_preview_swaps_both_centers(tmp_path: Path):
    b = _make_6l_board()
    board_path = tmp_path / "demo.kicad_pcb"
    board_path.write_text("(kicad_pcb)", encoding="utf-8")
    intent_obj = infer.draft(b)
    intent_obj["board"] = {"name": "hdi_test", "sha256": "0" * 64}
    intent_path = tmp_path / "demo.design_intent.json"
    intent_path.write_text(json.dumps(intent_obj), encoding="utf-8")
    rep_path = tmp_path / "run_report.json"
    rep = {
        "kind": "place_board",
        "passed": True,
        "inputs": {
            "board": {"path": str(board_path), "sha256": toolchain.sha256_file(board_path)},
            "intent": {"path": str(intent_path), "sha256": model.intent_hash(intent_obj)},
        },
        "stages": {"place": {"fixed": {"U1": "locked"}}},
        "artifacts": {"placed_board": str(board_path)},
    }
    rep_path.write_text(json.dumps(rep), encoding="utf-8")

    orig_read, orig_hash = refine.read_board, refine.sha256_file
    try:
        refine.read_board = lambda _p: b
        refine.sha256_file = lambda _p: toolchain.sha256_file(board_path)
        preview = refine.refine(
            str(rep_path),
            {"ref": "R1", "swap_with_ref": "R2", "confidence": 0.9},
            tmp_path / "out",
            preview=True,
        )
        rejected_fixed = refine.refine(
            str(rep_path),
            {"ref": "R1", "swap_with_ref": "U1", "confidence": 0.9},
            tmp_path / "out",
            preview=True,
        )
    finally:
        refine.read_board, refine.sha256_file = orig_read, orig_hash

    assert preview["status"] == "preview"
    assert preview["pose_after"]["x_mm"] == 20.0
    assert preview["swap_pose_after"]["ref"] == "R2"
    assert preview["swap_pose_after"]["x_mm"] == 16.0
    assert rejected_fixed["status"] == "rejected"
