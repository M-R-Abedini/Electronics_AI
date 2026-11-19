# SPDX-License-Identifier: MIT
"""Unit tests for Step 1 general hardening:
  1. Unconnected/fiducial & NPTH pad_keepout + KiCad 10 unconnected-(...) mechanical locking.
  2. Protruding-pad local_box enclosure and same-side pad_overlap legality check.
  3. Pad-safe inner-body DSN wire_keepout under crystals & converter inductors (zero pin trapping).
  4. Post-route DRC sanitizer UUID extraction (only routing items, never pads/locked).
  5. Expanded S6a critical-passive inner-plane taps on 4L+ boards.
"""

from __future__ import annotations

from eda_agent.placer.eval import legality
from eda_agent.placer.ir import (
    Board,
    CopperLayer,
    Courtyard,
    Footprint,
    LayerRole,
    Net,
    NetClass,
    Outline,
    Pad,
    derive,
)
from eda_agent.placer.place import problem
from eda_agent.placer.route import check as route_check_mod
from eda_agent.placer.route import s6a
from shapely.geometry import Point, Polygon


def _claim(value, confidence=0.95):
    return {"value": value, "source": {"kind": "motif"}, "confidence": confidence, "confirmed_by_user": False}


def test_fixed_parts_locks_kicad10_unconnected_fiducials():
    sq = [(-0.5, -0.5), (0.5, -0.5), (0.5, 0.5), (-0.5, 0.5)]
    fid = Footprint(
        "FID1", "Fid", "Fiducial:1mm", 10.0, 10.0, 0.0, "F", locked=False,
        pads=[Pad("Fid1", "unconnected-(FID1-FID*-PadFid1)", (0.0, 0.0), (1.0, 1.0), layers=("F.Cu",))],
        courtyard=Courtyard("file", [sq]), uuid="u-fid1",
    )
    res = Footprint(
        "R1", "10k", "R:0603", 15.0, 15.0, 0.0, "F", locked=False,
        pads=[
            Pad("1", "SIG1", (-0.5, 0.0), (0.5, 0.5), layers=("F.Cu",)),
            Pad("2", "GND", (0.5, 0.0), (0.5, 0.5), layers=("F.Cu",)),
        ],
        courtyard=Courtyard("file", [sq]), uuid="u-r1",
    )
    board = derive(Board(
        name="fid_test",
        footprints=[fid, res],
        outline=[Outline([(0, 0), (30, 0), (30, 30), (0, 30)])],
        board_area_mm2=900.0,
        copper_layers=[CopperLayer("F.Cu", "F.Cu", 0), CopperLayer("B.Cu", "B.Cu", 1)],
        nets=[Net("SIG1", "Default", 1), Net("GND", "Default", 1)],
        netclasses=[NetClass("Default", 0.2, 0.2, 0.6, 0.3)],
    ))
    fixed = problem.fixed_parts(board)
    assert "FID1" in fixed and fixed["FID1"] == "mechanical (no nets)"
    assert "R1" not in fixed


def test_local_box_and_legality_catch_pad_protruding_outside_courtyard():
    # Fixed connector has a small courtyard [-1..1] but a shield pad protruding to x=+3.0
    usb = Footprint(
        "USB1", "USB", "Conn:USB", 10.0, 10.0, 0.0, "F", locked=True,
        pads=[
            Pad("1", "VBUS", (0.0, 0.0), (0.6, 0.6), layers=("F.Cu",)),
            Pad("0", "GND", (3.0, 0.0), (1.0, 1.0), layers=("F.Cu",)),
        ],
        courtyard=Courtyard("file", [[(-1.0, -1.0), (1.0, -1.0), (1.0, 1.0), (-1.0, 1.0)]]),
        uuid="u-usb",
    )
    box = problem.local_box(usb)
    assert box[2] >= 3.5, f"local_box must enclose protruding pad: {box}"

    # Movable R1 placed on top of USB1's protruding pad (outside USB1's courtyard!)
    r1 = Footprint(
        "R1", "1k", "R:0603", 13.0, 10.0, 0.0, "F", locked=False,
        pads=[
            Pad("1", "LED", (0.0, 0.0), (0.6, 0.6), layers=("F.Cu",)),
            Pad("2", "GND", (1.2, 0.0), (0.6, 0.6), layers=("F.Cu",)),
        ],
        courtyard=Courtyard("file", [[(-0.5, -0.5), (1.7, -0.5), (1.7, 0.5), (-0.5, 0.5)]]),
        uuid="u-r1",
    )
    board = derive(Board(
        name="pad_overlap_test",
        footprints=[usb, r1],
        outline=[Outline([(0, 0), (30, 0), (30, 30), (0, 30)])],
        board_area_mm2=900.0,
        copper_layers=[CopperLayer("F.Cu", "F.Cu", 0), CopperLayer("B.Cu", "B.Cu", 1)],
        nets=[Net("VBUS", "Default", 1), Net("GND", "Default", 2), Net("LED", "Default", 1)],
        netclasses=[NetClass("Default", 0.2, 0.2, 0.6, 0.3)],
    ))
    findings = legality.check(board, fixed={"USB1"})
    pad_hits = [f for f in findings if f.detail == "pad_overlap" and not f.fixed]
    assert len(pad_hits) == 1
    assert pad_hits[0].refs == ["R1", "USB1"]
    assert pad_hits[0].waived_by is None


def test_pad_keepout_in_board_mask_obstacles_blocks_movable_pads():
    r1 = Footprint(
        "R1", "10k", "R:0603", 10.0, 10.0, 0.0, "F", locked=False,
        pads=[Pad("1", "SIG1", (0.0, 0.0), (0.6, 0.6), layers=("F.Cu",))],
        courtyard=Courtyard("file", [[(-0.5, -0.5), (0.5, -0.5), (0.5, 0.5), (-0.5, 0.5)]]),
        uuid="u-r1",
    )
    fid = Footprint(
        "FID1", "Fid", "Fid:1mm", 10.0, 10.0, 0.0, "F", locked=True,
        pads=[Pad("Fid1", "", (0.0, 0.0), (1.0, 1.0), layers=("F.Cu",))],
        courtyard=Courtyard("file", [[(-0.6, -0.6), (0.6, -0.6), (0.6, 0.6), (-0.6, 0.6)]]),
        uuid="u-fid",
    )
    board = derive(Board(
        name="fid_keepout",
        footprints=[fid, r1],
        outline=[Outline([(0, 0), (30, 0), (30, 30), (0, 30)])],
        board_area_mm2=900.0,
        copper_layers=[CopperLayer("F.Cu", "F.Cu", 0), CopperLayer("B.Cu", "B.Cu", 1)],
        nets=[Net("SIG1", "Default", 1)],
        netclasses=[NetClass("Default", 0.2, 0.2, 0.6, 0.3)],
        obstacles=[{
            "uuid": "fid-pad-1",
            "kind": "pad_keepout",
            "source": "footprint:FID1",
            "layer": "F.Cu",
            "copper": "F.Cu",
            "outlines": [[(8.5, 8.5), (11.5, 8.5), (11.5, 11.5), (8.5, 11.5)]],
            "overlapping_pads": [],
        }],
    ))
    findings = legality.check(board, fixed={"FID1"})
    assert any(f.kind == "keepout" and f.detail == "mask_aperture:fid-pad-1" for f in findings)


def test_intent_wire_keepouts_protect_crystal_and_inductor_without_trapping_pads():
    # Through-hole crystal (pads on both layers) + SMD inductor (pads on F.Cu only) on 2L board
    xtal = Footprint(
        "X1", "16MHz", "Crystal:HC49", 10.0, 10.0, 0.0, "F", locked=True,
        pads=[
            Pad("1", "XTAL1", (-2.5, 0.0), (1.0, 1.0), kind="thru_hole", drill=0.6, layers=("F.Cu", "B.Cu")),
            Pad("2", "XTAL2", (2.5, 0.0), (1.0, 1.0), kind="thru_hole", drill=0.6, layers=("F.Cu", "B.Cu")),
        ],
        courtyard=Courtyard("file", [[(-4.5, -2.5), (4.5, -2.5), (4.5, 2.5), (-4.5, 2.5)]]),
        uuid="u-x1",
    )
    ind = Footprint(
        "L1", "4.7uH", "Inductor:4x4", 25.0, 10.0, 0.0, "F", locked=True,
        pads=[
            Pad("1", "SW", (-1.8, 0.0), (0.8, 2.0), layers=("F.Cu",)),
            Pad("2", "VOUT", (1.8, 0.0), (0.8, 2.0), layers=("F.Cu",)),
        ],
        courtyard=Courtyard("file", [[(-2.5, -2.5), (2.5, -2.5), (2.5, 2.5), (-2.5, 2.5)]]),
        uuid="u-l1",
    )
    board = derive(Board(
        name="keepout_test",
        footprints=[xtal, ind],
        outline=[Outline([(0, 0), (40, 0), (40, 20), (0, 20)])],
        board_area_mm2=800.0,
        copper_layers=[CopperLayer("top_copper", "F.Cu", 0), CopperLayer("bottom_copper", "B.Cu", 1)],
        nets=[Net("XTAL1", "Default", 1), Net("XTAL2", "Default", 1), Net("SW", "Default", 1), Net("VOUT", "Default", 1)],
        netclasses=[NetClass("Default", 0.2, 0.2, 0.6, 0.3)],
        rules={"min_clearance": 0.2, "min_track_width": 0.15},
    ))
    intent = {"parts": {"X1": {"role": _claim("crystal")}, "L1": {"role": _claim("converter_inductor")}}}
    kps = route_check_mod.intent_wire_keepouts(board, intent)
    assert kps, "Expected keepouts under crystal and inductor"
    # Uses exact board layer names (top_copper / bottom_copper), never hardcoded F.Cu/B.Cu
    assert {k.layer for k in kps} <= {"top_copper", "bottom_copper"}
    # Crucially, NO keepout polygon may cover any pad center of X1 or L1!
    for kp in kps:
        poly = Polygon(kp.outline)
        for fp in (xtal, ind):
            for pad, (px, py) in fp.pads_world():
                if kp.layer == "bottom_copper" and not legality.has_thru_hole_pad(pad):
                    continue
                assert not poly.contains(Point(px, py)), f"Keepout {kp.name} trapped pad {fp.ref}-{pad.number}!"
    # L1 opposite side (bottom_copper) must be protected to stop fb_under_inductor
    assert any("L1" in k.name and k.layer == "bottom_copper" for k in kps)

    # 4L board with solid In1.Cu GND plane shields B.Cu from an F.Cu inductor
    board4 = derive(Board(
        name="keepout_4l",
        footprints=[ind],
        outline=[Outline([(0, 0), (40, 0), (40, 20), (0, 20)])],
        board_area_mm2=800.0,
        copper_layers=[
            CopperLayer("F.Cu", "F.Cu", 0),
            CopperLayer("In1.Cu", "In1.Cu", 1),
            CopperLayer("In2.Cu", "In2.Cu", 2),
            CopperLayer("B.Cu", "B.Cu", 3),
        ],
        nets=[Net("SW", "Default", 1), Net("VOUT", "Default", 1), Net("GND", "Default", 1)],
        netclasses=[NetClass("Default", 0.2, 0.2, 0.6, 0.3)],
        rules={"min_clearance": 0.2, "min_track_width": 0.15},
    ))
    board4.stackup.roles = [
        LayerRole("F.Cu", "signal", None, [], 0.0, 1.0),
        LayerRole("In1.Cu", "plane", "GND", ["GND"], 0.95, 1.0),
        LayerRole("In2.Cu", "plane", "+3V3", ["+3V3"], 0.95, 1.0),
        LayerRole("B.Cu", "signal", None, [], 0.0, 1.0),
    ]
    kps4 = route_check_mod.intent_wire_keepouts(board4, {"parts": {"L1": {"role": _claim("converter_inductor")}}})
    assert all(k.layer == "F.Cu" for k in kps4), f"Solid GND plane should shield B.Cu: {kps4}"


def test_router_item_uuids_extracts_only_routing_items():
    violations = [
        {
            "type": "clearance",
            "items": [
                {"description": "Track [GND] on F.Cu, length 1.2 mm", "uuid": "uuid-track-1"},
                {"description": "Pad 1 [GND] of U1 on F.Cu", "uuid": "uuid-pad-1"},
                {"description": "Via [SIG1] on F.Cu - B.Cu", "uuid": "uuid-via-1"},
                {"description": "Arc of U2 on F.Silkscreen", "uuid": "uuid-silk-1"},
            ],
        }
    ]
    assert route_check_mod._router_item_uuids(violations) == ["uuid-track-1", "uuid-via-1"]


def test_s6a_taps_include_critical_passives_on_four_layer_board():
    outline = [(0, 0), (20, 0), (20, 20), (0, 20)]
    cin = Footprint(
        "C10", "10uF", "C:0603", 10.0, 10.0, 0.0, side="F",
        pads=[
            Pad("1", "VIN", (-0.8, 0.0), (0.5, 0.5), layers=("F.Cu",)),
            Pad("2", "GND", (0.8, 0.0), (0.5, 0.5), layers=("F.Cu",)),
        ],
    )
    esd = Footprint(
        "D1", "TVS", "D:0402", 15.0, 10.0, 0.0, side="F",
        pads=[
            Pad("1", "USB+", (-0.6, 0.0), (0.4, 0.4), layers=("F.Cu",)),
            Pad("2", "GND", (0.6, 0.0), (0.4, 0.4), layers=("F.Cu",)),
        ],
    )
    board = Board(
        "4l_passives", [cin, esd], outline=[Outline(outline)],
        copper_layers=[CopperLayer("F.Cu", "F.Cu", 0), CopperLayer("In1.Cu", "In1.Cu", 1), CopperLayer("B.Cu", "B.Cu", 2)],
        nets=[Net("GND", "Default", 2), Net("VIN", "Default", 1), Net("USB+", "Default", 1)],
        netclasses=[NetClass("Default", 0.15, 0.2, 0.5, 0.3)],
        zones=[{"net": "GND", "layers": ["In1.Cu"], "outline": [outline], "uuid": "z1"}],
        rules={"hole_to_hole": 0.25},
    )
    board.stackup.roles = [
        LayerRole("F.Cu", "signal", None, [], 0.0, 1.0),
        LayerRole("In1.Cu", "plane", "GND", ["GND"], 1.0, 1.0),
        LayerRole("B.Cu", "signal", None, [], 0.0, 1.0),
    ]
    intent = {
        "parts": {
            "C10": {"role": _claim("regulator_input_cap")},
            "D1": {"role": _claim("esd")},
        },
        "diff_pairs": [],
    }
    planned = s6a.plan(board, intent, pairs=False)
    routed_taps = {(r["ref"], r["pad"]) for r in planned["report"]["taps"] if r["routed"]}
    assert ("C10", "2") in routed_taps
    assert ("D1", "2") in routed_taps
